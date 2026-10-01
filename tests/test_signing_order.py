"""签署顺序：固定 学生→学院→单位；乱序拒绝并留痕；学生不能代单位确认。"""
from base import BaseCase
import services


class TestSigningOrder(BaseCase):
    def test_out_of_order_rejected_and_logged(self):
        app_id, agid = self.to_signing()
        # 单位抢在学生之前签署 → 409 + order_error 事件
        e = self.assertErr(409, services.sign_agreement, agid, "company", self.actor("contact", 1))
        self.assertIn("顺序", e.message)
        self.assertIn("order_error", self.events(app_id))
        # 学院抢签同样被拒
        self.assertErr(409, services.sign_agreement, agid, "college", self.actor("teacher", 1))
        # 正确顺序可以走通
        services.sign_agreement(agid, "student", self.actor("student", 1))
        services.sign_agreement(agid, "college", self.actor("teacher", 1))
        r = services.sign_agreement(agid, "company", self.actor("contact", 1))
        self.assertEqual(r["status"], "effective")
        self.assertEqual(self.agreement(agid)["status"], "effective")

    def test_student_cannot_sign_as_company(self):
        app_id, agid = self.to_signing()
        services.sign_agreement(agid, "student", self.actor("student", 1))
        services.sign_agreement(agid, "college", self.actor("teacher", 1))
        # 轮到单位签署时，学生试图代单位签署 → 403 + unauthorized 事件
        e = self.assertErr(403, services.sign_agreement, agid, "company", self.actor("student", 1))
        self.assertIn("无权", e.message)
        self.assertIn("unauthorized", self.events(app_id))
        # 单位本人签署成功
        services.sign_agreement(agid, "company", self.actor("contact", 1))
        self.assertEqual(self.agreement(agid)["status"], "effective")

    def test_sign_effective_agreement_rejected(self):
        _, agid = self.to_effective()
        # 已生效协议不能再签（防止重复签署/旧版复活）
        self.assertErr(409, services.sign_agreement, agid, "student", self.actor("student", 1))

    def test_effective_creates_day_segment_and_file(self):
        app_id, agid = self.to_effective()
        days = services.get_days(1)
        self.assertEqual(days["total_days"], 184)  # 2026-07-01 ~ 2026-12-31
        self.assertTrue(days["continuous"])
        self.assertEqual(days["segments"][0]["agreement_id"], agid)
        # 协议文件已归档，文件名含版本号
        import adapters, os
        path = os.path.join(adapters.DATA_DIR, self.agreement(agid)["file_path"])
        self.assertTrue(os.path.isfile(path))
        self.assertIn("v1", os.path.basename(path))
