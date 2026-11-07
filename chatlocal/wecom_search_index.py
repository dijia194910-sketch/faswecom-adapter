"""Optional substring candidates, rebuilt in short, resumable transactions.

The original SQLite ``instr(lower(content), lower(query))`` is always the final
predicate. The index contains no second copy of message text. Triggers only
queue IDs, so importing a batch does not tokenize its entire contents under the
importer's write lock. Until the legacy backfill finishes, searches retain the
original scan; a small queue and unindexable documents are included thereafter.
"""
import sqlite3
import threading
import time


BATCH_ROWS = 64
BATCH_CHARS = 65536
MAX_CONTENT_CHARS = 65536
MAX_DIRTY_SEARCH = 256
ANALYZE_CHANGES = 50000
ANALYZE_INTERVAL = 60
_manager_lock = threading.RLock()


def _unsupported(error):
    text = str(error).lower()
    return any(value in text for value in (
        'no such module', 'no such tokenizer', 'unrecognized option',
        'error in tokenizer constructor', 'no such table: wecom_substring_fts'))


def initialize(db):
    # This migration never reads old message bodies or populates the FTS table.
    db.executescript('''
      CREATE TABLE IF NOT EXISTS wecom_search_state(
        id INTEGER PRIMARY KEY CHECK(id=1), available INTEGER NOT NULL DEFAULT 0,
        backfill_id INTEGER NOT NULL DEFAULT 0, target_id INTEGER NOT NULL,
        complete INTEGER NOT NULL DEFAULT 0, stats_water INTEGER NOT NULL DEFAULT -1,
        stats_at REAL NOT NULL DEFAULT 0);
      INSERT OR IGNORE INTO wecom_search_state(id,target_id,complete)
        SELECT 1,coalesce(max(id),0),CASE WHEN count(*)=0 THEN 1 ELSE 0 END
        FROM (SELECT id FROM messages ORDER BY id DESC LIMIT 1);
      CREATE TABLE IF NOT EXISTS wecom_search_dirty(message_id INTEGER PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS wecom_search_unindexed(message_id INTEGER PRIMARY KEY);
      CREATE TRIGGER IF NOT EXISTS wecom_search_insert AFTER INSERT ON messages
        WHEN NEW.platform='wecom' BEGIN
        INSERT OR IGNORE INTO wecom_search_dirty VALUES(NEW.id);
      END;
      CREATE TRIGGER IF NOT EXISTS wecom_search_delete AFTER DELETE ON messages
        WHEN OLD.platform='wecom' BEGIN
        INSERT OR IGNORE INTO wecom_search_dirty VALUES(OLD.id);
      END;
      CREATE TRIGGER IF NOT EXISTS wecom_search_update AFTER UPDATE OF id,content,platform ON messages
        WHEN (OLD.platform='wecom' OR NEW.platform='wecom') AND
          (OLD.id IS NOT NEW.id OR OLD.content IS NOT NEW.content OR OLD.platform IS NOT NEW.platform) BEGIN
        INSERT OR IGNORE INTO wecom_search_dirty VALUES(OLD.id);
        INSERT OR IGNORE INTO wecom_search_dirty VALUES(NEW.id);
      END;
    ''')
    existed = db.execute("SELECT 1 FROM sqlite_master WHERE name='wecom_substring_fts'").fetchone()
    try:
        db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS wecom_substring_fts USING fts5(
            body,content='',contentless_delete=1,tokenize='trigram case_sensitive 1')""")
        # Force an existing virtual table to initialize on this SQLite runtime.
        db.execute('SELECT rowid FROM wecom_substring_fts WHERE rowid=-1').fetchone()
    except sqlite3.OperationalError as error:
        if not _unsupported(error):
            raise
        db.execute('UPDATE wecom_search_state SET available=0 WHERE id=1')
    else:
        if not existed:
            # Recreating a lost/unsupported optional table must also invalidate
            # its old completion marker, otherwise existing rows disappear from
            # candidate searches until their next edit.
            db.execute('''UPDATE wecom_search_state SET backfill_id=0,
                target_id=(SELECT coalesce(max(id),0) FROM messages),
                complete=NOT EXISTS(SELECT 1 FROM messages) WHERE id=1''')
        db.execute('UPDATE wecom_search_state SET available=1 WHERE id=1')


def candidate_sql(db, query):
    """Return a candidate-ID subquery, or None for the exact scan fallback."""
    if len(query) < 3 or '\0' in query:
        return None
    try:
        state = db.execute('SELECT available,complete FROM wecom_search_state WHERE id=1').fetchone()
    except sqlite3.OperationalError as error:
        if 'no such table: wecom_search_state' not in str(error):
            raise
        # Read-only archives may predate the optional migration.
        return None
    if not state or not state[0] or not state[1]:
        return None
    # Large imports should not construct a million-row UNION just to reproduce
    # the fallback scan. This probe reads at most 257 integer keys.
    if db.execute('SELECT 1 FROM wecom_search_dirty LIMIT 1 OFFSET ?', (MAX_DIRTY_SEARCH,)).fetchone():
        return None
    lowered = db.execute('SELECT lower(?)', (query,)).fetchone()[0]
    match = '"' + lowered.replace('"', '""') + '"'
    return ('''SELECT rowid id FROM wecom_substring_fts WHERE wecom_substring_fts MATCH ?
        UNION SELECT message_id FROM wecom_search_dirty
        UNION SELECT message_id FROM wecom_search_unindexed''', [match])


def _index_row(db, row):
    mid, platform, content = row
    db.execute('DELETE FROM wecom_substring_fts WHERE rowid=?', (mid,))
    db.execute('DELETE FROM wecom_search_unindexed WHERE message_id=?', (mid,))
    if platform == 'wecom':
        if content is None:
            db.execute('INSERT OR IGNORE INTO wecom_search_unindexed VALUES(?)', (mid,))
        else:
            db.execute('INSERT INTO wecom_substring_fts(rowid,body) VALUES(?,?)', (mid, content))
    db.execute('DELETE FROM wecom_search_dirty WHERE message_id=?', (mid,))


_BODY = f'''CASE WHEN m.platform='wecom' AND length(m.content)<={MAX_CONTENT_CHARS}
    AND instr(m.content,char(0))=0 THEN lower(m.content) ELSE NULL END'''


def maintain_batch(db, *, backfill=False, batch_rows=BATCH_ROWS):
    """Index one bounded batch. Caller owns a short IMMEDIATE transaction."""
    state = db.execute('SELECT available,backfill_id,target_id,complete FROM wecom_search_state WHERE id=1').fetchone()
    if not state or not state[0]:
        return False
    legacy = backfill and not state[3]
    if legacy:
        # Rowid traversal handles mixed-platform stores and gaps without a sort.
        rows = db.execute(f'''SELECT m.id,m.platform,{_BODY} FROM messages m NOT INDEXED
            WHERE m.id>? AND m.id<=? ORDER BY m.id LIMIT ?''', (state[1], state[2], batch_rows))
    else:
        rows = db.execute(f'''SELECT d.message_id,m.platform,{_BODY} FROM wecom_search_dirty d
            LEFT JOIN messages m ON m.id=d.message_id ORDER BY d.message_id LIMIT ?''', (batch_rows,))
    # Read a limited batch before writing the same queue/table being iterated.
    selected, characters = [], 0
    for row in rows:
        selected.append(tuple(row))
        characters += len(row[2] or '')
        if characters >= BATCH_CHARS:
            break
    rows.close()
    for row in selected:
        _index_row(db, row)
    if legacy:
        if selected:
            db.execute('UPDATE wecom_search_state SET backfill_id=? WHERE id=1', (selected[-1][0],))
        else:
            db.execute('UPDATE wecom_search_state SET backfill_id=target_id,complete=1 WHERE id=1')
            return True
    if not selected and not legacy and not state[3]:
        return maintain_batch(db, backfill=True, batch_rows=batch_rows)
    return bool(selected)


def maintain_statistics(db, *, now=None):
    """Bounded ANALYZE, at most once a minute and every 50k journal changes."""
    if sqlite3.sqlite_version_info < (3, 32, 0):
        return False
    now = time.time() if now is None else now
    state = db.execute('SELECT stats_water,stats_at FROM wecom_search_state WHERE id=1').fetchone()
    water = db.execute('SELECT high_water FROM local_change_meta WHERE id=1').fetchone()[0]
    if not water or (state[0] >= 0 and (water - state[0] < ANALYZE_CHANGES or now - state[1] < ANALYZE_INTERVAL)):
        return False
    db.execute('PRAGMA analysis_limit=400')
    db.execute('ANALYZE messages')
    db.execute('ANALYZE wecom_chat_index')
    db.execute('UPDATE wecom_search_state SET stats_water=?,stats_at=? WHERE id=1', (water, now))
    return True


class Maintenance:
    def __init__(self, path):
        self.path = path
        self.users = 0
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name='wecom-search-index')

    def request(self):
        self.wake.set()

    def _run(self):
        backfill = False
        db = None
        try:
            while not self.stop.is_set():
                worked = False
                try:
                    if db is None:
                        # Do not recreate a temporary/account database removed
                        # while its owner is shutting down.
                        db = sqlite3.connect(self.path.as_uri()+'?mode=rw', uri=True, timeout=.05)
                    with db:
                        db.execute('BEGIN IMMEDIATE')
                        worked = maintain_batch(db, backfill=backfill)
                        maintain_statistics(db)
                except (OSError, sqlite3.Error):
                    # The index is optional; failures cannot break searches/imports.
                    worked = False
                if not worked and db is not None:
                    db.close()
                    db = None
                # Reuse the connection only across active batches. Every batch
                # commits before yielding, so imports retain a short lock wait;
                # reuse avoids a WAL checkpoint/schema reload per 64 messages.
                backfill = not backfill
                self.wake.wait(.01 if worked else 5)
                self.wake.clear()
        finally:
            if db is not None:
                db.close()


def start_maintenance(store):
    if store.readonly:
        return None
    with _manager_lock:
        manager = getattr(store, '_query_maintenance', None)
        if manager is None or manager.stop.is_set():
            manager = Maintenance(store.path)
            store._query_maintenance = manager
            manager.users = 1
            manager.thread.start()
        else:
            manager.users += 1
        return manager


def stop_maintenance(manager):
    if manager is None:
        return
    with _manager_lock:
        manager.users -= 1
        if manager.users:
            return
        manager.stop.set()
        manager.wake.set()
    # Returning from close means the worker's connection has actually closed.
    # Each transaction is bounded and SQLite's busy timeout is only 50 ms; a
    # timeout here would allow an unlucky slow disk commit to outlive teardown.
    manager.thread.join()
