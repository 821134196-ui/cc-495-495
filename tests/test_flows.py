"""流程测试:签署顺序、旧版失效、跨学院隔离、并发审批、天数连续性等。"""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import services as svc
from app.db import connect, init_db
from app.seed import seed

T0 = "2026-07-01"
T1 = "2026-12-31"


@pytest.fixture
def env(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = init_db(db_path)
    yield conn, db_path
    conn.close()


def build_world(conn):
    """两个学院、两名同学院老师、两家单位、两个岗位。"""
    conn.executemany("INSERT INTO colleges(name) VALUES(?)", [("计算机学院",), ("外国语学院",)])
    conn.executemany("INSERT INTO teachers(name, college_id) VALUES(?,?)",
                     [("王敏", 1), ("李强", 1), ("陈静", 2)])
    conn.executemany("INSERT INTO students(name, student_no, college_id) VALUES(?,?,?)",
                     [("张晓", "001", 1), ("赵雪", "002", 2)])
    conn.executemany("INSERT INTO units(name) VALUES(?)", [("云舟科技",), ("恒信咨询",)])
    conn.executemany("INSERT INTO unit_contacts(unit_id, name) VALUES(?,?)",
                     [(1, "刘洋"), (1, "周凯"), (2, "吴芳")])
    conn.executemany("INSERT INTO positions(unit_id, title, description) VALUES(?,?,?)",
                     [(1, "后端开发实习生", "开发"), (1, "测试开发实习生", "测试"),
                      (2, "数据分析实习生", "分析")])
    conn.commit()


ZHANG = {"role": "student", "id": 1}
ZHAO = {"role": "student", "id": 2}
WANG = {"role": "teacher", "id": 1}      # 计算机学院
LI = {"role": "teacher", "id": 2}        # 计算机学院
CHEN = {"role": "teacher", "id": 3}      # 外国语学院
LIU = {"role": "contact", "id": 1}       # 云舟科技
ZHOU = {"role": "contact", "id": 2}      # 云舟科技
WU = {"role": "contact", "id": 3}        # 恒信咨询


def make_signing_app(conn, db_path):
    """走完 申请→学院审核→单位确认,返回 (app_id, agreement_id)。"""
    app_id = svc.create_application(conn, db_path, ZHANG, 1, 1, T0, T1, today="2026-06-20")
    svc.college_review(conn, db_path, WANG, app_id, True, "符合要求", today="2026-06-21")
    ag_id = svc.unit_confirm(conn, db_path, LIU, app_id, today="2026-06-22")
    return app_id, ag_id


def make_active_app(conn, db_path):
    """三方签齐、协议生效。"""
    app_id, ag_id = make_signing_app(conn, db_path)
    svc.sign(conn, db_path, ZHANG, ag_id, today="2026-06-23")
    svc.sign(conn, db_path, WANG, ag_id, today="2026-06-24")
    svc.sign(conn, db_path, LIU, ag_id, today="2026-06-25")
    return app_id, ag_id


# ---------------------------------------------------------------- 签署顺序

def test_signing_order_enforced(env):
    conn, db_path = env
    build_world(conn)
    app_id, ag_id = make_signing_app(conn, db_path)

    # 单位抢在学生前签署 → 拒绝并留痕
    with pytest.raises(svc.BizError, match="顺序"):
        svc.sign(conn, db_path, LIU, ag_id)
    # 学生签完后,单位仍不能跳过学院
    svc.sign(conn, db_path, ZHANG, ag_id)
    with pytest.raises(svc.BizError, match="顺序"):
        svc.sign(conn, db_path, LIU, ag_id)

    rejected = conn.execute(
        "SELECT COUNT(*) c FROM audit_log WHERE action='sign_order_rejected'"
    ).fetchone()["c"]
    assert rejected == 2  # 两次越序都留了痕

    # 正确顺序可以签完并生效
    svc.sign(conn, db_path, WANG, ag_id)
    svc.sign(conn, db_path, LIU, ag_id)
    ag = conn.execute("SELECT * FROM agreements WHERE id=?", (ag_id,)).fetchone()
    assert ag["status"] == "effective"
    app = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    assert app["status"] == "active"


# ---------------------------------------------------------------- 旧版失效

def test_old_version_cannot_be_reactivated(env):
    conn, db_path = env
    build_world(conn)
    app_id, v1 = make_active_app(conn, db_path)

    # 学生发起提前结束 → 学院、单位确认 → 第 2 版生效
    v2 = svc.initiate_change(conn, db_path, ZHANG, app_id, "early_end",
                             {"new_end_date": "2026-09-30", "reason": "课程冲突"},
                             today="2026-09-10")
    svc.confirm_change(conn, db_path, WANG, v2, today="2026-09-11")
    svc.confirm_change(conn, db_path, LIU, v2, today="2026-09-12")

    rows = conn.execute(
        "SELECT version, status FROM agreements WHERE application_id=? ORDER BY version",
        (app_id,)).fetchall()
    assert [(r["version"], r["status"]) for r in rows] == [
        (1, "superseded"), (2, "effective")]

    # 旧版本不能再签署、不能重新生效
    with pytest.raises(svc.BizError, match="不在签署中"):
        svc.sign(conn, db_path, ZHANG, v1)
    with pytest.raises(svc.BizError, match="不在签署中"):
        svc.sign(conn, db_path, LIU, v1)
    assert conn.execute(
        "SELECT COUNT(*) c FROM agreements WHERE application_id=? AND status='effective'",
        (app_id,)).fetchone()["c"] == 1


# ---------------------------------------------------------------- 跨学院隔离

def test_cross_college_isolation(env):
    conn, db_path = env
    build_world(conn)
    # 张晓是计算机学院学生,陈静是外国语学院老师
    app_id = svc.create_application(conn, db_path, ZHANG, 1, 1, T0, T1)
    with pytest.raises(svc.BizError, match="本学院"):
        svc.college_review(conn, db_path, CHEN, app_id, True)
    # 留痕
    assert conn.execute(
        "SELECT COUNT(*) c FROM audit_log WHERE action='review_rejected_cross_college'"
    ).fetchone()["c"] == 1
    # 本学院老师可以审
    svc.college_review(conn, db_path, WANG, app_id, True)
    # 外国语学院老师也不能代签学院方
    svc.unit_confirm(conn, db_path, LIU, app_id)
    ag_id = conn.execute("SELECT id FROM agreements WHERE application_id=?",
                         (app_id,)).fetchone()["id"]
    svc.sign(conn, db_path, ZHANG, ag_id)
    with pytest.raises(svc.BizError, match="任何一方"):
        svc.sign(conn, db_path, CHEN, ag_id)


def test_teacher_todo_only_own_college(env):
    conn, db_path = env
    build_world(conn)
    svc.create_application(conn, db_path, ZHANG, 1, 1, T0, T1)   # 计算机学院
    svc.create_application(conn, db_path, ZHAO, 3, 3, T0, T1)    # 外国语学院
    todo_wang = svc.teacher_todo(conn, WANG["id"])
    todo_chen = svc.teacher_todo(conn, CHEN["id"])
    assert [a["student_id"] for a in todo_wang["reviews"]] == [ZHANG["id"]]
    assert [a["student_id"] for a in todo_chen["reviews"]] == [ZHAO["id"]]


# ---------------------------------------------------------------- 学生不能代单位确认

def test_student_cannot_confirm_for_unit(env):
    conn, db_path = env
    build_world(conn)
    app_id = svc.create_application(conn, db_path, ZHANG, 1, 1, T0, T1)
    svc.college_review(conn, db_path, WANG, app_id, True)
    # 学生试图代单位确认职责
    with pytest.raises(svc.BizError, match="学生不能代单位确认"):
        svc.unit_confirm(conn, db_path, ZHANG, app_id)
    # 其他单位的联系人也不行
    with pytest.raises(svc.BizError, match="本单位"):
        svc.unit_confirm(conn, db_path, WU, app_id)
    # 正确的单位联系人可以确认
    svc.unit_confirm(conn, db_path, LIU, app_id)
    assert conn.execute("SELECT status FROM applications WHERE id=?",
                        (app_id,)).fetchone()["status"] == "signing"


# ---------------------------------------------------------------- 并发审批

def _run_threads(fn, users):
    """每个 user 一个线程一个连接,栅栏同步起跑,收集异常。"""
    barrier = threading.Barrier(len(users))
    errors = []

    def worker(u):
        try:
            barrier.wait(timeout=10)
            fn(u)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(u,)) for u in users]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return errors


