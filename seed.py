"""演示数据：两学院、四学生、两单位、四个岗位。

关键样例通过服务层真实走流程（事件/通知/协议文件自然生成）：
- 张三：申请 → 审核 → 确认 → 三方签署 → 生效（完整链）
- 周雨：同上生效后，单位撤岗 → 受影响方确认 → v2 终止（撤岗样例，版本链可见）
- 林岚：学生已签、等待学院签署（进度页中间态）
- 赵磊：刚提交，等待学院审核（老师待办）
"""
import db
import services


def _insert_base():
    with db.tx() as conn:
        conn.executemany("INSERT INTO colleges(name) VALUES (?)", [("计算机学院",), ("外国语学院",)])
        conn.executemany(
            "INSERT INTO teachers(name, college_id) VALUES (?,?)",
            [("王芳", 1), ("陈明", 1), ("刘洋", 2)])
        conn.executemany(
            "INSERT INTO students(name, student_no, college_id) VALUES (?,?,?)",
            [("张三", "20230001", 1), ("周雨", "20230002", 1), ("林岚", "20230003", 2), ("赵磊", "20230004", 1)])
        conn.executemany("INSERT INTO companies(name) VALUES (?)", [("星图科技",), ("蓝海制造",)])
        conn.executemany(
            "INSERT INTO contacts(company_id, name, active) VALUES (?,?,1)",
            [(1, "李雷"), (2, "韩梅")])
        conn.executemany(
            "INSERT INTO positions(company_id, title, duties, status, created_at) VALUES (?,?,?, 'open', ?)",
            [(1, "数据分析实习生", "参与业务数据清洗、建模与周报产出，协助搭建数据看板。", services.now()),
             (1, "前端开发实习生", "参与 Web 端页面开发与联调，维护组件库文档。", services.now()),
             (2, "翻译助理", "协助技术文档中英互译与术语库维护。", services.now()),
             (2, "质检实习生", "参与来料检验与产线抽检，记录并汇总质量数据。", services.now())])


def _full_flow_to_effective(student_id, position_id, teacher_id, start, end, contact_id, reviewer_id):
    actor_s = {"role": "student", "id": student_id}
    app_id = services.create_application(actor_s, student_id, position_id, teacher_id, start, end)["id"]
    services.college_review(app_id, {"role": "teacher", "id": reviewer_id})
    agid = services.company_confirm(app_id, {"role": "contact", "id": contact_id})["agreement_id"]
    services.sign_agreement(agid, "student", actor_s)
    services.sign_agreement(agid, "college", {"role": "teacher", "id": reviewer_id})
    services.sign_agreement(agid, "company", {"role": "contact", "id": contact_id})
    return app_id, agid


def seed_if_empty():
    if db.query_one("SELECT id FROM colleges LIMIT 1"):
        return False
    _insert_base()

    # 张三：完整走通，协议生效
    _full_flow_to_effective(1, 1, 1, "2026-07-01", "2026-12-31", contact_id=1, reviewer_id=1)

    # 周雨：生效后单位撤岗 —— 撤岗样例（v1 生效 → 撤岗变更 → v2 终止，天数截断）
    app2, _ = _full_flow_to_effective(2, 2, 1, "2026-07-01", "2026-12-31", contact_id=1, reviewer_id=1)
    ch = services.propose_change(app2, {"role": "contact", "id": 1}, "withdraw", {"effective_date": "2026-09-30"})
    services.confirm_change(ch["id"], "student", {"role": "student", "id": 2})
    services.confirm_change(ch["id"], "college", {"role": "teacher", "id": 1})

    # 林岚：学生已签，等待学院签署
    app3 = services.create_application({"role": "student", "id": 3}, 3, 3, 3, "2026-08-01", "2027-01-31")["id"]
    services.college_review(app3, {"role": "teacher", "id": 3})
    agid3 = services.company_confirm(app3, {"role": "contact", "id": 2})["agreement_id"]
    services.sign_agreement(agid3, "student", {"role": "student", "id": 3})

    # 赵磊：刚提交，等待学院审核
    services.create_application({"role": "student", "id": 4}, 4, 4, 2, "2026-10-15", "2027-04-15")
    return True
