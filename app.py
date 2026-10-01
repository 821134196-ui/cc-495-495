"""一条命令启动：python3 app.py
初始化 SQLite、写入演示数据（含撤岗样例）、启动本地服务。
"""
import os

import adapters
import db
import seed
import server

PORT = int(os.environ.get("PORT", "8000"))


def main():
    db.init_db()
    if seed.seed_if_empty():
        print("已写入演示数据（含单位撤岗样例：周雨 / 前端开发实习生 / 星图科技）")
    print(f"数据目录：{os.path.dirname(db.DB_PATH)}（SQLite 库 + 协议文件 + 通知日志）")
    server.serve(PORT)


if __name__ == "__main__":
    main()
