"""业务逻辑:岗位申请、三方签署、变更版本链、实习天数台账。

约定:
- user 是一个 dict:{"role": "student"|"teacher"|"contact", "id": <主键>}
- today 为 "YYYY-MM-DD" 字符串,默认取当天;测试与种子数据传入固定日期。
- 所有会改状态的写操作都在 BEGIN IMMEDIATE 事务里做 CAS 更新,
  两名审核人同时操作时只有一方能成功,旧版本不会被重新生效。
"""
import os
from datetime import date, datetime, timedelta

from .adapters import MockNotificationAdapter, MockSignatureAdapter
from .db import dirs_for, immediate_tx

SIGN_ORDER = ["student", "college", "unit"]  # 学生 → 学院 → 单位
PARTY_LABEL = {"student": "学生", "college": "学院", "unit": "单位"}
CHANGE_LABEL = {
    "initial": "初始签署",
    "withdrawal": "单位撤岗",
    "transfer": "学生转岗",
    "early_end": "提前结束",
    "contact_change": "联系人更换",
    "attachment_fix": "附件不一致修订",
}
APP_STATUS_LABEL = {
    "pending_college": "待学院审核",
    "pending_unit": "待单位确认",
    "signing": "三方签署中",
    "active": "实习中",
    "completed": "已结束",
    "rejected": "已驳回",
}
AGREEMENT_STATUS_LABEL = {
    "signing": "签署中",
    "change_pending": "变更待确认",
    "effective": "生效中",
    "superseded": "已被新版本取代",
}


class BizError(Exception):
    """业务规则拒绝(权限、状态、顺序等)。"""


class ConcurrencyError(BizError):
    """并发冲突:状态已被他人改变。"""


# ---------------------------------------------------------------- 基础助手

def _today():
    return date.today().isoformat()


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _days(start, end):
    """含头含尾的天数。"""
    return (date.fromisoformat(end) - date.fromisoformat(start)).days + 1


def _day_before(d):
    return (date.fromisoformat(d) - timedelta(days=1)).isoformat()


def _get(conn, sql, args=()):
    row = conn.execute(sql, args).fetchone()
    if row is None:
        raise BizError("记录不存在")
    return row


def _audit(conn, app_id, agreement_id, actor, action, detail=""):
    conn.execute(
        "INSERT INTO audit_log(application_id, agreement_id, actor, action, detail, created_at)"
        " VALUES(?,?,?,?,?,?)",
        (app_id, agreement_id, actor, action, detail, _now()),
    )


def _actor(conn, user):
    if user["role"] == "student":
        r = _get(conn, "SELECT * FROM students WHERE id=?", (user["id"],))
        return f"学生:{r['name']}"
    if user["role"] == "teacher":
        r = _get(conn, "SELECT * FROM teachers WHERE id=?", (user["id"],))
        return f"老师:{r['name']}"
    r = _get(conn, "SELECT * FROM unit_contacts WHERE id=?", (user["id"],))
    return f"单位联系人:{r['name']}"


def _user_key_for_party(conn, agreement, party):
    if party == "student":
        app = _get(conn, "SELECT * FROM applications WHERE id=?",
                   (agreement["application_id"],))
        return f"student:{app['student_id']}"
    if party == "college":
        return f"teacher:{agreement['teacher_id']}"
    return f"contact:{agreement['unit_contact_id']}"


def _notify_party(conn, notifier, agreement, party, message):
    notifier.notify(conn, _user_key_for_party(conn, agreement, party), message)


def _party_of_user(conn, user, agreement):
    """返回该用户对这份协议而言属于哪一方,无权返回 None。"""
    app = _get(conn, "SELECT * FROM applications WHERE id=?",
               (agreement["application_id"],))
    if user["role"] == "student" and app["student_id"] == user["id"]:
        return "student"
    if user["role"] == "teacher" and agreement["teacher_id"] == user["id"]:
        return "college"
    if user["role"] == "contact":
        contact = _get(conn, "SELECT * FROM unit_contacts WHERE id=?", (user["id"],))
        if agreement["unit_contact_id"] == user["id"] and contact["active"]:
            return "unit"
    return None


# ---------------------------------------------------------------- 文件落盘

def _write_attachment(db_path, app_id, att_version, position_title, start, end):
    path = os.path.join(dirs_for(db_path)["attachments"],
                        f"APP-{app_id}-att-v{att_version}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "实习协议附件(虚构文本,仅用于系统演示)\n"
            f"附件版本:第 {att_version} 版\n"
            f"对应申请:APP-{app_id}\n"
            f"岗位:{position_title}\n"
            f"实习周期:{start} 至 {end}\n"
            "内容:岗位职责、考勤要求、安全须知与保密条款清单。\n"
        )
    return path


