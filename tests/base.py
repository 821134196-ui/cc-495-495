"""测试基座：每个用例独立临时目录（SQLite 库 + 协议文件），互不污染。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import adapters
import db
import services


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        db.configure(os.path.join(self.tmp, "test.db"))
        adapters.configure(self.tmp)
        db.init_db()
        with db.tx() as conn:
            conn.executemany("INSERT INTO colleges(id, name) VALUES (?,?)",
                             [(1, "计算机学院"), (2, "外国语学院")])
            conn.executemany("INSERT INTO teachers(id, name, college_id) VALUES (?,?,?)",
                             [(1, "王芳", 1), (2, "刘洋", 2), (3, "陈明", 1)])
            conn.executemany("INSERT INTO students(id, name, student_no, college_id) VALUES (?,?,?,?)",
                             [(1, "张三", "S001", 1), (2, "李四", "S002", 2)])
            conn.executemany("INSERT INTO companies(id, name) VALUES (?,?)",
                             [(1, "星图科技"), (2, "蓝海制造")])
            conn.executemany("INSERT INTO contacts(id, company_id, name, active) VALUES (?,?,?,1)",
                             [(1, 1, "李雷"), (2, 2, "韩梅")])
            conn.executemany("INSERT INTO positions(id, company_id, title, duties, status, created_at) VALUES (?,?,?,?, 'open', ?)",
                             [(1, 1, "数据分析实习生", "职责A", services.now()),
                              (2, 1, "前端开发实习生", "职责B", services.now()),
                              (3, 2, "翻译助理", "职责C", services.now())])

    # ---------- 快捷动作 ----------
    @staticmethod
    def actor(role, rid):
        return {"role": role, "id": rid}

    def make_application(self, student=1, position=1, teacher=1,
                         start="2026-07-01", end="2026-12-31"):
        return services.create_application(
            self.actor("student", student), student, position, teacher, start, end)["id"]

    def to_signing(self, student=1, position=1, teacher=1, contact=1, reviewer=1, **kw):
        app_id = self.make_application(student, position, teacher, **kw)
        services.college_review(app_id, self.actor("teacher", reviewer))
        agid = services.company_confirm(app_id, self.actor("contact", contact))["agreement_id"]
        return app_id, agid

    def to_effective(self, student=1, position=1, teacher=1, contact=1, reviewer=1, **kw):
        app_id, agid = self.to_signing(student, position, teacher, contact, reviewer, **kw)
        services.sign_agreement(agid, "student", self.actor("student", student))
        services.sign_agreement(agid, "college", self.actor("teacher", reviewer))
        services.sign_agreement(agid, "company", self.actor("contact", contact))
        return app_id, agid

    # ---------- 断言工具 ----------
    def assertErr(self, status, fn, *args, **kw):
        with self.assertRaises(services.ServiceError) as cm:
            fn(*args, **kw)
        self.assertEqual(cm.exception.status, status, cm.exception.message)
        return cm.exception

    def events(self, app_id):
        return [r["kind"] for r in db.query(
            "SELECT kind FROM events WHERE application_id=? ORDER BY id", (app_id,))]

    def agreement(self, agid):
        return db.query_one("SELECT * FROM agreements WHERE id=?", (agid,))
