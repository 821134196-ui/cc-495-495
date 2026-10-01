#!/usr/bin/env python3
"""一条命令启动:python3 run.py [--port 5000] [--reset]

- 自动建库、写入种子数据(含撤岗样例版本链)
- 启动 Web 服务,默认 http://127.0.0.1:5000
"""
import argparse
import os

from app.db import DB_PATH, init_db
from app.seed import seed
from app.web import create_app


def main():
    ap = argparse.ArgumentParser(description="实习三方协议平台")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--reset", action="store_true", help="清空数据后重新种子")
    args = ap.parse_args()

    if args.reset and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(DB_PATH + suffix):
                os.remove(DB_PATH + suffix)

    conn = init_db(DB_PATH)
    if seed(conn, DB_PATH):
        print("已写入种子数据(含撤岗样例 APP-1)")
    conn.close()

    app = create_app(DB_PATH)
    print(f"访问 http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