def write_agreement_file(conn, db_path, agreement_id):
    """把协议某一版的完整正文(含签署记录)写到本地文件。"""
    ag = _get(conn, "SELECT * FROM agreements WHERE id=?", (agreement_id,))
    app = _get(conn, "SELECT * FROM applications WHERE id=?", (ag["application_id"],))
    stu = _get(conn, "SELECT * FROM students WHERE id=?", (app["student_id"],))
    col = _get(conn, "SELECT * FROM colleges WHERE id=?", (stu["college_id"],))
    tea = _get(conn, "SELECT * FROM teachers WHERE id=?", (ag["teacher_id"],))
    pos = _get(conn, "SELECT * FROM positions WHERE id=?", (ag["position_id"],))
    unit = _get(conn, "SELECT * FROM units WHERE id=?", (pos["unit_id"],))
    con = _get(conn, "SELECT * FROM unit_contacts WHERE id=?", (ag["unit_contact_id"],))
    sigs = conn.execute(
        "SELECT * FROM signatures WHERE agreement_id=? ORDER BY seq", (agreement_id,)
    ).fetchall()

    lines = [
        "实 习 三 方 协 议(虚构文本,仅用于系统演示)",
        "=" * 46,
        f"协议编号:APP-{app['id']}-V{ag['version']}",
        f"版本:第 {ag['version']} 版({CHANGE_LABEL[ag['change_type']]})",
        f"状态:{AGREEMENT_STATUS_LABEL[ag['status']]}",
        "",
        f"甲方(学生):{stu['name']}(学号 {stu['student_no']},{col['name']})",
        f"乙方(学院):{col['name']}  指导老师:{tea['name']}",
        f"丙方(单位):{unit['name']}  联系人:{con['name']}",
        "",
        f"实习岗位:{pos['title']}",
        f"岗位职责:{pos['description']}",
        f"实习周期:{ag['start_date']} 至 {ag['end_date']}",
        f"协议附件:第 {ag['attachment_version']} 版"
        f"(attachments/APP-{app['id']}-att-v{ag['attachment_version']}.txt)",
    ]
    if ag["change_reason"]:
        lines += ["", f"变更原因:{ag['change_reason']}"]
    lines += ["", "签署记录:"]
    if sigs:
        for s in sigs:
            if s["status"] == "signed":
                lines.append(f"  {PARTY_LABEL[s['party']]} {s['signer_name']} "
                             f"已签 {s['token']} @ {s['signed_at']}")
            else:
                lines.append(f"  {PARTY_LABEL[s['party']]} 待签署")
    else:
        lines.append("  (变更版本,以受影响方确认记录为准)")
    confs = conn.execute(
        "SELECT * FROM confirmations WHERE agreement_id=?", (agreement_id,)
    ).fetchall()
    if confs:
        lines.append("变更确认:")
        for c in confs:
            mark = f"已确认 @ {c['confirmed_at']}" if c["status"] == "confirmed" else "待确认"
            lines.append(f"  {PARTY_LABEL[c['party']]} {mark}")
    lines += ["", "本文件由模拟签名适配器生成,不构成真实法律协议。"]

    path = os.path.join(dirs_for(db_path)["agreements"],
                        f"APP-{app['id']}-v{ag['version']}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    conn.execute("UPDATE agreements SET file_path=? WHERE id=?",
                 (os.path.relpath(path, dirs_for(db_path)["base"]), agreement_id))
    return path


# ---------------------------------------------------------------- 1. 岗位申请

def create_application(conn, db_path, user, position_id, teacher_id,
                       start_date, end_date, today=None):
    today = today or _today()
    if user["role"] != "student":
        raise BizError("只有学生可以提交岗位申请")
    stu = _get(conn, "SELECT * FROM students WHERE id=?", (user["id"],))
    pos = _get(conn, "SELECT * FROM positions WHERE id=?", (position_id,))
    tea = _get(conn, "SELECT * FROM teachers WHERE id=?", (teacher_id,))
    if pos["status"] != "open":
        raise BizError("该岗位已撤岗,不能申请")
    if tea["college_id"] != stu["college_id"]:
        raise BizError("指导老师必须来自学生所在学院")
    if start_date > end_date:
        raise BizError("开始日期不能晚于结束日期")

    cur = conn.execute(
        "INSERT INTO applications(student_id, position_id, teacher_id, start_date,"
        " end_date, status, created_at) VALUES(?,?,?,?,?,'pending_college',?)",
        (user["id"], position_id, teacher_id, start_date, end_date, _now()),
    )
    app_id = cur.lastrowid
    _audit(conn, app_id, None, _actor(conn, user), "submit_application",
           f"岗位:{pos['title']} 周期:{start_date}~{end_date}")
    MockNotificationAdapter(db_path).notify(
        conn, f"teacher:{teacher_id}",
        f"学生 {stu['name']} 提交了岗位申请(APP-{app_id}),请审核培养要求。")
    conn.commit()
    return app_id


