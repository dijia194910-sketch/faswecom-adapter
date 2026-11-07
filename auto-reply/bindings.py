"""微信客户ID ↔ 签约手机号 绑定表（后台维护，客户不参与）。"""
from __future__ import annotations
import sqlite3, time
from pathlib import Path

DB = Path(__file__).resolve().parent / "bindings.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bindings(
    account    TEXT NOT NULL,
    peer_id    TEXT NOT NULL,
    phone      TEXT NOT NULL,
    state      TEXT NOT NULL DEFAULT 'active',   -- active / pending / revoked
    source     TEXT NOT NULL DEFAULT 'admin',
    note       TEXT NOT NULL DEFAULT '',
    unionid    TEXT NOT NULL DEFAULT '',   -- 微信用户跨企业稳定ID（有就存，迁移用）
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (account, peer_id)
);
CREATE INDEX IF NOT EXISTS bindings_phone ON bindings(phone);
CREATE TABLE IF NOT EXISTS binding_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL, action TEXT NOT NULL,
    account TEXT, peer_id TEXT, phone TEXT, note TEXT
);
"""


def connect(path: Path | str = DB):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    cols = {r[1] for r in db.execute("PRAGMA table_info(bindings)")}
    if "unionid" not in cols:
        db.execute("ALTER TABLE bindings ADD COLUMN unionid TEXT NOT NULL DEFAULT ''")
    return db


def log(db, action, account, peer_id, phone, note=""):
    db.execute("INSERT INTO binding_log(at,action,account,peer_id,phone,note) VALUES(?,?,?,?,?,?)",
               (time.time(), action, account, peer_id, phone, note))


def set_binding(account: str, peer_id: str, phone: str, *, state="active",
                source="admin", note="", unionid="", db=None) -> None:
    own = db is None
    db = db or connect()
    try:
        phone = str(phone).strip()
        now = time.time()
        db.execute("""INSERT INTO bindings(account,peer_id,phone,state,source,note,unionid,created_at,updated_at)
                      VALUES(?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(account,peer_id) DO UPDATE SET
                        phone=excluded.phone, state=excluded.state, source=excluded.source,
                        note=excluded.note, unionid=excluded.unionid, updated_at=excluded.updated_at""",
                   (str(account), str(peer_id), phone, state, source, note, str(unionid or ""), now, now))
        log(db, "set:" + state, account, peer_id, phone, note)
        db.commit()
    finally:
        if own:
            db.close()


def get_binding(account: str, peer_id: str, *, db=None):
    own = db is None
    db = db or connect()
    try:
        row = db.execute("SELECT * FROM bindings WHERE account=? AND peer_id=?",
                         (str(account), str(peer_id))).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            db.close()


def list_bindings(state=None, db=None):
    own = db is None
    db = db or connect()
    try:
        if state:
            cur = db.execute("SELECT * FROM bindings WHERE state=? ORDER BY account,peer_id", (state,))
        else:
            cur = db.execute("SELECT * FROM bindings ORDER BY account,peer_id")
        return [dict(r) for r in cur]
    finally:
        if own:
            db.close()


def revoke(account: str, peer_id: str, note="", db=None):
    set_binding(account, peer_id, "", state="revoked", note=note, db=db)
