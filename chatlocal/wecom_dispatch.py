"""Exact-message permits issued by confirmed UI flows or scoped MCP grants.

No provider/Agent can issue a UI confirmation. The worker rechecks revocation
and account/scope immediately before entering text and before clicking send.
"""
from contextlib import contextmanager
import hashlib
import json
import sqlite3
import time
import uuid
from .client_accounts import bound_account
from .wecom_adapter import configuration, text_message
from .wecom_recipients import Recipients


class Dispatches:
    def __init__(self,store):
        self.store=store;self.path=store.path.parent/'wecom-dispatch.sqlite3'
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS permits(id TEXT PRIMARY KEY,owner TEXT NOT NULL,
                request_key TEXT NOT NULL,signature TEXT NOT NULL,recipient TEXT NOT NULL,message TEXT NOT NULL,
                backend TEXT NOT NULL,account TEXT NOT NULL,epoch TEXT NOT NULL,generation INTEGER NOT NULL,
                created REAL NOT NULL, UNIQUE(owner,request_key))''')

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    def access(self):
        from .mcp_access import MCPAccess
        return MCPAccess(self.store)

    def plan(self,owner):
        if owner=='console':return None
        if owner.startswith('auto:'):
            from .wecom_reply import validate_auto
            validate_auto(self.store,owner)
            return None
        if not owner.startswith('mcp:'):raise ValueError('发送权限来源无效')
        access=self.access();grant=access.by_id(owner[4:])
        if not grant['scope'].get('send') or 'wecom' not in grant['scope']['platforms']:raise ValueError('此 MCP 连接未获企微直接发送权限')
        return access.plan(grant)

    def issue_file(self,rid,meta,key):
        from .wecom_chat import ChatFiles
        saved,_=ChatFiles(self.store).get(meta['file_id'])
        if saved!=meta:raise ValueError('附件内容不一致')
        return self.issue(rid,json.dumps(meta,ensure_ascii=False),key,backend='desktop_file',_file=True)

    def issue(self,rid,message,key,*,owner='console',backend=None,_file=False):
        message=text_message(message)
        if not isinstance(key,str) or not 8<=len(key)<=100:raise ValueError('发送编号无效')
        signature=hashlib.sha256(json.dumps([rid,message,backend],ensure_ascii=False).encode()).hexdigest()
        with self.db() as db:
            old=db.execute('SELECT * FROM permits WHERE owner=? AND request_key=?',(owner,key)).fetchone()
        if old:
            if old['signature']!=signature:raise ValueError('同一发送编号不能用于不同接收人或正文')
            return dict(old)
        cfg=configuration(self.store.path.parent/'wecom-adapter.json')
        if cfg.get('send_enabled') is not True:raise ValueError('发送已关闭，请先在操作台重新启用')
        backend=backend or cfg.get('backend')
        if backend not in ('native','desktop_ocr','background_window') and not (backend=='desktop_file' and _file and owner=='console'):raise ValueError('发送方式无效')
        target=Recipients(self.store).send_target(rid,self.plan(owner),backend=backend)
        blocked={b.get('id') for b in (cfg.get('blocked') or []) if isinstance(b,dict)}
        if target.get('id') in blocked:raise ValueError('此联系人已被加入不可发送名单，请到 模型与配置 - 发送目标 修改')
        if owner.startswith('auto:'):
            from .wecom_reply import validate_auto
            validate_auto(self.store,owner,recipient=rid,message=message,backend=backend)
        permit=dict(id=uuid.uuid4().hex,owner=owner,request_key=key,signature=signature,
            recipient=json.dumps(target,ensure_ascii=False),message=message,backend=backend,
            account=bound_account(self.store,'wecom') or '',epoch=self.access().epoch(),
            generation=cfg.get('send_generation',0),created=time.time())
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO permits VALUES('+','.join('?'*len(permit))+')',list(permit.values()))
            saved=dict(db.execute('SELECT * FROM permits WHERE owner=? AND request_key=?',(owner,key)).fetchone())
        if saved['signature']!=signature:raise ValueError('同一发送编号发生冲突')
        return saved

    def get(self,pid,owner=None):
        with self.db() as db:row=db.execute('SELECT * FROM permits WHERE id=?',(pid,)).fetchone()
        if row is None or owner is not None and row['owner']!=owner:raise ValueError('发送操作不存在')
        return dict(row)

    def validate(self,pid):
        row=self.get(pid);cfg=configuration(self.store.path.parent/'wecom-adapter.json')
        if cfg.get('send_enabled') is not True:raise ValueError('发送已关闭，请先在操作台重新启用')
        if time.time()-row['created']>600:raise ValueError('本次发送确认已过期，请重新核对')
        if cfg.get('send_generation',0)!=row['generation']:raise ValueError('发送权限已撤销')
        if row['epoch']!=self.access().epoch() or row['account']!=(bound_account(self.store,'wecom') or ''):
            raise ValueError('企微资料账号已改变，未发送')
        target=json.loads(row['recipient'])
        if row['owner'].startswith('auto:'):
            from .wecom_reply import validate_auto
            validate_auto(self.store,row['owner'],recipient=target['id'],message=row['message'],backend=row['backend'])
        if row['backend']=='desktop_file':
            from .wecom_chat import ChatFiles
            expected=json.loads(row['message']);actual,_=ChatFiles(self.store).get(expected['file_id'])
            if actual!=expected:raise ValueError('附件内容已改变')
        if target.get('id') in {b.get('id') for b in (cfg.get('blocked') or []) if isinstance(b,dict)}:
            raise ValueError('此联系人已被加入不可发送名单，请到 模型与配置 - 发送目标 修改')
        current=Recipients(self.store).send_target(target['id'],self.plan(row['owner']),backend=row['backend'],hint=target)
        if current['name']!=target['name']:raise ValueError('接收人显示名已改变，请重新核对')
        return row,target