# ---------------------------------------------------------------- 2. 学院审核

def college_review(conn, db_path, user, app_id, approve, note="", today=None):
    today = today or _today()
    if user["role"] != "teacher":
        raise BizError("只有指导老师可以审核申请")
    tea = _get(conn, "SELECT * FROM teachers WHERE id=?", (user["id"],))
    app = _get(conn, "SELECT * FROM applications WHERE id=?", (app_id,))
    stu = _get(conn, "SELECT * FROM students WHERE id=?", (app["student_id"],))
    if tea["college_id"] != stu["college_id"]:
        _audit(conn, app_id, None, _actor(conn, user), "review_rejected_cross_college",
               "老师试图审核其他学院的申请")
        conn.commit()
        raise BizError("老师只能审核本学院的申请")
    if app["status"] != "pending_college":
        raise BizError("该申请不在待学院审核状态")

    new_status = "pending_unit" if approve else "rejected"
    # CAS:两名老师同时审核同一申请,只有一人成功
    cur = conn.execute(
        "UPDATE applications SET status=?, review_note=? WHERE id=? AND status='pending_college'",
        (new_status, note, app_id))
    if cur.rowcount != 1:
        raise ConcurrencyError("该申请已被其他审核人处理")
    _audit(conn, app_id, None, _actor(conn, user),
           "college_approve" if approve else "college_reject", note)

    notifier = MockNotificationAdapter(db_path)
    if approve:
        pos = _get(conn, "SELECT * FROM positions WHERE id=?", (app["position_id"],))
        for c in conn.execute(
                "SELECT * FROM unit_contacts WHERE unit_id=? AND active=1",
                (pos["unit_id"],)):
            notifier.notify(conn, f"contact:{c['id']}",
                            f"申请 APP-{app_id} 已通过学院审核,请确认岗位职责。")
    else:
        notifier.notify(conn, f"student:{app['student_id']}",
                        f"你的岗位申请(APP-{app_id})被学院驳回:{note}")
    conn.commit()


# ---------------------------------------------------------------- 3. 单位确认 → 生成协议 v1

def unit_confirm(conn, db_path, user, app_id, today=None):
    today = today or _today()
    if user["role"] != "contact":
        # 学生不能代单位确认
        _audit(conn, app_id, None, _actor(conn, user), "unit_confirm_forbidden",
               "非单位联系人尝试确认职责")
        conn.commit()
        raise BizError("只有单位联系人可以确认岗位职责,学生不能代单位确认")
    contact = _get(conn, "SELECT * FROM unit_contacts WHERE id=?", (user["id"],))
    if not contact["active"]:
        raise BizError("该联系人已停用")
    app = _get(conn, "SELECT * FROM applications WHERE id=?", (app_id,))
    pos = _get(conn, "SELECT * FROM positions WHERE id=?", (app["position_id"],))
    if contact["unit_id"] != pos["unit_id"]:
        raise BizError("只能确认本单位岗位的申请")
    if app["status"] != "pending_unit":
        raise BizError("该申请不在待单位确认状态")

    cur = conn.execute(
        "UPDATE applications SET status='signing' WHERE id=? AND status='pending_unit'",
        (app_id,))
    if cur.rowcount != 1:
        raise ConcurrencyError("该申请已被处理")

    att_path = _write_attachment(db_path, app_id, 1, pos["title"],
                                 app["start_date"], app["end_date"])
    cur = conn.execute(
        "INSERT INTO agreements(application_id, version, prev_id, change_type, initiator,"
        " position_id, teacher_id, unit_contact_id, start_date, end_date,"
        " attachment_version, status, created_at)"
        " VALUES(?,1,NULL,'initial','',?,?,?,?,?,1,'signing',?)",
        (app_id, app["position_id"], app["teacher_id"], user["id"],
         app["start_date"], app["end_date"], _now()),
    )
    ag_id = cur.lastrowid
    for seq, party in enumerate(SIGN_ORDER, start=1):
        conn.execute(
            "INSERT INTO signatures(agreement_id, party, seq) VALUES(?,?,?)",
            (ag_id, party, seq))
    _audit(conn, app_id, ag_id, _actor(conn, user), "unit_confirm",
           f"生成协议第 1 版,附件:{os.path.basename(att_path)}")

    ag = _get(conn, "SELECT * FROM agreements WHERE id=?", (ag_id,))
    notifier = MockNotificationAdapter(db_path)
    for party in SIGN_ORDER:
        _notify_party(conn, notifier, ag, party,
                      f"协议 APP-{app_id}-V1 已生成,请按 学生→学院→单位 顺序签署。")
    write_agreement_file(conn, db_path, ag_id)
    conn.commit()
    return ag_id


