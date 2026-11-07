"""Preserve IDs, references and indexes while allowing a third source platform."""
import sqlite3
from contextlib import closing


def allow_wecom(path):
    if not path.exists():
        return
    with closing(sqlite3.connect(path, timeout=30)) as db, db:
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'").fetchone()
        if not row or "'wecom'" in row[0]:
            return
        old = row[0]
        needle = "CHECK(platform IN ('qq','wechat'))"
        if needle not in old:
            raise ValueError('消息表结构未知，未执行企业微信迁移')
        indexes = [r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE tbl_name='messages' AND type IN ('index','trigger') AND sql IS NOT NULL")]
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('BEGIN IMMEDIATE')
        sql = old.replace(needle, "CHECK(platform IN ('qq','wechat','wecom'))")
        sql = sql.replace('CREATE TABLE messages', 'CREATE TABLE messages_wecom_migration', 1)
        if sql == old or 'CREATE TABLE messages_wecom_migration' not in sql:
            raise ValueError('消息表迁移无法识别')
        db.execute(sql)
        columns = ','.join('"' + r[1].replace('"', '""') + '"' for r in db.execute('PRAGMA table_info(messages)'))
        db.execute(f'INSERT INTO messages_wecom_migration ({columns}) SELECT {columns} FROM messages')
        db.execute('DROP TABLE messages')
        db.execute('ALTER TABLE messages_wecom_migration RENAME TO messages')
        for statement in indexes:
            db.execute(statement)
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('迁移引用校验失败，已回滚')