def test_concurrent_change_confirmation_single_effective(env):
    """两名审核人同时确认变更:旧版本只失效一次,不会重新生效。"""
    conn, db_path = env
    build_world(conn)

    for round_no in range(3):  # 多跑几轮,放大竞态窗口
        app_id, v1 = make_active_app(conn, db_path)
        v2 = svc.initiate_change(conn, db_path, LIU, app_id, "early_end",
                                 {"new_end_date": "2026-09-30", "reason": "单位项目收尾"},
                                 today="2026-09-10")

        def confirm(user):
            c = connect(db_path)
            try:
                svc.confirm_change(c, db_path, user, v2, today="2026-09-11")
            finally:
                c.close()

        # 学生与学院同时确认,双方都可能成为"最后一方"
        errors = _run_threads(confirm, [ZHANG, WANG])
        assert not errors, f"第{round_no}轮并发确认出现异常: {errors}"

        eff = conn.execute(
            "SELECT * FROM agreements WHERE application_id=? AND status='effective'",
            (app_id,)).fetchall()
        assert len(eff) == 1 and eff[0]["version"] == 2
        old = conn.execute("SELECT * FROM agreements WHERE id=?", (v1,)).fetchone()
        assert old["status"] == "superseded"
        # 生效事件只记录一次
        assert conn.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE application_id=?"
            " AND action='change_effective'", (app_id,)).fetchone()["c"] == 1
        # 台账连续
        assert svc.day_ledger(conn, app_id)["continuous"]