# ---------------------------------------------------------------- 4. 三方签署

def _signer_name(conn, agreement, party):
    app = _get(conn, "SELECT * FROM applications WHERE id=?",
               (agreement["application_id"],))
    if party == "student":
        return _get(conn, "SELECT * FROM students WHERE id=?",
                    (app["student_id"],))["name"]
    if party == "college":
        return _get(conn, "SELECT * FROM teachers WHERE id=?",
                    (agreement["teacher_id"],))["name"]
    return _get(conn, "SELECT * FROM unit_contacts WHERE id=?",
                (agreement["unit_contact_id"],))["name"]


def sign(conn, db_path, user, agreement_id, today=None):
    """按 学生→学院→单位 顺序签署;任何越序/越权都会拒绝并留痕。"""
    today = today or _today()
    agreement = _get(conn, "SELECT * FROM agreements WHERE id=?", (agreement_id,))
    party = _party_of_user(conn, user, agreement)
    actor = _actor(conn, user)
    if party is None:
        _audit(conn, agreement["application_id"], agreement_id, actor,
               "sign_forbidden", "用户无权代表该协议任何一方签署")
        conn.commit()
        raise BizError("你不能代表该协议的任何一方签署")
    if agreement["status"] != "signing":
        _audit(conn, agreement["application_id"], agreement_id, actor,
               "sign_rejected_stale",
               f"协议状态为 {agreement['status']},不可签署(旧版本不可重新生效)")
        conn.commit()
        raise BizError("该版本不在签署中状态(可能已被新版本取代),不能签署")

    row = _get(conn,
               "SELECT * FROM signatures WHERE agreement_id=? AND party=?",
               (agreement_id, party))
    if row["status"] == "signed":
        raise BizError("你这一方已经签署过了")
    prev_pending = conn.execute(
        "SELECT COUNT(*) c FROM signatures WHERE agreement_id=? AND seq<? AND status!='signed'",
        (agreement_id, row["seq"])).fetchone()["c"]
    if prev_pending:
        _audit(conn, agreement["application_id"], agreement_id, actor,
               "sign_order_rejected",
               f"{PARTY_LABEL[party]} 试图在前序方未签署时签署(顺序错误)")
        conn.commit()
        raise BizError("签署顺序错误:必须按 学生→学院→单位 顺序签署")

    signer = _signer_name(conn, agreement, party)
    token = MockSignatureAdapter(db_path).sign(agreement_id, party, signer, _now())

    with immediate_tx(conn):
        cur = conn.execute(
            "UPDATE signatures SET status='signed', signer_name=?, token=?, signed_at=?"
            " WHERE id=? AND status='pending'",
            (signer, token, _now(), row["id"]))
        if cur.rowcount != 1:
            raise ConcurrencyError("该签署记录已被更新,请刷新")
        _audit(conn, agreement["application_id"], agreement_id, actor,
               f"sign_{party}", token)

        remaining = conn.execute(
            "SELECT COUNT(*) c FROM signatures WHERE agreement_id=? AND status!='signed'",
            (agreement_id,)).fetchone()["c"]
        if remaining == 0:
            # 三方签齐 → 生效;CAS 保证并发下只有一个版本能生效
            cur = conn.execute(
                "UPDATE agreements SET status='effective', effective_at=?"
                " WHERE id=? AND status='signing'", (_now(), agreement_id))
            if cur.rowcount != 1:
                raise ConcurrencyError("协议状态已变化,生效失败")
            conn.execute(
                "UPDATE applications SET status='active' WHERE id=?",
                (agreement["application_id"],))
            conn.execute(
                "INSERT INTO segments(application_id, agreement_id, start_date, end_date, days)"
                " VALUES(?,?,?,?,?)",
                (agreement["application_id"], agreement_id,
                 agreement["start_date"], agreement["end_date"],
                 _days(agreement["start_date"], agreement["end_date"])))
            _audit(conn, agreement["application_id"], agreement_id, actor,
                   "agreement_effective", "三方签齐,协议生效")

    write_agreement_file(conn, db_path, agreement_id)
    conn.commit()
    notifier = MockNotificationAdapter(db_path)
    ag = _get(conn, "SELECT * FROM agreements WHERE id=?", (agreement_id,))
    if ag["status"] == "effective":
        for p in SIGN_ORDER:
            _notify_party(conn, notifier, ag, p,
                          f"协议 APP-{ag['application_id']}-V{ag['version']} 三方签齐,已生效。")
    else:
        nxt = conn.execute(
            "SELECT party FROM signatures WHERE agreement_id=? AND status='pending'"
            " ORDER BY seq LIMIT 1", (agreement_id,)).fetchone()
        if nxt:
            _notify_party(conn, notifier, ag, nxt["party"],
                          f"协议 APP-{ag['application_id']}-V{ag['version']} 等待你方签署。")
    conn.commit()


