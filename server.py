"""HTTP 层：stdlib ThreadingHTTPServer，JSON API + 静态文件。"""
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import services

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}

# (method, 正则, handler)；路径参数以命名分组传入
ROUTES = [
    ("GET", r"/api/state", lambda: services.get_state()),
    ("GET", r"/api/applications/(?P<app_id>\d+)/chain", lambda app_id: services.get_chain(int(app_id))),
    ("GET", r"/api/changes/(?P<change_id>\d+)/compare", lambda change_id: services.get_compare(int(change_id))),
    ("GET", r"/api/students/(?P<student_id>\d+)/days", lambda student_id: services.get_days(int(student_id))),
    ("GET", r"/api/teachers/(?P<teacher_id>\d+)/todos", lambda teacher_id: services.get_teacher_todos(int(teacher_id))),
    ("POST", r"/api/applications", lambda body: services.create_application(
        body.get("actor"), body.get("student_id"), body.get("position_id"),
        body.get("teacher_id"), body.get("start_date"), body.get("end_date"))),
    ("POST", r"/api/applications/(?P<app_id>\d+)/college-review",
     lambda body, app_id: services.college_review(int(app_id), body.get("actor"))),
    ("POST", r"/api/applications/(?P<app_id>\d+)/company-confirm",
     lambda body, app_id: services.company_confirm(int(app_id), body.get("actor"))),
    ("POST", r"/api/applications/(?P<app_id>\d+)/changes", lambda body, app_id: services.propose_change(
        int(app_id), body.get("actor"), body.get("type"), body.get("payload"))),
    ("POST", r"/api/agreements/(?P<agreement_id>\d+)/sign",
     lambda body, agreement_id: services.sign_agreement(int(agreement_id), body.get("party"), body.get("actor"))),
    ("POST", r"/api/changes/(?P<change_id>\d+)/confirm", lambda body, change_id: services.confirm_change(
        int(change_id), body.get("party"), body.get("actor"))),
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    # -------------------------------------------------- 工具
    def _send(self, status, payload, content_type="application/json; charset=utf-8"):
        body = payload.encode("utf-8") if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise services.ServiceError(400, "请求体不是合法 JSON")

    def _dispatch(self, method):
        path = self.path.split("?", 1)[0]
        if method == "GET" and path == "/api/agreements-file":
            return self._agreement_file()
        for m, pattern, fn in ROUTES:
            if m != method:
                continue
            match = re.fullmatch(pattern, path)
            if match:
                try:
                    if method == "POST":
                        result = fn(self._json_body(), **match.groupdict())
                    else:
                        result = fn(**match.groupdict())
                    self._send(200, result)
                except services.ServiceError as e:
                    self._send(e.status, {"error": e.message})
                return
        if method == "GET":
            return self._static(path)
        self._send(404, {"error": "接口不存在"})

    def _agreement_file(self):
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        try:
            text = services.get_agreement_file(int(q.get("id", [0])[0]))
            self._send(200, text, content_type="text/plain; charset=utf-8")
        except services.ServiceError as e:
            self._send(e.status, {"error": e.message})

    def _static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        safe = os.path.normpath(path).lstrip("/")
        full = os.path.join(STATIC_DIR, safe)
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            return self._send(404, {"error": "页面不存在"})
        ext = os.path.splitext(full)[1]
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


def serve(port):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"实习三方协议系统已启动：http://127.0.0.1:{port}  （Ctrl+C 停止）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