def test_concurrent_same_party_confirm_only_one_wins(env):
    """同一方重复并发确认:只有一次成功。"""
    conn, db_path = env
    build_world(conn)
    app_id, v1 = make_active_app(conn, db_path)
    v2 = svc.initiate_change(conn, db_path, LIU, app_id, "early_end",
                             {"new_end_date": "2026-09-30"}, today="2026-09-10")

    def confirm(user):
        c = connect(db_path)
        try:
            svc.confirm_change(c, db_path, user, v2, today="2026-09-11")
        finally:
            c.close()

    errors = _run_threads(confirm, [ZHANG] * 4)
    ok = 4 - len(errors)
    assert ok == 1  # 同一方只能确认一次
    assert all(isinstance(e, svc.BizError) for e in errors)


def test_concurrent_college_review(env):
    """两名同学院老师同时审核同一申请:只有一人成功。"""
    conn, db_path = env
    build_world(conn)
    app_id = svc.create_application(conn, db_path, ZHANG, 1, 1, T0, T1)

    def review(user):
        c = connect(db_path)
        try:
            svc.college_review(c, db_path, user, app_id, True, "同意")
        finally:
            c.close()

    errors = _run_threads(review, [WANG, LI])
    ok = 2 - len(errors)
    assert ok == 1
    assert any(isinstance(e, svc.ConcurrencyError) for e in errors)
    assert conn.execute("SELECT status FROM applications WHERE id=?",
                        (app_id,)).fetchone()["status"] == "pending_unit"


# ---------------------------------------------------------------- 天数连续可查

def test_day_ledger_continuous_after_early_end(env):
    conn, db_path = env
    build_world(conn)
    app_id, v1 = make_active_app(conn, db_path)
    v2 = svc.initiate_change(conn, db_path, ZHANG, app_id, "early_end",
                             {"new_end_date": "2026-09-30"}, today="2026-09-10")
    svc.confirm_change(conn, db_path, WANG, v2, today="2026-09-14")
    svc.confirm_change(conn, db_path, LIU, v2, today="2026-09-15")

    ledger = svc.day_ledger(conn, app_id)
    segs = [(s["start_date"], s["end_date"]) for s in ledger["segments"]]
    assert segs == [(T0, "2026-09-14"), ("2026-09-15", "2026-09-30")]
    assert ledger["continuous"]
    assert ledger["total_days"] == svc._days(T0, "2026-09-14") + svc._days("2026-09-15", "2026-09-30")