# ---------------------------------------------------------------- 5. 变更(版本链)

def _current_effective(conn, app_id):
    return conn.execute(
        "SELECT * FROM agreements WHERE application_id=? AND status='effective'",
        (app_id,)).fetchone()


def initiate_change(conn, db_path, user, app_id, change_type, params, today=None):
    """对已生效协议发起变更,生成新版本(change_pending),受影响方需重新确认。

    params: reason, new_position_id, new_end_date, new_contact_id
    """
    today = today or _today()
    if change_type not in CHANGE_LABEL or change_type == "initial":
        raise BizError("未知的变更类型")
    app = _get(conn, "SELECT * FROM applications WHERE id=?", (app_id,))
    old = _current_effective(conn, app_id)
    if old is None:
        raise BizError("当前没有生效中的协议,不能发起变更")
    actor = _actor(conn, user)
    initiator = _party_of_user(conn, user, old)
    if initiator is None:
        raise BizError("你不是该协议的当事方,不能发起变更")
    if change_type == "withdrawal" and initiator != "unit":
        raise BizError("撤岗只能由单位发起")
    if change_type == "transfer" and initiator != "student":
        raise BizError("转岗只能由学生发起")
    if change_type == "contact_change" and initiator != "unit":
        raise BizError("联系人更换只能由单位发起")

    reason = (params.get("reason") or "").strip()
    new_position_id = old["position_id"]
    new_end = old["end_date"]
    new_contact = old["unit_contact_id"]
    new_att_ver = old["attachment_version"]

    if change_type in ("withdrawal", "transfer"):
        new_position_id = params.get("new_position_id") or old["position_id"]
        pos = _get(conn, "SELECT * FROM positions WHERE id=?", (new_position_id,))
        old_pos = _get(conn, "SELECT * FROM positions WHERE id=?", (old["position_id"],))
        if pos["unit_id"] != old_pos["unit_id"]:
            raise BizError("新岗位必须属于同一实习单位")
        if change_type == "withdrawal":
            # 撤掉原岗位
            conn.execute("UPDATE positions SET status='withdrawn' WHERE id=?",
                         (old["position_id"],))
        if params.get("new_end_date"):
            new_end = params["new_end_date"]
        elif change_type == "withdrawal" and new_position_id == old["position_id"]:
            # 撤岗且未安排新岗位:实习即日结束
            new_end = today
    elif change_type == "early_end":
        new_end = params.get("new_end_date") or ""
        if not new_end or new_end >= old["end_date"]:
            raise BizError("提前结束的日期必须早于原结束日期")
        if new_end < today:
            raise BizError("提前结束的日期不能早于今天")
    elif change_type == "contact_change":
        new_contact = params.get("new_contact_id")
        nc = _get(conn, "SELECT * FROM unit_contacts WHERE id=?", (new_contact,))
        old_pos = _get(conn, "SELECT * FROM positions WHERE id=?", (old["position_id"],))
        if nc["unit_id"] != old_pos["unit_id"] or not nc["active"]:
            raise BizError("新联系人必须是该单位的在用联系人")
    elif change_type == "attachment_fix":
        new_att_ver = old["attachment_version"] + 1

    version = conn.execute(
        "SELECT COALESCE(MAX(version),0)+1 v FROM agreements WHERE application_id=?",
        (app_id,)).fetchone()["v"]
    cur = conn.execute(
        "INSERT INTO agreements(application_id, version, prev_id, change_type, change_reason,"
        " initiator, position_id, teacher_id, unit_contact_id, start_date, end_date,"
        " attachment_version, status, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'change_pending',?)",
        (app_id, version, old["id"], change_type, reason, initiator,
         new_position_id, old["teacher_id"], new_contact,
         old["start_date"], new_end, new_att_ver, _now()),
    )
    new_id = cur.lastrowid

    affected = [p for p in SIGN_ORDER if p != initiator]
    for p in affected:
        conn.execute(
            "INSERT INTO confirmations(agreement_id, party) VALUES(?,?)", (new_id, p))

    if change_type == "attachment_fix":
        pos = _get(conn, "SELECT * FROM positions WHERE id=?", (new_position_id,))
        _write_attachment(db_path, app_id, new_att_ver, pos["title"], today, new_end)

    _audit(conn, app_id, new_id, actor, f"change_initiated_{change_type}",
           f"第 {version} 版,受影响方:{'、'.join(PARTY_LABEL[p] for p in affected)};{reason}")

    new_ag = _get(conn, "SELECT * FROM agreements WHERE id=?", (new_id,))
    notifier = MockNotificationAdapter(db_path)
    for p in affected:
        _notify_party(conn, notifier, new_ag, p,
                      f"协议 APP-{app_id} 发起变更({CHANGE_LABEL[change_type]}),"
                      f"第 {version} 版待你方确认。")
    write_agreement_file(conn, db_path, new_id)
    conn.commit()
    return new_id


