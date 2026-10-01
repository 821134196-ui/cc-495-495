"""并发审批：两名审核人同时操作，只有一个生效；旧版本绝不能重新生效。"""
import sqlite3
import threading

from base import BaseCase
import db
import services


def run_parallel(fn_a, fn_b):
    """两个函数在屏障后同时起跑，返回 (结果列表, ServiceError 列表)。"""
    barrier = threading.Barrier(2)
    results, errors = [None, None], [None, None]

    def wrap(i, fn):
        try:
            barrier.wait(timeout=10)
            results[i] = fn()
        except services.ServiceError as e:
            errors[i] = e

    threads = [threading.Thread(target=wrap, args=(i, f)) for i, f in enumerate((fn_a, fn_b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results, errors


class TestConcurrency(BaseCase):
    def test_concurrent_review_single_winner(self):
        app_id = self.make_application()
        # 王芳、陈明（同学院两名审核人）同时审核同一申请
        results, errors = run_parallel(
            lambda: services.college_review(app_id, self.actor("teacher", 1)),
            lambda: services.college_review(app_id, self.actor("teacher", 3)),
        )
        self.assertEqual(sum(r is not None for r in results), 1, "只能有一个审核人成功")
        self.assertEqual(sum(e is not None and e.status == 409 for e in errors), 1)
        self.assertEqual(db.query_one("SELECT status FROM applications WHERE id=?", (app_id,))["status"],
                         "college_approved")
        self.assertEqual(self.events(app_id).count("college_approved"), 1)

    def test_concurrent_same_party_sign_single_record(self):
        _, agid = self.to_signing()
        results, errors = run_parallel(
            lambda: services.sign_agreement(agid, "student", self.actor("student", 1)),
            lambda: services.sign_agreement(agid, "student", self.actor("student", 1)),
        )
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(sum(e is not None and e.status == 409 for e in errors), 1)
        n = db.query_one("SELECT COUNT(*) AS n FROM signatures WHERE agreement_id=?", (agid,))["n"]
        self.assertEqual(n, 1)

    def test_concurrent_change_confirm_applied_once(self):
        app_id, _ = self.to_effective()
        # 老师发起提前结束（学院方自动确认），学生与单位同时确认
        ch = services.propose_change(app_id, self.actor("teacher", 1), "early_end",
                                     {"end_date": "2026-10-31"})
        results, errors = run_parallel(
            lambda: services.confirm_change(ch["id"], "student", self.actor("student", 1)),
            lambda: services.confirm_change(ch["id"], "company", self.actor("contact", 1)),
        )
        self.assertEqual(sum(r is not None for r in results), 2)  # 两方确认都应被接受
        change = db.query_one("SELECT * FROM changes WHERE id=?", (ch["id"],))
        self.assertEqual(change["status"], "applied")
        # 只应用一次：恰好两个版本，v1 被取代，v2 终止
        versions = db.query("SELECT version, status FROM agreements WHERE application_id=? ORDER BY version", (app_id,))
        self.assertEqual([(v["version"], v["status"]) for v in versions],
                         [(1, "superseded"), (2, "terminated")])
        self.assertEqual(self.events(app_id).count("change_applied"), 1)
        # 天数只截断一次
        self.assertEqual(services.get_days(1)["total_days"], 123)

    def test_old_version_never_reactivated_under_race(self):
        """两名审核人同时确认变更：旧版本不能重新生效，生效版本唯一。"""
        app_id, agid = self.to_effective()
        ch = services.propose_change(app_id, self.actor("student", 1), "transfer",
                                     {"new_position_id": 3, "effective_date": "2026-11-02"})
        # 学院、单位两名审核人同时确认（学生发起时已自动确认）
        results, errors = run_parallel(
            lambda: services.confirm_change(ch["id"], "college", self.actor("teacher", 1)),
            lambda: services.confirm_change(ch["id"], "company", self.actor("contact", 2)),
        )
        self.assertEqual(sum(r is not None for r in results), 2)
        # 生效版本唯一且为 v2
        effective = db.query("SELECT * FROM agreements WHERE application_id=? AND status='effective'", (app_id,))
        self.assertEqual(len(effective), 1)
        self.assertEqual(effective[0]["version"], 2)
        self.assertEqual(self.agreement(agid)["status"], "superseded")
        # 旧版本不能通过签署复活
        self.assertErr(409, services.sign_agreement, agid, "student", self.actor("student", 1))
        # 数据库层面同样无法把旧版本改回生效（唯一部分索引）
        with self.assertRaises(sqlite3.IntegrityError):
            with db.tx() as conn:
                conn.execute("UPDATE agreements SET status='effective' WHERE id=?", (agid,))

    def test_concurrent_propose_change_single_pending(self):
        app_id, _ = self.to_effective()
        results, errors = run_parallel(
            lambda: services.propose_change(app_id, self.actor("student", 1), "early_end",
                                            {"end_date": "2026-10-31"}),
            lambda: services.propose_change(app_id, self.actor("contact", 1), "withdraw",
                                            {"effective_date": "2026-09-30"}),
        )
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(sum(e is not None and e.status == 409 for e in errors), 1)
        n = db.query_one("SELECT COUNT(*) AS n FROM changes WHERE application_id=? AND status='pending'", (app_id,))["n"]
        self.assertEqual(n, 1)
