"""Flask Web 层:进度页、岗位申请、签署、变更对比、指导老师待办、协议归档。"""
import os

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_from_directory, session, url_for)

from . import services as svc
from .db import connect, dirs_for, init_db
from .seed import seed

ROLE_LABEL = {"student": "学生", "teacher": "老师", "contact": "单位联系人"}


def create_app(db_path):
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "internship-demo-key"
    app.config["DB_PATH"] = db_path

    def get_db():
        if "db" not in g:
            g.db = connect(db_path)
        return g.db

    @app.teardown_appcontext
    def close_db(exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    # ------------------------------------------------------------ 用户切换
    def current_user():
        raw = session.get("user")
        if not raw:
            return None
        role, uid = raw.split(":")
        return {"role": role, "id": int(uid)}

    def user_key(user):
        return f"{user['role']}:{user['id']}" if user else ""

    def display_user(conn, user):
        if not user:
            return "未选择身份"
        if user["role"] == "student":
            r = conn.execute("SELECT name FROM students WHERE id=?",
                             (user["id"],)).fetchone()
        elif user["role"] == "teacher":
            r = conn.execute("SELECT name FROM teachers WHERE id=?",
                             (user["id"],)).fetchone()
        else:
            r = conn.execute("SELECT name FROM unit_contacts WHERE id=?",
                             (user["id"],)).fetchone()
        return f"{ROLE_LABEL[user['role']]} · {r['name'] if r else '?'}"

    @app.context_processor
    def inject_globals():
        conn = get_db()
        user = current_user()
        unread = 0
        if user:
            unread = conn.execute(
                "SELECT COUNT(*) c FROM notifications WHERE user_key=? AND read=0",
                (user_key(user),)).fetchone()["c"]
        return {
            "cu": user,
            "cu_display": display_user(conn, user),
            "unread": unread,
            "users": svc.list_users(conn),
            "PARTY_LABEL": svc.PARTY_LABEL,
            "CHANGE_LABEL": svc.CHANGE_LABEL,
            "APP_STATUS_LABEL": svc.APP_STATUS_LABEL,
            "AGREEMENT_STATUS_LABEL": svc.AGREEMENT_STATUS_LABEL,
        }

    @app.post("/switch-user")
    def switch_user():
        session["user"] = request.form["user"]
        flash("已切换身份")
        return redirect(request.form.get("next") or url_for("dashboard"))

    def run_service(fn, *args, **kwargs):
        """统一捕获业务异常,转成 flash 提示。返回 (成功与否, 结果)。"""
        try:
            return True, fn(get_db(), db_path, *args, **kwargs)
        except svc.BizError as e:
            flash(str(e), "error")
            return False, None

    # ------------------------------------------------------------ 进度页
    @app.get("/")
    def dashboard():
        return render_template("dashboard.html", rows=svc.list_progress(get_db()))

    # ------------------------------------------------------------ 岗位申请
    @app.route("/apply", methods=["GET", "POST"])
    def apply():
        conn = get_db()
        user = current_user()
        if not user or user["role"] != "student":
            flash("请以学生身份提交岗位申请", "error")
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            ok, app_id = run_service(
                svc.create_application, user,
                int(request.form["position_id"]), int(request.form["teacher_id"]),
                request.form["start_date"], request.form["end_date"])
            if ok:
                flash("申请已提交,等待学院审核")
                return redirect(url_for("detail", app_id=app_id))
        stu = conn.execute("SELECT * FROM students WHERE id=?",
                           (user["id"],)).fetchone()
        return render_template(
            "apply.html",
            positions=svc.open_positions(conn),
            teachers=svc.teachers_of_college(conn, stu["college_id"]))

    # ------------------------------------------------------------ 申请详情
    @app.get("/applications/<int:app_id>")
    def detail(app_id):
        conn = get_db()
        data = svc.get_detail(conn, app_id)
        user = current_user()
        actions = _available_actions(conn, data, user)
        return render_template("detail.html", **data, actions=actions,
                               trail=svc.audit_trail(conn, app_id))

    def _available_actions(conn, data, user):
        """当前用户在该申请上可执行的操作。"""
        acts = {"review": False, "unit_confirm": False, "sign": None,
                "confirm_change": None, "changes": []}
        if not user:
            return acts
        app = data["app"]
        if user["role"] == "teacher" and app["status"] == "pending_college":
            tea = conn.execute("SELECT * FROM teachers WHERE id=?",
                               (user["id"],)).fetchone()
            acts["review"] = tea and tea["college_id"] == app["student_college_id"]
        if user["role"] == "contact" and app["status"] == "pending_unit":
            pos = conn.execute("SELECT * FROM positions WHERE id=?",
                               (app["position_id"],)).fetchone()
            contact = conn.execute("SELECT * FROM unit_contacts WHERE id=?",
                                   (user["id"],)).fetchone()
            acts["unit_confirm"] = (contact and contact["active"]
                                    and contact["unit_id"] == pos["unit_id"])
        for vd in data["versions"]:
            ag = vd["agreement"]
            party = svc._party_of_user(conn, user, ag)
            if ag["status"] == "signing" and party:
                sig = next((s for s in vd["sigs"] if s["party"] == party), None)
                if sig and sig["status"] == "pending":
                    turn = all(s["status"] == "signed" for s in vd["sigs"]
                               if s["seq"] < sig["seq"])
                    acts["sign"] = {"agreement_id": ag["id"], "party": party,
                                    "turn": turn, "version": ag["version"]}
            if ag["status"] == "change_pending" and party:
                conf = next((c for c in vd["confs"] if c["party"] == party), None)
                if conf and conf["status"] == "pending":
                    acts["confirm_change"] = {"agreement_id": ag["id"],
                                              "version": ag["version"]}
            if ag["status"] == "effective" and party:
                acts["changes"] = _change_links(user)
        return acts

    def _change_links(user):
        links = []
        if user["role"] == "contact":
            links += [("withdrawal", "单位撤岗"), ("contact_change", "更换联系人")]
        if user["role"] == "student":
            links.append(("transfer", "学生转岗"))
        links.append(("early_end", "提前结束"))
        links.append(("attachment_fix", "附件修订"))
        return links

    # ------------------------------------------------------------ 审核 / 确认 / 签署
    @app.post("/applications/<int:app_id>/review")
    def review(app_id):
        ok, _ = run_service(svc.college_review, current_user(), app_id,
                            request.form.get("decision") == "approve",
                            request.form.get("note", ""))
        if ok:
            flash("审核完成")
        return redirect(url_for("detail", app_id=app_id))

    @app.post("/applications/<int:app_id>/unit-confirm")
    def unit_confirm(app_id):
        ok, _ = run_service(svc.unit_confirm, current_user(), app_id)
        if ok:
            flash("单位已确认,协议第 1 版生成,进入三方签署")
        return redirect(url_for("detail", app_id=app_id))

    @app.post("/agreements/<int:ag_id>/sign")
    def sign(ag_id):
        ok, _ = run_service(svc.sign, current_user(), ag_id)
        ag = get_db().execute("SELECT application_id FROM agreements WHERE id=?",
                              (ag_id,)).fetchone()
        if ok:
            flash("签署完成")
        return redirect(url_for("detail", app_id=ag["application_id"]))

    # ------------------------------------------------------------ 变更
    @app.route("/applications/<int:app_id>/change/<change_type>", methods=["GET", "POST"])
    def change(app_id, change_type):
        conn = get_db()
        user = current_user()
        if change_type not in svc.CHANGE_LABEL or change_type == "initial":
            abort(404)
        if request.method == "POST":
            params = {
                "reason": request.form.get("reason", ""),
                "new_position_id": (int(request.form["new_position_id"])
                                    if request.form.get("new_position_id") else None),
                "new_end_date": request.form.get("new_end_date") or None,
                "new_contact_id": (int(request.form["new_contact_id"])
                                   if request.form.get("new_contact_id") else None),
            }
            ok, new_id = run_service(svc.initiate_change, user, app_id,
                                     change_type, params)
            if ok:
                ver = conn.execute("SELECT version FROM agreements WHERE id=?",
                                   (new_id,)).fetchone()["version"]
                flash(f"变更已发起,等待受影响方确认(第 {ver} 版)")
                return redirect(url_for("detail", app_id=app_id))
        cur = conn.execute(
            "SELECT * FROM agreements WHERE application_id=? AND status='effective'",
            (app_id,)).fetchone()
        if cur is None:
            flash("当前没有生效中的协议,不能发起变更", "error")
            return redirect(url_for("detail", app_id=app_id))
        pos = conn.execute("SELECT * FROM positions WHERE id=?",
                           (cur["position_id"],)).fetchone()
        same_unit_positions = conn.execute(
            "SELECT * FROM positions WHERE unit_id=? AND status='open' AND id!=?",
            (pos["unit_id"], pos["id"])).fetchall()
        contacts = conn.execute(
            "SELECT * FROM unit_contacts WHERE unit_id=? AND active=1 AND id!=?",
            (pos["unit_id"], cur["unit_contact_id"])).fetchall()
        return render_template("change_form.html", app_id=app_id,
                               change_type=change_type, cur=cur,
                               positions=same_unit_positions, contacts=contacts)

    @app.post("/agreements/<int:ag_id>/confirm-change")
    def confirm_change(ag_id):
        ok, _ = run_service(svc.confirm_change, current_user(), ag_id)
        ag = get_db().execute("SELECT application_id FROM agreements WHERE id=?",
                              (ag_id,)).fetchone()
        if ok:
            flash("确认完成")
        return redirect(url_for("detail", app_id=ag["application_id"]))

    @app.get("/applications/<int:app_id>/compare")
    def compare(app_id):
        conn = get_db()
        versions = conn.execute(
            "SELECT * FROM agreements WHERE application_id=? ORDER BY version",
            (app_id,)).fetchall()
        if len(versions) < 2:
            flash("该申请还没有可对比的版本", "error")
            return redirect(url_for("detail", app_id=app_id))
        old_id = int(request.args.get("old", versions[-2]["id"]))
        new_id = int(request.args.get("new", versions[-1]["id"]))
        data = svc.compare_versions(conn, app_id, old_id, new_id)
        return render_template("compare.html", app_id=app_id, versions=versions, **data)

    # ------------------------------------------------------------ 待办 / 归档 / 通知
    @app.get("/todo")
    def todo():
        user = current_user()
        if not user or user["role"] != "teacher":
            flash("请以指导老师身份查看待办", "error")
            return redirect(url_for("dashboard"))
        return render_template("todo.html", todo=svc.teacher_todo(get_db(), user["id"]))

    @app.get("/archive")
    def archive():
        return render_template("archive.html", groups=svc.archive(get_db()))

    @app.get("/agreements/<int:ag_id>/file")
    def agreement_file(ag_id):
        conn = get_db()
        ag = conn.execute("SELECT * FROM agreements WHERE id=?", (ag_id,)).fetchone()
        if not ag or not ag["file_path"]:
            abort(404)
        full = os.path.join(dirs_for(db_path)["base"], ag["file_path"])
        with open(full, encoding="utf-8") as f:
            content = f.read()
        return render_template("agreement_file.html", ag=ag, content=content)

    @app.get("/files/<path:relpath>")
    def files(relpath):
        base = dirs_for(db_path)["base"]
        if not (relpath.startswith("agreements/") or relpath.startswith("attachments/")):
            abort(403)
        return send_from_directory(base, relpath)

    @app.get("/notifications")
    def notifications():
        user = current_user()
        if not user:
            flash("请先选择身份", "error")
            return redirect(url_for("dashboard"))
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM notifications WHERE user_key=? ORDER BY id DESC",
            (user_key(user),)).fetchall()
        conn.execute("UPDATE notifications SET read=1 WHERE user_key=?",
                     (user_key(user),))
        conn.commit()
        return render_template("notifications.html", rows=rows)

    return app


def make_default_app():
    """一条命令启动用:建库、种子(含撤岗样例)、返回 app。"""
    from .db import DB_PATH
    conn = init_db(DB_PATH)
    seed(conn, DB_PATH)
    conn.close()
    return create_app(DB_PATH)