def confirm_change(conn, db_path, user, agreement_id, today=None):
    """受影响方确认变更;最后一方确认时原子地完成新旧版本切换与台账结转。"""
    today = today or _today()
    agreement = _get(conn, "SELECT * FROM agreements WHERE id=?", (agreement_id,))
    if agreement["status"] != "change_pending":
        raise BizError("该版本不在变更待确认状态")
    party = _party_of_user(conn, user, agreement)
    if party is None:
        raise BizError("你不是该变更的受影响方")
    actor = _actor(conn, user)

    with immediate_tx(conn):
        cur = conn.execute(
            "UPDATE confirmations SET status='confirmed', confirmed_at=?"
            " WHERE agreement_id=? AND party=? AND status='pending'",
            (_now(), agreement_id, party))
        if cur.rowcount != 1:
            raise BizError("你这一方无需确认或已确认过")
        _audit(conn, agreement["application_id"], agreement_id, actor,
               f"change_confirmed_{party}", "")

        remaining = conn.execute(
            "SELECT COUNT(*) c FROM confirmations WHERE agreement_id=? AND status!='confirmed'",
            (agreement_id,)).fetchone()["c"]
        old_id = None
        if remaining == 0:
            old_id = _finalize_change(conn, agreement, today)

    write_agreement_file(conn, db_path, agreement_id)
    if old_id is not None:
        # 旧版本文件同步重写,状态从"生效中"变为"已被新版本取代"
        write_agreement_file(conn, db_path, old_id)
    conn.commit()
    ag = _get(conn, "SELECT * FROM agreements WHERE id=?", (agreement_id,))
    if ag["status"] == "effective":
        notifier = MockNotificationAdapter(db_path)
        for p in SIGN_ORDER:
            _notify_party(conn, notifier, ag, p,
                          f"协议 APP-{ag['application_id']}-V{ag['version']}"
                          f"({CHANGE_LABEL[ag['change_type']]})已生效,旧版本同时失效。")
        conn.commit()


