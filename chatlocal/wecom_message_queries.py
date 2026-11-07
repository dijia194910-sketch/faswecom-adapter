"""Stable message pages and optional exact-substring candidate searches."""
import sqlite3
from .wecom_search_index import candidate_sql, _unsupported


COLUMNS = 'm.id,m.conversation_id,m.conversation,m.sender_id,m.sender,m.timestamp,m.content,m.is_self,m.source_id'


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _recent_matches(db, cid, query, before, limit):
    """Bounded fast path for common terms; do not sort a million FTS hits."""
    clauses, args = ["m.platform='wecom'"], []
    if cid:
        clauses.append('m.conversation_id=?')
        args.append(cid)
    if before is not None:
        clauses.append('(m.timestamp,m.id)<(?,?)')
        args.extend(before)
    index = 'msg_timeline' if cid else 'msg_wecom_time'
    return db.execute(f'''SELECT * FROM (
        SELECT {COLUMNS} FROM messages m INDEXED BY {index}
        WHERE {' AND '.join(clauses)} ORDER BY m.timestamp DESC,m.id DESC LIMIT ?)
        WHERE instr(lower(content),lower(?))>0 ORDER BY timestamp DESC,id DESC LIMIT ?''',
        args + [max(256, limit*4), query, limit]).fetchall()


def _query(db, cid, query, before, limit, offset=0):
    clauses, args = ["m.platform='wecom'"], []
    if cid:
        clauses.append('m.conversation_id=?')
        args.append(cid)
    if before is not None:
        clauses.append('(m.timestamp,m.id)<(?,?)')
        args.extend(before)
    if query:
        clauses.append('instr(lower(m.content),lower(?))>0')
        args.append(query)
    candidates = candidate_sql(db, query) if query else None
    if candidates:
        if not offset:
            recent = _recent_matches(db, cid, query, before, limit)
            if len(recent) == limit:
                return recent
        sql, candidate_args = candidates
        # Candidate-driven rowid lookups avoid scanning the timeline for rare
        # hits. Sorting the matching subset preserves the old search ordering.
        # CROSS JOIN fixes the driving side even before ANALYZE. A normal JOIN
        # can be reordered into msg_sender(platform=?) + a lookup in an empty
        # candidate set, scanning every message for a zero-hit search.
        source = f'({sql}) c CROSS JOIN messages m ON m.id=c.id'
        params = candidate_args + args
    else:
        # Without statistics SQLite may otherwise choose msg_sender and sort
        # every WeCom row even for an unfiltered latest-50 request.
        source = 'messages m INDEXED BY ' + ('msg_timeline' if cid else 'msg_wecom_time')
        params = args
    sql = f'''SELECT {COLUMNS} FROM {source} WHERE {' AND '.join(clauses)}
        ORDER BY m.timestamp DESC,m.id DESC LIMIT ? OFFSET ?'''
    try:
        return db.execute(sql, params + [limit, offset]).fetchall()
    except sqlite3.OperationalError as error:
        if not candidates or not _unsupported(error):
            raise
        # An optional tokenizer may be absent after changing the Python runtime.
        source = 'messages m INDEXED BY ' + ('msg_timeline' if cid else 'msg_wecom_time')
        return db.execute(f'''SELECT {COLUMNS} FROM {source} WHERE {' AND '.join(clauses)}
            ORDER BY m.timestamp DESC,m.id DESC LIMIT ? OFFSET ?''', args + [limit, offset]).fetchall()


def message_page(store, *, conversation_id='', query='', offset=0, before=None, anchor=None, limit=50):
    """Return newest-first rows; ``before`` is the previous page's last key.

    Old OFFSET callers remain supported. Anchors select at most 20 immediately
    newer records plus the anchor and older records, without counting history.
    The anchor response's offset is local page length; cursor clients should use
    next_cursor, since deriving a global offset would require the removed scan.
    """
    if not isinstance(conversation_id, str) or len(conversation_id) > 400:
        raise ValueError('会话编号无效')
    if not isinstance(query, str) or len(query) > 200:
        raise ValueError('搜索内容最多200字')
    if not _integer(offset, 0, 1000000) or not _integer(limit, 1, 100):
        raise ValueError('分页参数无效')
    if before is not None:
        if not isinstance(before, (tuple, list)) or len(before) != 2 or not _integer(before[0], -(2**63), 2**63-1) or not _integer(before[1], 1, 2**63-1):
            raise ValueError('消息游标无效')
        if offset:
            raise ValueError('游标与偏移不能同时使用')
    if anchor is not None and not _integer(anchor, 1, 2**63-1):
        raise ValueError('来源消息编号无效')
    with store.connect() as db:
        # Keep the candidate state, pending IDs and message rows in one SQLite
        # snapshot while the background index commits independently.
        db.execute('BEGIN')
        if anchor is not None:
            target = db.execute("SELECT conversation_id,timestamp FROM messages WHERE id=? AND platform='wecom'", (anchor,)).fetchone()
            if target is None:
                raise LookupError('来源消息不存在')
            conversation_id, query = target['conversation_id'], ''
            newer = db.execute(f'''SELECT {COLUMNS} FROM messages m INDEXED BY msg_timeline
                WHERE m.platform='wecom' AND m.conversation_id=? AND (m.timestamp,m.id)>(?,?)
                ORDER BY m.timestamp,m.id LIMIT ?''', (conversation_id, target['timestamp'], anchor, min(20, limit-1))).fetchall()
            # Include the anchor without adding one to a possibly maximal int64.
            older = db.execute(f'''SELECT {COLUMNS} FROM messages m INDEXED BY msg_timeline
                WHERE m.platform='wecom' AND m.conversation_id=? AND (m.timestamp,m.id)<=(?,?)
                ORDER BY m.timestamp DESC,m.id DESC LIMIT ?''', (conversation_id, target['timestamp'], anchor, limit-len(newer)+1)).fetchall()
            rows = list(reversed(newer)) + list(older)
            offset = 0
        else:
            rows = _query(db, conversation_id, query, before, limit+1, offset)
    shown = [dict(row) for row in rows[:limit]]
    last = shown[-1] if shown else None
    return dict(messages=shown, conversation_id=conversation_id, has_more=len(rows)>limit,
        next_offset=offset+limit, next_cursor=dict(timestamp=last['timestamp'], id=last['id']) if last else None)
