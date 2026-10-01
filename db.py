"""SQLite 存储层：连接、事务、schema。

设计要点：
- 每次操作独立连接（线程安全，配合 ThreadingHTTPServer 与并发测试）。
- BEGIN IMMEDIATE 串行化写事务；唯一部分索引保证同一申请至多一个生效版本，
  从数据库层面杜绝“两名审核人同时操作让旧版本重新生效”。
"""
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "app.db")

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS colleges (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS teachers (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    college_id INTEGER NOT NULL REFERENCES colleges(id)
);

CREATE TABLE IF NOT EXISTS students (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    student_no TEXT NOT NULL UNIQUE,
    college_id INTEGER NOT NULL REFERENCES colleges(id)
);

CREATE TABLE IF NOT EXISTS companies (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS contacts (
    id         INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    name       TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS positions (
    id         INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    title      TEXT NOT NULL,
    duties     TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'open',      -- open | withdrawn
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id          INTEGER PRIMARY KEY,
    student_id  INTEGER NOT NULL REFERENCES students(id),
    position_id INTEGER NOT NULL REFERENCES positions(id),
    teacher_id  INTEGER NOT NULL REFERENCES teachers(id),   -- 指导老师
    start_date  TEXT NOT NULL,
    end_date    TEXT NOT NULL,
    -- submitted -> college_approved -> company_confirmed -> signing -> effective -> ended
    status      TEXT NOT NULL DEFAULT 'submitted',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agreements (
    id                INTEGER PRIMARY KEY,
    application_id    INTEGER NOT NULL REFERENCES applications(id),
    version           INTEGER NOT NULL,
    attachment_version INTEGER NOT NULL DEFAULT 1,
    status            TEXT NOT NULL DEFAULT 'signing',  -- signing | effective | superseded | terminated
    prev_id           INTEGER REFERENCES agreements(id),
    change_id         INTEGER,                          -- 由哪个变更产生（v1 为 NULL）
    start_date        TEXT NOT NULL,
    end_date          TEXT NOT NULL,
    position_title    TEXT NOT NULL,
    position_duties   TEXT NOT NULL,
    company_name      TEXT NOT NULL,
    contact_name      TEXT NOT NULL,
    file_path         TEXT,
    note              TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    effective_at      TEXT,
    UNIQUE (application_id, version)
);

-- 同一申请至多一个生效版本：并发审批也不会让旧版本重新生效
CREATE UNIQUE INDEX IF NOT EXISTS one_effective_per_application
    ON agreements(application_id) WHERE status = 'effective';

CREATE TABLE IF NOT EXISTS signatures (
    id           INTEGER PRIMARY KEY,
    agreement_id INTEGER NOT NULL REFERENCES agreements(id),
    party        TEXT NOT NULL,        -- student | college | company
    signer       TEXT NOT NULL,
    sig_hash     TEXT NOT NULL,
    signed_at    TEXT NOT NULL,
    UNIQUE (agreement_id, party)
);

CREATE TABLE IF NOT EXISTS changes (
    id                 INTEGER PRIMARY KEY,
    application_id     INTEGER NOT NULL REFERENCES applications(id),
    type               TEXT NOT NULL,  -- withdraw | transfer | early_end | contact_change | attachment_fix
    status             TEXT NOT NULL DEFAULT 'pending',  -- pending | applied | cancelled
    payload            TEXT NOT NULL DEFAULT '{}',
    affected           TEXT NOT NULL,  -- JSON array of parties
    base_agreement_id  INTEGER NOT NULL REFERENCES agreements(id),
    result_agreement_id INTEGER REFERENCES agreements(id),
    created_by         TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    applied_at         TEXT
);

CREATE TABLE IF NOT EXISTS change_confirmations (
    id           INTEGER PRIMARY KEY,
    change_id    INTEGER NOT NULL REFERENCES changes(id),
    party        TEXT NOT NULL,
    confirmer    TEXT NOT NULL,
    sig_hash     TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    UNIQUE (change_id, party)
);

-- 实习天数分段：每段归属一个协议版本，变更时截断/续接，保证连续可查
CREATE TABLE IF NOT EXISTS day_segments (
    id             INTEGER PRIMARY KEY,
    student_id     INTEGER NOT NULL REFERENCES students(id),
    application_id INTEGER NOT NULL REFERENCES applications(id),
    agreement_id   INTEGER NOT NULL REFERENCES agreements(id),
    start_date     TEXT NOT NULL,
    end_date       TEXT NOT NULL,
    days           INTEGER NOT NULL
);

-- 版本链事件流：版本产生/签署/生效/失效/变更/异常（签署顺序错误、越权尝试）全部留痕
CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL,
    agreement_id   INTEGER,
    kind           TEXT NOT NULL,
    detail         TEXT NOT NULL,
    actor          TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY,
    recipient  TEXT NOT NULL,
    subject    TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def configure(db_path):
    """切换数据库文件（测试用临时库）。"""
    global DB_PATH
    DB_PATH = db_path


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def tx():
    """写事务：BEGIN IMMEDIATE 串行化并发写，提交/回滚自动处理。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query(sql, params=()):
    conn = connect()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def query_one(sql, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