def _finalize_change(conn, agreement, today):
    """在事务内完成版本切换:CAS 保证旧版本只失效一次、新版本只生效一次。"""
    app_id = agreement["application_id"]
    old = _current_effective(conn, app_id)
    if old is None:
        raise ConcurrencyError("旧版本已失效,变更无法生效")
    cur = conn.execute(
        "UPDATE agreements SET status='superseded' WHERE id=? AND status='effective'",
        (old["id"],))
    if cur.rowcount != 1:
        raise ConcurrencyError("旧版本状态已变化,拒绝重复失效(并发冲突)")
    cur = conn.execute(
        "UPDATE agreements SET status='effective', effective_at=?, start_date=?"
        " WHERE id=? AND status='change_pending'",
        (_now(), today, agreement["id"]))
    if cur.rowcount != 1:
        raise ConcurrencyError("新版本状态已变化,生效失败(并发冲突)")

    # 实习天数台账:旧段在变更生效前一天截止,新段从当天接续,保证连续可查
    open_seg = conn.execute(
        "SELECT * FROM segments WHERE application_id=? ORDER BY id DESC LIMIT 1",
        (app_id,)).fetchone()
    if open_seg is not None:
        close_end = _day_before(today)
        if close_end >= open_seg["start_date"]:
            conn.execute(
                "UPDATE segments SET end_date=?, days=? WHERE id=?",
                (close_end, _days(open_seg["start_date"], close_end), open_seg["id"]))
        else:
            conn.execute("DELETE FROM segments WHERE id=?", (open_seg["id"],))

    terminal = (agreement["change_type"] == "withdrawal"
                and agreement["position_id"] == old["position_id"]
                and agreement["end_date"] <= today)
    if not terminal:
        conn.execute(
            "INSERT INTO segments(application_id, agreement_id, start_date, end_date, days)"
            " VALUES(?,?,?,?,?)",
            (app_id, agreement["id"], today, agreement["end_date"],
             _days(today, agreement["end_date"])))
    if terminal or (agreement["change_type"] == "early_end"
                    and agreement["end_date"] <= today):
        conn.execute("UPDATE applications SET status='completed' WHERE id=?", (app_id,))

    _audit(conn, app_id, agreement["id"], "system", "change_effective",
           f"第 {agreement['version']} 版生效,第 {old['version']} 版失效")
    return old["id"]


# ---------------------------------------------------------------- 查询

def list_progress(conn):
    """进度页:每条申请的三方签署状态。"""
    apps = conn.execute(
        "SELECT a.*, s.name student_name, s.student_no, c.name college_name,"
        " p.title position_title, u.name unit_name, t.name teacher_name"
        " FROM applications a"
        " JOIN students s ON s.id=a.student_id"
        " JOIN colleges c ON c.id=s.college_id"
        " JOIN positions p ON p.id=a.position_id"
        " JOIN units u ON u.id=p.unit_id"
        " JOIN teachers t ON t.id=a.teacher_id"
        " ORDER BY a.id DESC").fetchall()
    out = []
    for app in apps:
        ag = conn.execute(
            "SELECT * FROM agreements WHERE application_id=? ORDER BY version DESC LIMIT 1",
            (app["id"],)).fetchone()
        sigs = {}
        confs = {}
        if ag:
            for s in conn.execute(
                    "SELECT * FROM signatures WHERE agreement_id=?", (ag["id"],)):
                sigs[s["party"]] = s["status"]
            for cf in conn.execute(
                    "SELECT * FROM confirmations WHERE agreement_id=?", (ag["id"],)):
                confs[cf["party"]] = cf["status"]
        out.append({"app": app, "agreement": ag, "sigs": sigs, "confs": confs})
    return out


def get_detail(conn, app_id):
    app = _get(conn,
               "SELECT a.*, s.name student_name, s.student_no, c.name college_name,"
               " s.college_id student_college_id"
               " FROM applications a JOIN students s ON s.id=a.student_id"
               " JOIN colleges c ON c.id=s.college_id WHERE a.id=?", (app_id,))
    versions = conn.execute(
        "SELECT * FROM agreements WHERE application_id=? ORDER BY version", (app_id,)).fetchall()
    vdata = []
    for v in versions:
        sigs = conn.execute(
            "SELECT * FROM signatures WHERE agreement_id=? ORDER BY seq", (v["id"],)).fetchall()
        confs = conn.execute(
            "SELECT * FROM confirmations WHERE agreement_id=?", (v["id"],)).fetchall()
        vdata.append({"agreement": v, "sigs": sigs, "confs": confs})
    return {"app": app, "versions": vdata, "ledger": day_ledger(conn, app_id)}


def day_ledger(conn, app_id):
    """实习天数台账:分段 + 累计 + 连续性校验。"""
    segs = conn.execute(
        "SELECT s.*, a.version FROM segments s JOIN agreements a ON a.id=s.agreement_id"
        " WHERE s.application_id=? ORDER BY s.start_date, s.id", (app_id,)).fetchall()
    total = sum(s["days"] for s in segs)
    continuous = True
    for prev, nxt in zip(segs, segs[1:]):
        expect = (date.fromisoformat(prev["end_date"]) + timedelta(days=1)).isoformat()
        if nxt["start_date"] != expect:
            continuous = False
    return {"segments": segs, "total_days": total, "continuous": continuous}


