"""跨学院隔离与越权：老师只能审本学院；学生不能代单位确认；外单位联系人不可操作。"""
from base import BaseCase
import services


class TestIsolation(BaseCase):
    def test_cross_college_review_forbidden(self):
        app_id = self.make_application(student=1, teacher=1)  # 张三：计算机学院
        # 刘洋（外国语学院）审核 → 403 + unauthorized 事件
        self.assertErr(403, services.college_review, app_id, self.actor("teacher", 2))
        self.assertIn("unauthorized", self.events(app_id))
        # 本学院老师可以审
        services.college_review(app_id, self.actor("teacher", 3))  # 陈明（计算机学院）

    def test_cross_college_change_confirm_forbidden(self):
        app_id, _ = self.to_effective()
        ch = services.propose_change(app_id, self.actor("student", 1), "early_end",
                                     {"end_date": "2026-10-31"})
        self.assertErr(403, services.confirm_change, ch["id"], "college", self.actor("teacher", 2))
        services.confirm_change(ch["id"], "college", self.actor("teacher", 1))

    def test_student_cannot_confirm_as_company(self):
        app_id, _ = self.to_effective()
        ch = services.propose_change(app_id, self.actor("teacher", 1), "attachment_fix", {"note": "x"})
        # 学生本人确认 student 方没问题
        services.confirm_change(ch["id"], "student", self.actor("student", 1))
        # 但学生不能代单位确认
        self.assertErr(403, services.confirm_change, ch["id"], "company", self.actor("student", 1))

    def test_other_company_contact_forbidden(self):
        _, agid = self.to_signing()
        services.sign_agreement(agid, "student", self.actor("student", 1))
        services.sign_agreement(agid, "college", self.actor("teacher", 1))
        # 蓝海制造的联系人不能代星图科技签署
        self.assertErr(403, services.sign_agreement, agid, "company", self.actor("contact", 2))

    def test_cross_college_attachment_fix_forbidden(self):
        app_id, _ = self.to_effective()
        self.assertErr(403, services.propose_change, app_id, self.actor("teacher", 2),
                       "attachment_fix", {"note": "x"})

    def test_withdraw_only_by_own_company(self):
        app_id, _ = self.to_effective()
        self.assertErr(403, services.propose_change, app_id, self.actor("contact", 2),
                       "withdraw", {"effective_date": "2026-09-30"})

    def test_teacher_todos_scoped_to_own_college(self):
        # 计算机学院、外国语学院各一条待审申请
        self.make_application(student=1, teacher=1)
        self.make_application(student=2, position=3, teacher=2)
        todos1 = services.get_teacher_todos(1)  # 王芳（计算机）
        todos2 = services.get_teacher_todos(2)  # 刘洋（外国语）
        self.assertEqual([r["student_name"] for r in todos1["reviews"]], ["张三"])
        self.assertEqual([r["student_name"] for r in todos2["reviews"]], ["李四"])
