"""业务服务层：岗位申请、三方签署、变更（版本链）、实习天数。

规则落点：
- 签署顺序固定为 学生 -> 学院 -> 单位；乱序拒绝并记录 order_error 事件。
- 学生不能代单位确认、老师只能审核本学院申请：越权一律 403 并记录 unauthorized 事件。
- 所有状态跃迁用条件 UPDATE（WHERE status=旧值），配合唯一部分索引
  one_effective_per_application，两名审核人同时操作也不会让旧版本重新生效。
- 变更生效时截断/续接 day_segments，变更前后实习天数连续可查。
"""
import functools
import json
import os
from datetime import date, datetime, timedelta

import adapters
import db

SIGN_ORDER = ["student", "college", "company"]
PARTY_NAMES = {"student": "学生", "college": "学院", "company": "单位"}
APP_ACTIVE = ("submitted", "college_approved", "signing", "effective")
CHANGE_TYPES = ("withdraw", "transfer", "early_end", "contact_change", "attachment_fix")
CHANGE_TYPE_NAMES = {
    "withdraw": "单位撤岗",
    "transfer": "学生转岗",
    "early_end": "实习提前结束",
    "contact_change": "单位联系人更换",
    "attachment_fix": "协议附件不一致",
}


class ServiceError(Exception):
    def __init__(self, status, message, audit=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.audit = audit  # 拒绝也要留痕：(app_id, agreement_id, kind, detail, actor)


def audited(fn):
    """被拒绝的操作（乱序签署、越权）在事务回滚后，仍把事件写入版本链。"""
    @functools.wraps(fn)
    def wrapper(*args, **kw):
        try:
            return fn(*args, **kw)
        except ServiceError as e:
            if e.audit:
                app_id, agreement_id, kind, detail, actor = e.audit
                with db.tx() as conn:
                    _event(conn, app_id, agreement_id, kind, detail, actor)
            raise
    return wrapper


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _date(s, field):
    try:
        return date.fromisoformat(str(s))
    except (ValueError, TypeError):
        raise ServiceError(400, f"{field} 不是合法日期（YYYY-MM-DD）：{s!r}")


def _days(start, end):
    return (end - start).days + 1


# ---------------------------------------------------------------- 基础装载

def _one(conn, sql, params, what):
    row = conn.execute(sql, params).fetchone()
    if not row:
        raise ServiceError(404, f"{what}不存在")
    return row


def load_application(conn, app_id):
    return _one(
        conn,
        """SELECT a.*, s.name AS student_name, s.college_id AS student_college_id,
                  c.name AS college_name, p.company_id, p.title AS position_title
           FROM applications a
           JOIN students s ON s.id = a.student_id
           JOIN colleges c ON c.id = s.college_id
           JOIN positions p ON p.id = a.position_id
           WHERE a.id=?""",
        (app_id,),
        "岗位申请",
    )


def load_actor(conn, actor):
    """解析操作者身份，返回 (role, row, 展示名)。"""
    if not isinstance(actor, dict):
        raise ServiceError(400, "缺少操作者身份 actor")
    role, aid = actor.get("role"), actor.get("id")
    if role == "student":
        row = _one(conn, "SELECT s.*, c.name AS college_name FROM students s JOIN colleges c ON c.id=s.college_id WHERE s.id=?", (aid,), "学生")
        return role, row, f"学生 {row['name']}"
    if role == "teacher":
        row = _one(conn, "SELECT t.*, c.name AS college_name FROM teachers t JOIN colleges c ON c.id=t.college_id WHERE t.id=?", (aid,), "老师")
        return role, row, f"老师 {row['name']}（{row['college_name']}）"
    if role == "contact":
        row = _one(conn, "SELECT ct.*, cp.name AS company_name FROM contacts ct JOIN companies cp ON cp.id=ct.company_id WHERE ct.id=?", (aid,), "单位联系人")
        return role, row, f"单位联系人 {row['name']}（{row['company_name']}）"
    raise ServiceError(400, f"未知操作者角色：{role!r}")


def _event(conn, app_id, agreement_id, kind, detail, actor_label=""):
    conn.execute(
        "INSERT INTO events(application_id, agreement_id, kind, detail, actor, created_at) VALUES (?,?,?,?,?,?)",
        (app_id, agreement_id, kind, detail, actor_label, now()),
    )


def _authorize_party(conn, app, party, actor, change=None):
    """校验 actor 是否有权以 party 身份操作，返回签署人展示名。越权记录事件并 403。"""
    role, row, label = load_actor(conn, actor)
    ok = False
    if party == "student":
        ok = role == "student" and row["id"] == app["student_id"]
    elif party == "college":
        ok = role == "teacher" and row["college_id"] == app["student_college_id"]
    elif party == "company":
        company_id = app["company_id"]
        if change is not None and change["type"] == "transfer":
            payload = json.loads(change["payload"])
            pos = _one(conn, "SELECT * FROM positions WHERE id=?", (payload["new_position_id"],), "新岗位")
            company_id = pos["company_id"]
        ok = role == "contact" and row["active"] == 1 and row["company_id"] == company_id
    else:
        raise ServiceError(400, f"未知签署方：{party!r}")
    if not ok:
        detail = f"{label} 试图以「{PARTY_NAMES[party]}」身份操作申请 #{app['id']}，被拒绝"
        raise ServiceError(403, f"{label} 无权以「{PARTY_NAMES[party]}」身份操作（学生不能代单位确认，老师只能处理本学院申请）",
                           audit=(app["id"], None, "unauthorized", detail, label))
    return label


# ---------------------------------------------------------------- 岗位申请

def create_application(actor, student_id, position_id, teacher_id, start_date, end_date):
    with db.tx() as conn:
        role, student, slabel = load_actor(conn, actor)
        if role != "student" or student["id"] != student_id:
            raise ServiceError(403, "只能由学生本人提交岗位申请")
        position = _one(conn, "SELECT * FROM positions WHERE id=?", (position_id,), "岗位")
        if position["status"] != "open":
            raise ServiceError(409, f"岗位「{position['title']}」已撤岗，不能申请")
        teacher = _one(conn, "SELECT * FROM teachers WHERE id=?", (teacher_id,), "指导老师")
        if teacher["college_id"] != student["college_id"]:
            raise ServiceError(400, "指导老师必须来自学生所在学院")
        d1, d2 = _date(start_date, "开始日期"), _date(end_date, "结束日期")
        if d1 >= d2:
            raise ServiceError(400, "结束日期必须晚于开始日期")
        dup = conn.execute(
            f"SELECT id FROM applications WHERE student_id=? AND status IN ({','.join('?' * len(APP_ACTIVE))})",
            (student_id, *APP_ACTIVE),
        ).fetchone()
        if dup:
            raise ServiceError(409, f"该学生已有进行中的申请 #{dup['id']}，不能重复提交")
        cur = conn.execute(
            "INSERT INTO applications(student_id, position_id, teacher_id, start_date, end_date, status, created_at) VALUES (?,?,?,?,?, 'submitted', ?)",
            (student_id, position_id, teacher_id, d1.isoformat(), d2.isoformat(), now()),
        )
        app_id = cur.lastrowid
        _event(conn, app_id, None, "application_submitted",
               f"{slabel} 提交岗位申请：{position['title']}，周期 {d1} ~ {d2}", slabel)
        adapters.notify(conn, f"老师 {teacher['name']}", "新的岗位申请待审核",
                        f"申请 #{app_id}：{student['name']} 申请 {position['title']}，请核对培养要求。", now())
        return {"id": app_id}


@audited
def college_review(app_id, actor):
    """学院审核（核对培养要求）。仅本学院老师；条件更新保证并发只有一人成功。"""
    with db.tx() as conn:
        app = load_application(conn, app_id)
        role, teacher, tlabel = load_actor(conn, actor)
        if role != "teacher" or teacher["college_id"] != app["student_college_id"]:
            raise ServiceError(403, "老师只能审核本学院的申请",
                               audit=(app_id, None, "unauthorized",
                                      f"{tlabel} 试图审核 {app['college_name']} 的申请 #{app_id}，被拒绝（跨学院）", tlabel))
        cur = conn.execute(
            "UPDATE applications SET status='college_approved' WHERE id=? AND status='submitted'", (app_id,))
        if cur.rowcount == 0:
            raise ServiceError(409, "申请状态已变化（可能已被其他审核人处理），请刷新")
        _event(conn, app_id, None, "college_approved", f"{tlabel} 审核通过（培养要求核对无误）", tlabel)
        contact = conn.execute(
            "SELECT * FROM contacts WHERE company_id=? AND active=1", (app["company_id"],)).fetchone()
        if contact:
            adapters.notify(conn, f"单位联系人 {contact['name']}", "岗位职责待确认",
                            f"申请 #{app_id}（{app['student_name']} / {app['position_title']}）已通过学院审核，请确认岗位职责。", now())
        return {"status": "college_approved"}


@audited
def company_confirm(app_id, actor):
    """单位联系人确认职责，随后生成三方协议 v1 进入电子签署。"""
    with db.tx() as conn:
        app = load_application(conn, app_id)
        role, contact, clabel = load_actor(conn, actor)
        if role != "contact" or contact["active"] != 1 or contact["company_id"] != app["company_id"]:
            raise ServiceError(403, "只能由本单位在任联系人确认职责",
                               audit=(app_id, None, "unauthorized",
                                      f"{clabel} 试图确认非本单位申请 #{app_id}，被拒绝", clabel))
        cur = conn.execute(
            "UPDATE applications SET status='signing' WHERE id=? AND status='college_approved'", (app_id,))
        if cur.rowcount == 0:
            raise ServiceError(409, "申请状态已变化（可能已被其他联系人处理），请刷新")
        position = conn.execute("SELECT * FROM positions WHERE id=?", (app["position_id"],)).fetchone()
        company = conn.execute("SELECT * FROM companies WHERE id=?", (app["company_id"],)).fetchone()
        cur = conn.execute(
            """INSERT INTO agreements(application_id, version, attachment_version, status, prev_id, change_id,
                                      start_date, end_date, position_title, position_duties, company_name,
                                      contact_name, created_at)
               VALUES (?, 1, 1, 'signing', NULL, NULL, ?,?,?,?,?,?,?)""",
            (app_id, app["start_date"], app["end_date"], position["title"], position["duties"],
             company["name"], contact["name"], now()),
        )
        agid = cur.lastrowid
        agreement = conn.execute("SELECT * FROM agreements WHERE id=?", (agid,)).fetchone()
        fpath = adapters.render_agreement_file(conn, agreement)
        conn.execute("UPDATE agreements SET file_path=? WHERE id=?", (fpath, agid))
        _event(conn, app_id, agid, "company_confirmed", f"{clabel} 确认岗位职责", clabel)
        _event(conn, app_id, agid, "version_created", "三方协议 v1 生成，进入电子签署（顺序：学生 → 学院 → 单位）", clabel)
        adapters.notify(conn, f"学生 {app['student_name']}", "三方协议待签署",
                        f"申请 #{app_id} 的三方协议 v1 已生成，请按顺序完成电子签署。", now())
        return {"agreement_id": agid}


# ---------------------------------------------------------------- 三方签署

@audited
def sign_agreement(agreement_id, party, actor):
    """按 学生→学院→单位 顺序签署；乱序 409 并留 order_error 事件；越权 403 留 unauthorized。"""
    with db.tx() as conn:
        agreement = _one(conn, "SELECT * FROM agreements WHERE id=?", (agreement_id,), "协议版本")
        app = load_application(conn, agreement["application_id"])
        _, _, alabel = load_actor(conn, actor)
        if agreement["status"] != "signing":
            raise ServiceError(409, f"协议 v{agreement['version']} 当前状态为 {agreement['status']}，旧版本不能重新签署或生效")
        signed = [r["party"] for r in conn.execute(
            "SELECT party FROM signatures WHERE agreement_id=? ORDER BY id", (agreement_id,)).fetchall()]
        expected = SIGN_ORDER[len(signed)]
        if party not in PARTY_NAMES:
            raise ServiceError(400, f"未知签署方：{party!r}")
        if party != expected:
            raise ServiceError(409, f"签署顺序错误：当前应由{PARTY_NAMES[expected]}方签署",
                               audit=(app["id"], agreement_id, "order_error",
                                      f"签署顺序错误：应由{PARTY_NAMES[expected]}方签署，{alabel} 试图以{PARTY_NAMES[party]}方身份提前签署，已拒绝",
                                      alabel))
        signer_label = _authorize_party(conn, app, party, actor)
        sig_hash = adapters.sign(agreement_id, agreement["version"], party, signer_label, now())
        try:
            conn.execute(
                "INSERT INTO signatures(agreement_id, party, signer, sig_hash, signed_at) VALUES (?,?,?,?,?)",
                (agreement_id, party, signer_label, sig_hash, now()),
            )
        except Exception as e:
            if "UNIQUE" in str(e):
                raise ServiceError(409, f"{PARTY_NAMES[party]}方已签署过该版本，不能重复签署")
            raise
        _event(conn, app["id"], agreement_id, "signed",
               f"{PARTY_NAMES[party]}方 {signer_label} 完成签署（{sig_hash}）", signer_label)
        if len(signed) + 1 == len(SIGN_ORDER):
            cur = conn.execute(
                "UPDATE agreements SET status='effective', effective_at=? WHERE id=? AND status='signing'",
                (now(), agreement_id))
            if cur.rowcount == 0:
                raise ServiceError(409, "协议状态已变化，请刷新")
            conn.execute("UPDATE applications SET status='effective' WHERE id=? AND status='signing'", (app["id"],))
            d1, d2 = _date(agreement["start_date"], "开始"), _date(agreement["end_date"], "结束")
            conn.execute(
                "INSERT INTO day_segments(student_id, application_id, agreement_id, start_date, end_date, days) VALUES (?,?,?,?,?,?)",
                (app["student_id"], app["id"], agreement_id, d1.isoformat(), d2.isoformat(), _days(d1, d2)))
            fresh = conn.execute("SELECT * FROM agreements WHERE id=?", (agreement_id,)).fetchone()
            fpath = adapters.render_agreement_file(conn, fresh)
            conn.execute("UPDATE agreements SET file_path=? WHERE id=?", (fpath, agreement_id))
            _event(conn, app["id"], agreement_id, "effective",
                   f"三方签署完成，协议 v{agreement['version']} 生效，实习周期 {d1} ~ {d2}（{_days(d1, d2)} 天）")
            for recip in (f"学生 {app['student_name']}", f"{app['college_name']} 指导老师", f"单位联系人 {agreement['contact_name']}"):
                adapters.notify(conn, recip, "三方协议已生效",
                                f"申请 #{app['id']} 协议 v{agreement['version']} 已生效并归档。", now())
            return {"status": "effective", "sig_hash": sig_hash}
        return {"status": "signing", "next": SIGN_ORDER[len(signed) + 1], "sig_hash": sig_hash}


# ---------------------------------------------------------------- 变更

def _project_change(conn, app, base, ctype, payload):
    """计算变更后的协议字段（纯函数，供对比预览与生效共用）。"""
    new = {
        "start_date": base["start_date"], "end_date": base["end_date"],
        "position_title": base["position_title"], "position_duties": base["position_duties"],
        "company_name": base["company_name"], "contact_name": base["contact_name"],
        "attachment_version": base["attachment_version"],
        "status": "effective", "note": "",
    }
    if ctype == "withdraw":
        new.update(end_date=payload["effective_date"], status="terminated",
                   note=f"单位撤岗，协议自 {payload['effective_date']} 起终止")
    elif ctype == "early_end":
        new.update(end_date=payload["end_date"], status="terminated",
                   note=f"实习提前结束，周期调整为 {base['start_date']} ~ {payload['end_date']}")
    elif ctype == "transfer":
        pos = _one(conn, "SELECT p.*, c.name AS company_name FROM positions p JOIN companies c ON c.id=p.company_id WHERE p.id=?",
                   (payload["new_position_id"],), "新岗位")
        contact = conn.execute("SELECT * FROM contacts WHERE company_id=? AND active=1", (pos["company_id"],)).fetchone()
        new.update(start_date=payload["effective_date"], end_date=payload.get("new_end_date") or base["end_date"],
                   position_title=pos["title"], position_duties=pos["duties"], company_name=pos["company_name"],
                   contact_name=contact["name"] if contact else "（未指定）",
                   note=f"学生转岗：{base['position_title']} → {pos['title']}，自 {payload['effective_date']} 起")
    elif ctype == "contact_change":
        new.update(contact_name=payload["new_contact_name"],
                   note=f"单位联系人更换：{base['contact_name']} → {payload['new_contact_name']}")
    elif ctype == "attachment_fix":
        new.update(attachment_version=base["attachment_version"] + 1,
                   note=f"协议附件不一致，更新附件至 A{base['attachment_version'] + 1}：{payload.get('note', '')}")
    return new


def propose_change(app_id, actor, ctype, payload):
    """对已生效协议发起变更；受影响方需重新确认。发起人所在方自动视为已确认。"""
    payload = payload or {}
    with db.tx() as conn:
        app = load_application(conn, app_id)
        role, row, alabel = load_actor(conn, actor)
        if ctype not in CHANGE_TYPES:
            raise ServiceError(400, f"未知变更类型：{ctype!r}")
        if app["status"] != "effective":
            raise ServiceError(409, "仅已生效的协议可以发起变更")
        base = conn.execute(
            "SELECT * FROM agreements WHERE application_id=? AND status='effective'", (app_id,)).fetchone()
        if not base:
            raise ServiceError(409, "找不到生效中的协议版本")
        pending = conn.execute(
            "SELECT id FROM changes WHERE application_id=? AND status='pending'", (app_id,)).fetchone()
        if pending:
            raise ServiceError(409, f"变更 #{pending['id']} 仍在确认中，同一时间只能有一个待确认变更")

        b1, b2 = _date(base["start_date"], "开始"), _date(base["end_date"], "结束")
        affected = ["student", "college", "company"]
        if ctype == "withdraw":
            if role != "contact" or row["company_id"] != app["company_id"] or row["active"] != 1:
                raise ServiceError(403, "撤岗只能由本单位在任联系人发起")
            d = _date(payload.get("effective_date"), "撤岗生效日期")
            if not (b1 <= d <= b2):
                raise ServiceError(400, f"撤岗生效日期须位于实习周期 {b1} ~ {b2} 内")
            conn.execute("UPDATE positions SET status='withdrawn' WHERE id=?", (app["position_id"],))
            _event(conn, app_id, base["id"], "position_withdrawn",
                   f"{alabel} 撤回岗位「{app['position_title']}」，自 {d} 起生效", alabel)
            payload = {"effective_date": d.isoformat()}
            affected = ["student", "college"]
        elif ctype == "transfer":
            if role != "student" or row["id"] != app["student_id"]:
                raise ServiceError(403, "转岗只能由学生本人发起")
            pos = _one(conn, "SELECT * FROM positions WHERE id=?", (payload.get("new_position_id"),), "新岗位")
            if pos["status"] != "open":
                raise ServiceError(409, f"新岗位「{pos['title']}」不可申请")
            if pos["id"] == app["position_id"]:
                raise ServiceError(400, "新岗位与当前岗位相同")
            d = _date(payload.get("effective_date"), "转岗生效日期")
            if not (b1 < d <= b2):
                raise ServiceError(400, f"转岗生效日期须位于 ({b1}, {b2}] 区间内")
            new_end = _date(payload["new_end_date"], "新结束日期") if payload.get("new_end_date") else b2
            if new_end < d:
                raise ServiceError(400, "新结束日期不能早于转岗生效日期")
            payload = {"new_position_id": pos["id"], "effective_date": d.isoformat(),
                       "new_end_date": new_end.isoformat()}
        elif ctype == "early_end":
            own = (role == "student" and row["id"] == app["student_id"]) or \
                  (role == "teacher" and row["college_id"] == app["student_college_id"]) or \
                  (role == "contact" and row["company_id"] == app["company_id"] and row["active"] == 1)
            if not own:
                raise ServiceError(403, "提前结束只能由学生本人、本学院老师或本单位联系人发起")
            d = _date(payload.get("end_date"), "新结束日期")
            if not (b1 <= d < b2):
                raise ServiceError(400, f"新结束日期须位于 [{b1}, {b2}) 区间内")
            payload = {"end_date": d.isoformat()}
        elif ctype == "contact_change":
            if role != "contact" or row["company_id"] != app["company_id"] or row["active"] != 1:
                raise ServiceError(403, "联系人更换只能由本单位在任联系人发起")
            name = str(payload.get("new_contact_name", "")).strip()
            if not name:
                raise ServiceError(400, "新联系人姓名不能为空")
            payload = {"new_contact_name": name}
            affected = ["student", "college"]
        elif ctype == "attachment_fix":
            if role != "teacher" or row["college_id"] != app["student_college_id"]:
                raise ServiceError(403, "附件更正只能由本学院老师发起")
            payload = {"note": str(payload.get("note", "")).strip()}

        cur = conn.execute(
            "INSERT INTO changes(application_id, type, status, payload, affected, base_agreement_id, created_by, created_at) VALUES (?,?, 'pending', ?,?,?,?,?)",
            (app_id, ctype, json.dumps(payload, ensure_ascii=False), json.dumps(affected), base["id"], alabel, now()),
        )
        change_id = cur.lastrowid
        _event(conn, app_id, base["id"], "change_proposed",
               f"{alabel} 发起变更「{CHANGE_TYPE_NAMES[ctype]}」（变更单 #{change_id}），受影响方：{'、'.join(PARTY_NAMES[p] for p in affected)}", alabel)
        proposer_party = {"student": "student", "teacher": "college", "contact": "company"}[role]
        if proposer_party in affected:
            sig = adapters.sign(change_id, 0, proposer_party, alabel, now())
            conn.execute(
                "INSERT INTO change_confirmations(change_id, party, confirmer, sig_hash, confirmed_at) VALUES (?,?,?,?,?)",
                (change_id, proposer_party, alabel, sig, now()))
        for p in affected:
            if p != proposer_party:
                recip = {"student": f"学生 {app['student_name']}",
                         "college": f"{app['college_name']} 指导老师",
                         "company": f"单位联系人 {base['contact_name']}"}[p]
                adapters.notify(conn, recip, f"协议变更待确认：{CHANGE_TYPE_NAMES[ctype]}",
                                f"申请 #{app_id} 的协议 v{base['version']} 有变更待你确认（变更单 #{change_id}）。", now())
        return {"id": change_id, "affected": affected}


@audited
def confirm_change(change_id, party, actor):
    """受影响方确认变更；全部确认后原子生效：旧版本 superseded，新版本落链。"""
    with db.tx() as conn:
        change = _one(conn, "SELECT * FROM changes WHERE id=?", (change_id,), "变更单")
        if change["status"] != "pending":
            raise ServiceError(409, f"变更单 #{change_id} 已处理（{change['status']}），不能重复确认")
        app = load_application(conn, change["application_id"])
        affected = json.loads(change["affected"])
        if party not in affected:
            raise ServiceError(400, f"{PARTY_NAMES.get(party, party)}方不是本次变更的受影响方")
        confirmer = _authorize_party(conn, app, party, actor, change=change)
        sig = adapters.sign(change["base_agreement_id"], 0, party, confirmer, now())
        try:
            conn.execute(
                "INSERT INTO change_confirmations(change_id, party, confirmer, sig_hash, confirmed_at) VALUES (?,?,?,?,?)",
                (change_id, party, confirmer, sig, now()))
        except Exception as e:
            if "UNIQUE" in str(e):
                raise ServiceError(409, f"{PARTY_NAMES[party]}方已确认过该变更")
            raise
        _event(conn, app["id"], change["base_agreement_id"], "change_confirmed",
               f"{PARTY_NAMES[party]}方 {confirmer} 确认变更「{CHANGE_TYPE_NAMES[change['type']]}」", confirmer)
        done = {r["party"] for r in conn.execute(
            "SELECT party FROM change_confirmations WHERE change_id=?", (change_id,)).fetchall()}
        if set(affected) <= done:
            _apply_change(conn, change)
            return {"status": "applied"}
        return {"status": "pending", "confirmed": sorted(done), "waiting": sorted(set(affected) - done)}


def _apply_change(conn, change):
    """所有受影响方确认后生效。条件 UPDATE 保证并发下只生效一次。"""
    cur = conn.execute(
        "UPDATE changes SET status='applied', applied_at=? WHERE id=? AND status='pending'", (now(), change["id"]))
    if cur.rowcount == 0:
        return  # 另一审核人/线程已先行生效，本次直接返回，绝不重复应用
    app = load_application(conn, change["application_id"])
    base = conn.execute("SELECT * FROM agreements WHERE id=?", (change["base_agreement_id"],)).fetchone()
    payload = json.loads(change["payload"])
    new = _project_change(conn, app, base, change["type"], payload)
    next_version = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM agreements WHERE application_id=?",
        (app["id"],)).fetchone()["v"]

    # 旧版本失效：effective -> superseded（条件更新，防并发复活）
    cur = conn.execute(
        "UPDATE agreements SET status='superseded' WHERE id=? AND status='effective'", (base["id"],))
    if cur.rowcount == 0:
        raise ServiceError(409, "基准协议版本已失效，变更无法应用")
    _event(conn, app["id"], base["id"], "superseded",
           f"协议 v{base['version']} 被 v{next_version} 取代，即时失效")

    if change["type"] == "contact_change":
        conn.execute("UPDATE contacts SET active=0 WHERE company_id=? AND active=1", (app["company_id"],))
        conn.execute("INSERT INTO contacts(company_id, name, active) VALUES (?,?,1)",
                     (app["company_id"], payload["new_contact_name"]))

    cur = conn.execute(
        """INSERT INTO agreements(application_id, version, attachment_version, status, prev_id, change_id,
                                  start_date, end_date, position_title, position_duties, company_name,
                                  contact_name, note, created_at, effective_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (app["id"], next_version, new["attachment_version"], new["status"], base["id"], change["id"],
         new["start_date"], new["end_date"], new["position_title"], new["position_duties"],
         new["company_name"], new["contact_name"], new["note"], now(), now()))
    new_id = cur.lastrowid
    conn.execute("UPDATE changes SET result_agreement_id=? WHERE id=?", (new_id, change["id"]))

    # 变更确认即新版本的签署记录
    for c in conn.execute("SELECT party, confirmer, sig_hash, confirmed_at FROM change_confirmations WHERE change_id=?",
                          (change["id"],)).fetchall():
        conn.execute(
            "INSERT INTO signatures(agreement_id, party, signer, sig_hash, signed_at) VALUES (?,?,?,?,?)",
            (new_id, c["party"], c["confirmer"], c["sig_hash"], c["confirmed_at"]))

    # 实习天数：截断旧段 / 续接新段，保证连续可查
    seg = conn.execute("SELECT * FROM day_segments WHERE agreement_id=?", (base["id"],)).fetchone()
    if change["type"] in ("withdraw", "early_end"):
        end = _date(new["end_date"], "终止日期")
        if seg:
            s = _date(seg["start_date"], "段开始")
            conn.execute("UPDATE day_segments SET end_date=?, days=? WHERE id=?",
                         (end.isoformat(), _days(s, end), seg["id"]))
        conn.execute("UPDATE applications SET status='ended' WHERE id=?", (app["id"],))
        _event(conn, app["id"], new_id, "terminated",
               f"协议 v{next_version} 登记终止：{new['note']}，实习天数截断至 {end}")
    elif change["type"] == "transfer":
        d = _date(payload["effective_date"], "转岗生效日期")
        if seg:
            s = _date(seg["start_date"], "段开始")
            conn.execute("UPDATE day_segments SET end_date=?, days=? WHERE id=?",
                         ((d - timedelta(days=1)).isoformat(), _days(s, d - timedelta(days=1)), seg["id"]))
        e = _date(new["end_date"], "新结束日期")
        conn.execute(
            "INSERT INTO day_segments(student_id, application_id, agreement_id, start_date, end_date, days) VALUES (?,?,?,?,?,?)",
            (app["student_id"], app["id"], new_id, d.isoformat(), e.isoformat(), _days(d, e)))

    fresh = conn.execute("SELECT * FROM agreements WHERE id=?", (new_id,)).fetchone()
    fpath = adapters.render_agreement_file(conn, fresh)
    conn.execute("UPDATE agreements SET file_path=? WHERE id=?", (fpath, new_id))
    _event(conn, app["id"], new_id, "version_created",
           f"协议 v{next_version}（附件 A{new['attachment_version']}）由变更「{CHANGE_TYPE_NAMES[change['type']]}」产生并归档")
    _event(conn, app["id"], new_id, "change_applied", f"变更单 #{change['id']} 已生效：{new['note']}")
    for p in json.loads(change["affected"]):
        recip = {"student": f"学生 {app['student_name']}", "college": f"{app['college_name']} 指导老师",
                 "company": f"单位联系人 {new['contact_name']}"}[p]
        adapters.notify(conn, recip, "协议变更已生效",
                        f"申请 #{app['id']}：{CHANGE_TYPE_NAMES[change['type']]}已生效，协议 v{next_version} 已归档，旧版本同时失效。", now())


# ---------------------------------------------------------------- 查询

def get_days(student_id):
    rows = db.query(
        "SELECT * FROM day_segments WHERE student_id=? ORDER BY start_date, id", (student_id,))
    segments, continuous = [], True
    prev_end = None
    for r in rows:
        s, e = _date(r["start_date"], "开始"), _date(r["end_date"], "结束")
        if prev_end is not None and s != prev_end + timedelta(days=1):
            continuous = False
        segments.append({"application_id": r["application_id"], "agreement_id": r["agreement_id"],
                         "start_date": s.isoformat(), "end_date": e.isoformat(), "days": r["days"]})
        prev_end = e
    return {"student_id": student_id, "segments": segments,
            "total_days": sum(s["days"] for s in segments), "continuous": continuous}


def _agreement_json(conn, a):
    sigs = conn.execute(
        "SELECT party, signer, sig_hash, signed_at FROM signatures WHERE agreement_id=? ORDER BY id", (a["id"],)).fetchall()
    return {"id": a["id"], "version": a["version"], "attachment_version": a["attachment_version"],
            "status": a["status"], "prev_id": a["prev_id"], "change_id": a["change_id"],
            "start_date": a["start_date"], "end_date": a["end_date"],
            "position_title": a["position_title"], "position_duties": a["position_duties"],
            "company_name": a["company_name"], "contact_name": a["contact_name"],
            "note": a["note"], "file_path": a["file_path"],
            "created_at": a["created_at"], "effective_at": a["effective_at"],
            "signatures": [dict(s) for s in sigs]}


def get_chain(app_id):
    conn = db.connect()
    try:
        app = load_application(conn, app_id)
        versions = [_agreement_json(conn, a) for a in conn.execute(
            "SELECT * FROM agreements WHERE application_id=? ORDER BY version", (app_id,)).fetchall()]
        events = [dict(e) for e in conn.execute(
            "SELECT * FROM events WHERE application_id=? ORDER BY created_at, id", (app_id,)).fetchall()]
        return {"application_id": app_id, "student_name": app["student_name"], "versions": versions, "events": events}
    finally:
        conn.close()


def get_compare(change_id):
    conn = db.connect()
    try:
        change = _one(conn, "SELECT * FROM changes WHERE id=?", (change_id,), "变更单")
        app = load_application(conn, change["application_id"])
        base = conn.execute("SELECT * FROM agreements WHERE id=?", (change["base_agreement_id"],)).fetchone()
        payload = json.loads(change["payload"])
        projected = _project_change(conn, app, base, change["type"], payload)
        result = None
        if change["result_agreement_id"]:
            r = conn.execute("SELECT * FROM agreements WHERE id=?", (change["result_agreement_id"],)).fetchone()
            result = _agreement_json(conn, r)
        after = result or {**projected, "version": base["version"] + 1, "status": projected["status"]}
        fields = [("position_title", "岗位"), ("position_duties", "岗位职责"),
                  ("start_date", "开始日期"), ("end_date", "结束日期"),
                  ("company_name", "实习单位"), ("contact_name", "单位联系人")]
        rows = []
        for key, label in fields:
            before, after_v = base[key], after[key]
            rows.append({"field": label, "before": before, "after": after_v, "changed": before != after_v})
        rows.append({"field": "附件版本", "before": f"A{base['attachment_version']}",
                     "after": f"A{after['attachment_version']}",
                     "changed": base["attachment_version"] != after["attachment_version"]})
        confirmations = [dict(c) for c in conn.execute(
            "SELECT party, confirmer, confirmed_at FROM change_confirmations WHERE change_id=? ORDER BY id",
            (change_id,)).fetchall()]
        return {"id": change["id"], "application_id": change["application_id"], "type": change["type"],
                "type_name": CHANGE_TYPE_NAMES[change["type"]], "status": change["status"],
                "student_name": app["student_name"], "created_by": change["created_by"],
                "created_at": change["created_at"], "affected": json.loads(change["affected"]),
                "confirmations": confirmations, "base_version": base["version"],
                "result_version": after.get("version"), "rows": rows}
    finally:
        conn.close()


def get_teacher_todos(teacher_id):
    conn = db.connect()
    try:
        teacher = _one(conn, "SELECT t.*, c.name AS college_name FROM teachers t JOIN colleges c ON c.id=t.college_id WHERE t.id=?",
                       (teacher_id,), "老师")
        cid = teacher["college_id"]
        reviews = [dict(r) for r in conn.execute(
            """SELECT a.id, a.created_at, s.name AS student_name, p.title AS position_title, a.start_date, a.end_date
               FROM applications a JOIN students s ON s.id=a.student_id JOIN positions p ON p.id=a.position_id
               WHERE a.status='submitted' AND s.college_id=? ORDER BY a.id""", (cid,)).fetchall()]
        to_sign = []
        for a in conn.execute(
                """SELECT ag.id, ag.version, ag.application_id, s.name AS student_name, ag.position_title
                   FROM agreements ag JOIN applications ap ON ap.id=ag.application_id
                   JOIN students s ON s.id=ap.student_id
                   WHERE ag.status='signing' AND s.college_id=? ORDER BY ag.id""", (cid,)).fetchall():
            n = conn.execute("SELECT COUNT(*) AS n FROM signatures WHERE agreement_id=?", (a["id"],)).fetchone()["n"]
            if SIGN_ORDER[n] == "college":
                to_sign.append(dict(a))
        changes = [dict(r) for r in conn.execute(
            """SELECT ch.id, ch.type, ch.created_at, ch.created_by, s.name AS student_name, ch.application_id
               FROM changes ch JOIN applications a ON a.id=ch.application_id JOIN students s ON s.id=a.student_id
               WHERE ch.status='pending' AND s.college_id=? AND ch.affected LIKE '%"college"%'
               AND NOT EXISTS (SELECT 1 FROM change_confirmations cc WHERE cc.change_id=ch.id AND cc.party='college')
               ORDER BY ch.id""", (cid,)).fetchall()]
        for c in changes:
            c["type_name"] = CHANGE_TYPE_NAMES[c["type"]]
        return {"teacher": {"id": teacher["id"], "name": teacher["name"], "college_name": teacher["college_name"]},
                "reviews": reviews, "to_sign": to_sign, "changes": changes}
    finally:
        conn.close()


def get_state():
    conn = db.connect()
    try:
        def rows(sql, p=()):
            return [dict(r) for r in conn.execute(sql, p).fetchall()]
        state = {
            "colleges": rows("SELECT * FROM colleges ORDER BY id"),
            "teachers": rows("""SELECT t.*, c.name AS college_name FROM teachers t JOIN colleges c ON c.id=t.college_id ORDER BY t.id"""),
            "students": rows("""SELECT s.*, c.name AS college_name FROM students s JOIN colleges c ON c.id=s.college_id ORDER BY s.id"""),
            "companies": rows("SELECT * FROM companies ORDER BY id"),
            "contacts": rows("""SELECT ct.*, cp.name AS company_name FROM contacts ct JOIN companies cp ON cp.id=ct.company_id ORDER BY ct.id"""),
            "positions": rows("""SELECT p.*, c.name AS company_name FROM positions p JOIN companies c ON c.id=p.company_id ORDER BY p.id"""),
            "notifications": rows("SELECT * FROM notifications ORDER BY id DESC LIMIT 30"),
        }
        apps = []
        for a in rows("""SELECT ap.*, s.name AS student_name, s.college_id AS student_college_id,
                                c.name AS college_name, t.name AS teacher_name,
                                p.title AS position_title, p.company_id, cp.name AS company_name
                         FROM applications ap
                         JOIN students s ON s.id=ap.student_id JOIN colleges c ON c.id=s.college_id
                         JOIN teachers t ON t.id=ap.teacher_id JOIN positions p ON p.id=ap.position_id
                         JOIN companies cp ON cp.id=p.company_id ORDER BY ap.id"""):
            cur = conn.execute(
                "SELECT * FROM agreements WHERE application_id=? ORDER BY version DESC LIMIT 1", (a["id"],)).fetchone()
            a["agreement"] = _agreement_json(conn, cur) if cur else None
            if cur and cur["status"] == "signing":
                n = conn.execute("SELECT COUNT(*) AS n FROM signatures WHERE agreement_id=?", (cur["id"],)).fetchone()["n"]
                a["next_party"] = SIGN_ORDER[n]
            else:
                a["next_party"] = None
            ch = conn.execute(
                "SELECT * FROM changes WHERE application_id=? AND status='pending' ORDER BY id DESC LIMIT 1", (a["id"],)).fetchone()
            if ch:
                ch = dict(ch)
                ch["affected"] = json.loads(ch["affected"])
                ch["type_name"] = CHANGE_TYPE_NAMES[ch["type"]]
                ch["confirmations"] = rows("SELECT party, confirmer, confirmed_at FROM change_confirmations WHERE change_id=?", (ch["id"],))
            a["pending_change"] = ch
            apps.append(a)
        state["applications"] = apps
        changes = []
        for ch in rows("""SELECT ch.*, s.name AS student_name FROM changes ch
                          JOIN applications a ON a.id=ch.application_id JOIN students s ON s.id=a.student_id
                          ORDER BY ch.id DESC"""):
            ch["affected"] = json.loads(ch["affected"])
            ch["type_name"] = CHANGE_TYPE_NAMES[ch["type"]]
            ch["confirmations"] = rows("SELECT party, confirmer, confirmed_at FROM change_confirmations WHERE change_id=?", (ch["id"],))
            changes.append(ch)
        state["changes"] = changes
        return state
    finally:
        conn.close()


def get_agreement_file(agreement_id):
    row = db.query_one("SELECT file_path FROM agreements WHERE id=?", (agreement_id,))
    if not row or not row["file_path"]:
        raise ServiceError(404, "协议文件不存在")
    path = os.path.join(adapters.DATA_DIR, row["file_path"])
    if not os.path.isfile(path):
        raise ServiceError(404, "协议文件已被移除")
    with open(path, encoding="utf-8") as f:
        return f.read()
