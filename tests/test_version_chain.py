"""版本链：变更产生新版本、旧版本即时失效且不能复活；实习天数连续可查。"""
import os

from base import BaseCase
import adapters
import db
import services


class TestVersionChain(BaseCase):
    def test_transfer_new_version_and_old_invalidated(self):
        app_id, agid = self.to_effective()
        ch = services.propose_change(app_id, self.actor("student", 1), "transfer",
                                     {"new_position_id": 3, "effective_date": "2026-11-02"})
        services.confirm_change(ch["id"], "college", self.actor("teacher", 1))
        r = services.confirm_change(ch["id"], "company", self.actor("contact", 2))  # 新岗位属蓝海制造
        self.assertEqual(r["status"], "applied")

        v1, v2 = self.agreement(agid), db.query_one(
            "SELECT * FROM agreements WHERE application_id=? AND version=2", (app_id,))
        self.assertEqual(v1["status"], "superseded")          # 旧版失效
        self.assertEqual(v2["status"], "effective")
        self.assertEqual(v2["prev_id"], agid)                 # 版本链相连
        self.assertEqual(v2["position_title"], "翻译助理")
        self.assertEqual(v2["start_date"], "2026-11-02")
        # 旧版本不能重新签署/生效
        self.assertErr(409, services.sign_agreement, agid, "student", self.actor("student", 1))
        # 同一申请至多一个生效版本
        n = db.query_one("SELECT COUNT(*) AS n FROM agreements WHERE application_id=? AND status='effective'", (app_id,))["n"]
        self.assertEqual(n, 1)
        # 实习天数连续：旧段 07-01~11-01，新段 11-02~12-31
        days = services.get_days(1)
        self.assertTrue(days["continuous"])
        self.assertEqual(days["total_days"], 184)
        self.assertEqual(days["segments"][0]["end_date"], "2026-11-01")
        self.assertEqual(days["segments"][1]["start_date"], "2026-11-02")

    def test_withdraw_terminates_and_truncates_days(self):
        app_id, _ = self.to_effective(position=2)
        ch = services.propose_change(app_id, self.actor("contact", 1), "withdraw",
                                     {"effective_date": "2026-09-30"})
        services.confirm_change(ch["id"], "student", self.actor("student", 1))
        services.confirm_change(ch["id"], "college", self.actor("teacher", 1))

        v2 = db.query_one("SELECT * FROM agreements WHERE application_id=? AND version=2", (app_id,))
        self.assertEqual(v2["status"], "terminated")
        self.assertEqual(v2["end_date"], "2026-09-30")
        self.assertEqual(db.query_one("SELECT status FROM positions WHERE id=2")["status"], "withdrawn")
        self.assertEqual(db.query_one("SELECT status FROM applications WHERE id=?", (app_id,))["status"], "ended")
        days = services.get_days(1)
        self.assertEqual(days["total_days"], 92)  # 07-01 ~ 09-30
        self.assertEqual(days["segments"][0]["end_date"], "2026-09-30")
        self.assertIn("position_withdrawn", self.events(app_id))

    def test_early_end_truncates_days(self):
        app_id, _ = self.to_effective()
        ch = services.propose_change(app_id, self.actor("student", 1), "early_end",
                                     {"end_date": "2026-10-31"})
        services.confirm_change(ch["id"], "college", self.actor("teacher", 1))
        services.confirm_change(ch["id"], "company", self.actor("contact", 1))
        days = services.get_days(1)
        self.assertEqual(days["total_days"], 123)  # 07-01 ~ 10-31
        v2 = db.query_one("SELECT * FROM agreements WHERE application_id=? AND version=2", (app_id,))
        self.assertEqual(v2["status"], "terminated")

    def test_attachment_fix_bumps_attachment_version(self):
        app_id, _ = self.to_effective()
        ch = services.propose_change(app_id, self.actor("teacher", 1), "attachment_fix",
                                     {"note": "补充保险条款"})
        services.confirm_change(ch["id"], "student", self.actor("student", 1))
        services.confirm_change(ch["id"], "company", self.actor("contact", 1))
        v2 = db.query_one("SELECT * FROM agreements WHERE application_id=? AND version=2", (app_id,))
        self.assertEqual(v2["attachment_version"], 2)
        self.assertIn("A2", os.path.basename(v2["file_path"]))
        self.assertTrue(os.path.isfile(os.path.join(adapters.DATA_DIR, v2["file_path"])))

    def test_contact_change_switches_active_contact(self):
        app_id, _ = self.to_effective()
        ch = services.propose_change(app_id, self.actor("contact", 1), "contact_change",
                                     {"new_contact_name": "孙强"})
        services.confirm_change(ch["id"], "student", self.actor("student", 1))
        services.confirm_change(ch["id"], "college", self.actor("teacher", 1))
        v2 = db.query_one("SELECT * FROM agreements WHERE application_id=? AND version=2", (app_id,))
        self.assertEqual(v2["contact_name"], "孙强")
        self.assertEqual(db.query_one("SELECT active FROM contacts WHERE id=1")["active"], 0)
        new = db.query_one("SELECT * FROM contacts WHERE name='孙强'")
        self.assertEqual(new["active"], 1)

    def test_only_one_pending_change(self):
        app_id, _ = self.to_effective()
        services.propose_change(app_id, self.actor("student", 1), "early_end", {"end_date": "2026-10-31"})
        self.assertErr(409, services.propose_change, app_id, self.actor("student", 1),
                       "early_end", {"end_date": "2026-11-30"})

    def test_change_requires_effective_agreement(self):
        app_id, _ = self.to_signing()
        self.assertErr(409, services.propose_change, app_id, self.actor("student", 1),
                       "early_end", {"end_date": "2026-10-31"})

    def test_compare_shows_position_and_date_diff(self):
        app_id, _ = self.to_effective()
        ch = services.propose_change(app_id, self.actor("student", 1), "transfer",
                                     {"new_position_id": 3, "effective_date": "2026-11-02"})
        cmp_ = services.get_compare(ch["id"])
        rows = {r["field"]: r for r in cmp_["rows"]}
        self.assertTrue(rows["岗位"]["changed"])
        self.assertEqual(rows["岗位"]["after"], "翻译助理")
        self.assertTrue(rows["开始日期"]["changed"])
        self.assertFalse(rows["结束日期"]["changed"])
