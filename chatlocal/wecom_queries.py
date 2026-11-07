"""Cache summaries until messages or names change, not optional index writes."""
import sqlite3
import threading


class WeComQueries:
    def __init__(self, store):
        self.store=store
        self.path=store.path.resolve()
        self.db=None
        self.lock=threading.RLock()
        self.version=None
        self.content_version=None
        self.cache={}
        # Application factories and read-only callers do not own a background
        # lifetime. ASGI lifespan calls start()/close() as a matched pair.
        self.maintenance=None

    def start(self):
        from .wecom_search_index import start_maintenance
        with self.lock:
            if self.maintenance is None:
                self.maintenance=start_maintenance(self.store)

    def read(self,key,sql,args=()):
        with self.lock:
            if self.db is None:
                self.db=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,check_same_thread=False)
                self.db.row_factory=sqlite3.Row
            version=self.db.execute('PRAGMA data_version').fetchone()[0]
            if version!=self.version:
                # Background FTS batches and ANALYZE change data_version too.
                # The transactional journal covers message/name edits, so index
                # maintenance must not flush otherwise unchanged UI summaries.
                content_version=tuple(self.db.execute('''SELECT epoch,high_water,
                    (SELECT coalesce(sum(revision),0) FROM wecom_names_state)
                    FROM local_change_meta WHERE id=1''').fetchone())
                if content_version!=self.content_version:
                    self.cache.clear();self.content_version=content_version
                self.version=version
            if key not in self.cache:
                if len(self.cache)>=32:self.cache.clear()
                self.cache[key]=[dict(r) for r in self.db.execute(sql,args)]
            return [dict(row) for row in self.cache[key]]

    def summary(self):
        return self.read('summary',"SELECT coalesce(sum(count),0) messages,count(*) conversations,(SELECT coalesce(max(id),0) FROM messages WHERE platform='wecom') revision,max(latest) latest,(SELECT coalesce(sum(revision),0) FROM wecom_names_state) names_revision FROM wecom_chat_index")[0]

    def conversations(self,query):
        return self.read(('conversations',query),"""SELECT conversation_id,name,count,latest,kind FROM wecom_chat_index
            WHERE instr(lower(name),lower(?))>0 ORDER BY latest DESC,latest_id DESC LIMIT 500""",(query,))

    def close(self):
        from .wecom_search_index import stop_maintenance
        with self.lock:
            if self.db is not None:self.db.close();self.db=None
            self.cache.clear();self.version=None;self.content_version=None
            maintenance=self.maintenance;self.maintenance=None
        stop_maintenance(maintenance)
