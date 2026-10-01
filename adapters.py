"""模拟适配器：电子签名、通知、协议文件落盘。均为本地虚构实现，不触网。"""
import hashlib
import os

import db

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

PARTY_NAMES = {"student": "学生", "college": "学院", "company": "单位"}


def configure(data_dir):
    """切换数据目录（测试用临时目录）。"""
    global DATA_DIR
    DATA_DIR = data_dir


def agreements_dir():
    path = os.path.join(DATA_DIR, "agreements")
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------- 模拟签名

def sign(agreement_id, version, party, signer, when):
    """生成虚构电子签名（哈希即签名值）。"""
    raw = f"AG{agreement_id}:v{version}:{party}:{signer}:{when}"
    return "SIG-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16].upper()


# ---------------------------------------------------------------- 模拟通知

def notify(conn, recipient, subject, body, when):
    """通知写入 SQLite，并追加到本地 notifications.log（模拟短信/邮件网关）。"""
    conn.execute(
        "INSERT INTO notifications(recipient, subject, body, created_at) VALUES (?,?,?,?)",
        (recipient, subject, body, when),
    )
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, "notifications.log"), "a", encoding="utf-8") as f:
        f.write(f"[{when}] -> {recipient} | {subject} | {body}\n")


# ---------------------------------------------------------------- 协议文件

def agreement_filename(agreement):
    return f"AG-{agreement['application_id']:04d}-v{agreement['version']}-A{agreement['attachment_version']}.md"


def render_agreement_file(conn, agreement):
    """把某版本协议渲染为本地虚构协议文件，返回相对路径。文件名含版本号与附件版本号。"""
    sigs = conn.execute(
        "SELECT party, signer, sig_hash, signed_at FROM signatures WHERE agreement_id=? ORDER BY signed_at, id",
        (agreement["id"],),
    ).fetchall()
    app = conn.execute(
        """SELECT a.*, s.name AS student_name, s.student_no, c.name AS college_name, t.name AS teacher_name
           FROM applications a
           JOIN students s ON s.id = a.student_id
           JOIN colleges c ON c.id = s.college_id
           JOIN teachers t ON t.id = a.teacher_id
           WHERE a.id=?""",
        (agreement["application_id"],),
    ).fetchone()

    status_names = {"signing": "签署中", "effective": "已生效", "superseded": "已被新版本取代", "terminated": "已终止"}
    lines = [
        "# 学生校外实习三方协议（虚构样张）",
        "",
        f"协议编号：AG-{agreement['application_id']:04d}    版本：v{agreement['version']}    附件版本：A{agreement['attachment_version']}",
        f"状态：{status_names.get(agreement['status'], agreement['status'])}",
        "",
        "## 甲（学生）",
        f"{app['student_name']}（学号 {app['student_no']}，{app['college_name']}）",
        "",
        "## 乙（学院）",
        f"{app['college_name']}    指导老师：{app['teacher_name']}",
        "",
        "## 丙（实习单位）",
        f"{agreement['company_name']}    单位联系人：{agreement['contact_name']}",
        "",
        "## 实习岗位",
        f"岗位名称：{agreement['position_title']}",
        f"岗位职责：{agreement['position_duties']}",
        "",
        "## 实习周期",
        f"{agreement['start_date']} 至 {agreement['end_date']}",
        "",
        "## 签署记录",
    ]
    if sigs:
        for s in sigs:
            lines.append(f"- {PARTY_NAMES.get(s['party'], s['party'])}方 {s['signer']} 于 {s['signed_at']} 签署，签名 {s['sig_hash']}")
    else:
        lines.append("- （暂无）")
    if agreement["note"]:
        lines += ["", "## 变更说明", agreement["note"]]
    if agreement["prev_id"]:
        lines += ["", f"本版本由 v{agreement['version'] - 1} 变更产生，变更前后版本链完整可查。"]

    fname = agreement_filename(agreement)
    with open(os.path.join(agreements_dir(), fname), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return os.path.join("agreements", fname)