def test_withdrawal_without_replacement_ends_internship(env):
    conn, db_path = env
    build_world(conn)
    app_id, v1 = make_active_app(conn, db_path)
    v2 = svc.initiate_change(conn, db_path, LIU, app_id, "withdrawal",
                             {"reason": "岗位撤销"}, today="2026-08-01")
    svc.confirm_change(conn, db_path, ZHANG, v2, today="2026-08-02")
    svc.confirm_change(conn, db_path, WANG, v2, today="2026-08-03")

    app = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    assert app["status"] == "completed"
    ledger = svc.day_ledger(conn, app_id)
    assert ledger["continuous"]
    # 撤岗即日结束:只有一段,到 8 月 2 日(生效前一天)为止
    assert [(s["start_date"], s["end_date"]) for s in ledger["segments"]] == [
        (T0, "2026-08-02")]


# ---------------------------------------------------------------- 其他版本链事件

def test_contact_change_leaves_version_chain(env):
    conn, db_path = env
    build_world(conn)
    app_id, v1 = make_active_app(conn, db_path)
    v2 = svc.initiate_change(conn, db_path, LIU, app_id, "contact_change",
                             {"new_contact_id": 2, "reason": "刘洋离职,周凯接任"},
                             today="2026-08-10")
    svc.confirm_change(conn, db_path, ZHANG, v2, today="2026-08-10")
    svc.confirm_change(conn, db_path, WANG, v2, today="2026-08-11")

    ag = conn.execute("SELECT * FROM agreements WHERE id=?", (v2,)).fetchone()
    assert ag["status"] == "effective" and ag["unit_contact_id"] == 2
    # 旧联系人不再能代表单位
    assert svc._party_of_user(conn, LIU, ag) is None
    assert svc._party_of_user(conn, ZHOU, ag) == "unit"
    assert svc.day_ledger(conn, app_id)["continuous"]


def test_attachment_fix_bumps_attachment_version(env):
    conn, db_path = env
    build_world(conn)
    app_id, v1 = make_active_app(conn, db_path)
    v2 = svc.initiate_change(conn, db_path, WANG, app_id, "attachment_fix",
                             {"reason": "附件岗位职责与正文不一致,修订"}, today="2026-08-10")
    svc.confirm_change(conn, db_path, ZHANG, v2, today="2026-08-10")
    svc.confirm_change(conn, db_path, LIU, v2, today="2026-08-11")

    ag = conn.execute("SELECT * FROM agreements WHERE id=?", (v2,)).fetchone()
    assert ag["attachment_version"] == 2
    from app.db import dirs_for
    att = os.path.join(dirs_for(db_path)["attachments"], f"APP-{app_id}-att-v2.txt")
    assert os.path.exists(att)


# ---------------------------------------------------------------- 种子撤岗样例

def test_seed_withdrawal_sample(env):
    conn, db_path = env
    assert seed(conn, db_path)

    versions = conn.execute(
        "SELECT * FROM agreements WHERE application_id=1 ORDER BY version").fetchall()
    assert [v["change_type"] for v in versions] == ["initial", "withdrawal"]
    assert versions[0]["status"] == "superseded"
    assert versions[1]["status"] == "effective"
    assert versions[1]["prev_id"] == versions[0]["id"]

    ledger = svc.day_ledger(conn, 1)
    assert ledger["continuous"]
    assert [(s["start_date"], s["end_date"]) for s in ledger["segments"]] == [
        ("2026-07-01", "2026-09-16"), ("2026-09-17", "2026-11-30")]

    # 协议文件与附件都带版本号落盘
    from app.db import dirs_for
    d = dirs_for(db_path)
    assert os.path.exists(os.path.join(d["agreements"], "APP-1-v1.txt"))
    assert os.path.exists(os.path.join(d["agreements"], "APP-1-v2.txt"))
    assert os.path.exists(os.path.join(d["attachments"], "APP-1-att-v1.txt"))