def compare_versions(conn, app_id, old_id, new_id):
    """变更对比:并排列出两个版本的差异。"""
    old = _get(conn, "SELECT * FROM agreements WHERE id=?", (old_id,))
    new = _get(conn, "SELECT * FROM agreements WHERE id=?", (new_id,))

    def row_view(ag):
        pos = _get(conn, "SELECT * FROM positions WHERE id=?", (ag["position_id"],))
        unit = _get(conn, "SELECT * FROM units WHERE id=?", (pos["unit_id"],))
        tea = _get(conn, "SELECT * FROM teachers WHERE id=?", (ag["teacher_id"],))
        con = _get(conn, "SELECT * FROM unit_contacts WHERE id=?", (ag["unit_contact_id"],))
        return {
            "岗位": pos["title"], "单位": unit["name"], "指导老师": tea["name"],
            "单位联系人": con["name"], "开始日期": ag["start_date"],
            "结束日期": ag["end_date"], "附件版本": f"第 {ag['attachment_version']} 版",
            "协议状态": AGREEMENT_STATUS_LABEL[ag["status"]],
        }

    o, n = row_view(old), row_view(new)
    fields = ["岗位", "单位", "指导老师", "单位联系人", "开始日期", "结束日期",
              "附件版本", "协议状态"]
    rows = [{"field": f, "old": o[f], "new": n[f], "changed": o[f] != n[f]} for f in fields]
    return {"old": old, "new": new, "rows": rows}


def teacher_todo(conn, teacher_id):
    tea = _get(conn, "SELECT * FROM teachers WHERE id=?", (teacher_id,))
    reviews = conn.execute(
        "SELECT a.*, s.name student_name, p.title position_title FROM applications a"
        " JOIN students s ON s.id=a.student_id"
        " JOIN positions p ON p.id=a.position_id"
        " WHERE a.status='pending_college' AND s.college_id=?"
        " ORDER BY a.id", (tea["college_id"],)).fetchall()
    signs = conn.execute(
        "SELECT ag.*, a.student_id, s.name student_name FROM agreements ag"
        " JOIN applications a ON a.id=ag.application_id"
        " JOIN students s ON s.id=a.student_id"
        " WHERE ag.status='signing' AND ag.teacher_id=?"
        " AND EXISTS (SELECT 1 FROM signatures g WHERE g.agreement_id=ag.id"
        "             AND g.party='college' AND g.status='pending')"
        " ORDER BY ag.id", (teacher_id,)).fetchall()
    confirms = conn.execute(
        "SELECT ag.*, s.name student_name FROM agreements ag"
        " JOIN applications a ON a.id=ag.application_id"
        " JOIN students s ON s.id=a.student_id"
        " WHERE ag.status='change_pending' AND ag.teacher_id=?"
        " AND EXISTS (SELECT 1 FROM confirmations c WHERE c.agreement_id=ag.id"
        "             AND c.party='college' AND c.status='pending')"
        " ORDER BY ag.id", (teacher_id,)).fetchall()
    return {"teacher": tea, "reviews": reviews, "signs": signs, "confirms": confirms}


def archive(conn):
    """协议归档:按申请分组的全部版本。"""
    apps = conn.execute(
        "SELECT a.id, s.name student_name, p.title position_title, u.name unit_name"
        " FROM applications a JOIN students s ON s.id=a.student_id"
        " JOIN positions p ON p.id=a.position_id JOIN units u ON u.id=p.unit_id"
        " ORDER BY a.id").fetchall()
    out = []
    for app in apps:
        versions = conn.execute(
            "SELECT * FROM agreements WHERE application_id=? ORDER BY version",
            (app["id"],)).fetchall()
        if versions:
            out.append({"app": app, "versions": versions})
    return out


def audit_trail(conn, app_id):
    return conn.execute(
        "SELECT * FROM audit_log WHERE application_id=? ORDER BY id", (app_id,)).fetchall()


def list_users(conn):
    return {
        "students": conn.execute(
            "SELECT s.*, c.name college_name FROM students s"
            " JOIN colleges c ON c.id=s.college_id ORDER BY s.id").fetchall(),
        "teachers": conn.execute(
            "SELECT t.*, c.name college_name FROM teachers t"
            " JOIN colleges c ON c.id=t.college_id ORDER BY t.id").fetchall(),
        "contacts": conn.execute(
            "SELECT uc.*, u.name unit_name FROM unit_contacts uc"
            " JOIN units u ON u.id=uc.unit_id ORDER BY uc.id").fetchall(),
    }


def open_positions(conn):
    return conn.execute(
        "SELECT p.*, u.name unit_name FROM positions p JOIN units u ON u.id=p.unit_id"
        " WHERE p.status='open' ORDER BY p.id").fetchall()


def teachers_of_college(conn, college_id):
    return conn.execute(
        "SELECT * FROM teachers WHERE college_id=? ORDER BY id", (college_id,)).fetchall()
