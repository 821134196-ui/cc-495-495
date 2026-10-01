"""种子数据:两个学院、三名学生、两家单位,以及一条完整的撤岗版本链样例。

撤岗样例(APP-1):
  张晓 2026-07-01 起在云舟科技做后端开发实习,协议第 1 版三方签齐生效;
  2026-09-15 单位撤岗(后端岗位撤销),生成第 2 版(转测试开发岗、
  结束日期提前到 11-30),学生与学院重新确认后第 2 版生效、第 1 版失效。
  实习天数台账:07-01~09-16(第1版) + 09-17~11-30(第2版),连续可查。
"""
from . import services as svc

COLLEGES = ["计算机学院", "外国语学院"]
TEACHERS = [("王敏", 1), ("李强", 1), ("陈静", 2)]
STUDENTS = [("张晓", "2023001", 1), ("李华", "2023002", 1), ("赵雪", "2023003", 2)]
UNITS = ["云舟科技", "恒信咨询"]
CONTACTS = [(1, "刘洋"), (1, "周凯"), (2, "吴芳")]
POSITIONS = [
    (1, "后端开发实习生", "参与服务端接口开发与单元测试,跟随导师完成迭代任务。"),
    (1, "测试开发实习生", "编写自动化测试脚本,维护持续集成流水线。"),
    (2, "数据分析实习生", "整理业务数据,制作周报看板,协助建模分析。"),
    (2, "翻译实习生", "负责合同与产品文档的中英互译和校对。"),
]


def seed(conn, db_path):
    if conn.execute("SELECT COUNT(*) c FROM applications").fetchone()["c"]:
        return False

    for name in COLLEGES:
        conn.execute("INSERT INTO colleges(name) VALUES(?)", (name,))
    for name, cid in TEACHERS:
        conn.execute("INSERT INTO teachers(name, college_id) VALUES(?,?)", (name, cid))
    for name, no, cid in STUDENTS:
        conn.execute("INSERT INTO students(name, student_no, college_id) VALUES(?,?,?)",
                     (name, no, cid))
    for name in UNITS:
        conn.execute("INSERT INTO units(name) VALUES(?)", (name,))
    for uid, name in CONTACTS:
        conn.execute("INSERT INTO unit_contacts(unit_id, name) VALUES(?,?)", (uid, name))
    for uid, title, desc in POSITIONS:
        conn.execute("INSERT INTO positions(unit_id, title, description) VALUES(?,?,?)",
                     (uid, title, desc))
    conn.commit()

    zhang = {"role": "student", "id": 1}
    li = {"role": "student", "id": 2}
    zhao = {"role": "student", "id": 3}
    wang = {"role": "teacher", "id": 1}
    li_qiang = {"role": "teacher", "id": 2}
    liu = {"role": "contact", "id": 1}
    wu = {"role": "contact", "id": 3}

    # ---- APP-1:撤岗样例(完整版本链) ----
    app1 = svc.create_application(conn, db_path, zhang, 1, 1,
                                  "2026-07-01", "2026-12-31", today="2026-06-20")
    svc.college_review(conn, db_path, wang, app1, True, "符合培养方案要求",
                       today="2026-06-21")
    svc.unit_confirm(conn, db_path, liu, app1, today="2026-06-22")
    ag1 = conn.execute("SELECT id FROM agreements WHERE application_id=? AND version=1",
                       (app1,)).fetchone()["id"]
    svc.sign(conn, db_path, zhang, ag1, today="2026-06-23")
    svc.sign(conn, db_path, wang, ag1, today="2026-06-24")
    svc.sign(conn, db_path, liu, ag1, today="2026-06-25")  # 第 1 版生效

    v2 = svc.initiate_change(conn, db_path, liu, app1, "withdrawal", {
        "reason": "业务线调整,后端开发岗位撤销,转为测试开发岗位,结束日期相应提前。",
        "new_position_id": 2,
        "new_end_date": "2026-11-30",
    }, today="2026-09-15")
    svc.confirm_change(conn, db_path, zhang, v2, today="2026-09-16")
    svc.confirm_change(conn, db_path, wang, v2, today="2026-09-17")  # 第 2 版生效

    # ---- APP-2:签署流程进行中(学生已签,等学院签) ----
    app2 = svc.create_application(conn, db_path, li, 3, 2,
                                  "2026-10-12", "2027-01-12", today="2026-10-08")
    svc.college_review(conn, db_path, li_qiang, app2, True, "同意", today="2026-10-09")
    svc.unit_confirm(conn, db_path, wu, app2, today="2026-10-09")
    ag2 = conn.execute("SELECT id FROM agreements WHERE application_id=? AND version=1",
                       (app2,)).fetchone()["id"]
    svc.sign(conn, db_path, li, ag2, today="2026-10-10")

    # ---- APP-3:刚提交,待学院审核 ----
    svc.create_application(conn, db_path, zhao, 4, 3,
                           "2026-11-01", "2027-02-01", today="2026-10-01")
    return True
