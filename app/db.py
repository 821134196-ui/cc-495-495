"""SQLite 数据层:连接、schema、事务助手。

签署记录、版本链、确认记录、实习天数台账都落在 SQLite;
协议正文与附件是本地文件(见 services 中的文件写入)。
所有数据文件都放在数据库文件所在目录下,测试可指向临时目录。
"""
import os
import sqlite3
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "internship.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS colleges(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS teachers(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  college_id INTEGER NOT NULL REFERENCES colleges(id)
);
CREATE TABLE IF NOT EXISTS students(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  student_no TEXT NOT NULL,
  college_id INTEGER NOT NULL REFERENCES colleges(id)
);
CREATE TABLE IF NOT EXISTS units(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unit_contacts(
  id INTEGER PRIMARY KEY,
  unit_id INTEGER NOT NULL REFERENCES units(id),
  name TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS positions(
  id INTEGER PRIMARY KEY,
  unit_id INTEGER NOT NULL REFERENCES units(id),
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open'          -- open / withdrawn
);
CREATE TABLE IF NOT EXISTS applications(
  id INTEGER PRIMARY KEY,
  student_id INTEGER NOT NULL REFERENCES students(id),
  position_id INTEGER NOT NULL REFERENCES positions(id),
  teacher_id INTEGER NOT NULL REFERENCES teachers(id),
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending_college',
  -- pending_college / pending_unit / signing / active / completed / rejected
  review_note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agreements(
  id INTEGER PRIMARY KEY,
  application_id INTEGER NOT NULL REFERENCES applications(id),
  version INTEGER NOT NULL,
  prev_id INTEGER REFERENCES agreements(id),
  change_type TEXT NOT NULL DEFAULT 'initial',
  -- initial / withdrawal / transfer / early_end / contact_change / attachment_fix
  change_reason TEXT NOT NULL DEFAULT '',
  initiator TEXT NOT NULL DEFAULT '',          -- student / college / unit
  position_id INTEGER NOT NULL REFERENCES positions(id),
  teacher_id INTEGER NOT NULL REFERENCES teachers(id),
  unit_contact_id INTEGER NOT NULL REFERENCES unit_contacts(id),
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  attachment_version INTEGER NOT NULL DEFAULT 1,
  file_path TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'signing',
  -- signing / change_pending / effective / superseded
  created_at TEXT NOT NULL,
  effective_at TEXT,
  UNIQUE(application_id, version)
);
-- 关键约束:同一申请同一时刻只允许一个生效版本(并发兜底)
CREATE UNIQUE INDEX IF NOT EXISTS one_effective_per_app
  ON agreements(application_id) WHERE status='effective';
CREATE TABLE IF NOT EXISTS signatures(
  id INTEGER PRIMARY KEY,
  agreement_id INTEGER NOT NULL REFERENCES agreements(id),
  party TEXT NOT NULL,                          -- student / college / unit
  seq INTEGER NOT NULL,                         -- 签署顺序 1..3
  signer_name TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',       -- pending / signed
  token TEXT NOT NULL DEFAULT '',               -- 模拟签名适配器返回的签名串
  signed_at TEXT,
  UNIQUE(agreement_id, party)
);
CREATE TABLE IF NOT EXISTS confirmations(
  id INTEGER PRIMARY KEY,
  agreement_id INTEGER NOT NULL REFERENCES agreements(id),
  party TEXT NOT NULL,                          -- 受影响方
  status TEXT NOT NULL DEFAULT 'pending',       -- pending / confirmed
  confirmed_at TEXT,
  UNIQUE(agreement_id, party)
);
CREATE TABLE IF NOT EXISTS segments(
  id INTEGER PRIMARY KEY,
  application_id INTEGER NOT NULL REFERENCES applications(id),
  agreement_id INTEGER NOT NULL REFERENCES agreements(id),
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  days INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY,
  user_key TEXT NOT NULL,
  message TEXT NOT NULL,
  created_at TEXT NOT NULL,
  read INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY,
  application_id INTEGER,
  agreement_id INTEGER,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
"""


def dirs_for(db_path):
    """协议正文、附件、日志目录都跟随数据库所在目录。"""
    base = os.path.dirname(os.path.abspath(db_path))
    return {
        "base": base,
        "agreements": os.path.join(base, "agreements"),
        "attachments": os.path.join(base, "attachments"),
        "logs": os.path.join(base, "logs"),
    }


def ensure_dirs(db_path=DB_PATH):
    for d in dirs_for(db_path).values():
        os.makedirs(d, exist_ok=True)


def connect(db_path=DB_PATH):
    ensure_dirs(db_path)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(db_path=DB_PATH):
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


@contextmanager
def immediate_tx(conn):
    """BEGIN IMMEDIATE 事务:进入即拿写锁,配合 CAS 更新保证并发安全。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
