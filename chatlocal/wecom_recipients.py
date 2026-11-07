"""Recipient suggestions from locally imported identities, never fuzzy auto-send."""
import hashlib
from pathlib import Path
import threading
import time
import re
from .retrieval import Plan, scope_sql
from .wecom_adapter import configuration


def identity(kind, value):
    return kind+'-'+hashlib.sha256(value.encode()).hexdigest()[:24]


class Recipients:
    def __init__(self,store):self.store=store

    def grouped_rows(self,where,args):
        # Reuse grouped metadata between the consecutive lookup/confirmation
        # checks. Main/WAL file changes and a short expiry invalidate results.
        # Scope parameters are part of the key; no unscoped cache for MCP.
        def stamp():
            result=[]
            for path in (self.store.path,Path(str(self.store.path)+'-wal')):
                try:
                    st=path.stat();result.append((st.st_size,st.st_mtime_ns,st.st_ctime_ns))
                except OSError:result.append(None)
            return tuple(result)
        cache=self.store.__dict__.setdefault('_wecom_recipient_cache',dict(lock=threading.RLock(),rows={}))
        key=(where,tuple(args));version=stamp();now=time.monotonic()
        with cache['lock']:
            entry=cache['rows'].get(key)
            if entry and entry[0]==version and entry[1]>now:return [dict(r) for r in entry[2]]
            with self.store.connect() as db:
                rows=[dict(r) for r in db.execute(f'''SELECT sender_id,sender,conversation_id,conversation,conversation_type,
                    max(timestamp) latest FROM messages m WHERE {where} AND platform='wecom' AND is_self=0
                    GROUP BY sender_id,sender,conversation_id ORDER BY latest DESC''',args)]
            if len(cache['rows'])>=4:cache['rows'].clear()
            if stamp()==version:cache['rows'][key]=(version,now+2,rows)
            return [dict(r) for r in rows]

    def rows(self,plan=None,*,hint=None):
        where,args=scope_sql(plan or Plan(platforms=['wecom']))
        if hint and hint.get('account'):
            if hint.get('kind')=='private':
                prefix=hint['account']+':'
                where+=' AND sender_id=? AND conversation_id>=? AND conversation_id<?'
                args=[*args,hint['native_id'],prefix,prefix+'\uffff']
            elif hint.get('kind')=='group':
                where+=' AND conversation_id=?';args=[*args,hint['native_id']]
        people={};groups={}
        rows=self.grouped_rows(where,args)
        for row in rows:
            account=row['conversation_id'].split(':',1)[0]
            if row['sender_id'] and row['sender'] and not row['sender'].isdigit():
                rid=identity('person',account+':'+row['sender_id'])
                person=people.setdefault(rid,dict(id=rid,name=row['sender'],kind='private',
                    native_id=row['sender_id'],account=account,aliases=[],evidence=[],conversation_ids=[]))
                if row['sender'] not in person['aliases']:person['aliases'].append(row['sender'])
                if row['conversation_id'] not in person['conversation_ids']:
                    person['conversation_ids'].append(row['conversation_id'])
                    if len(person['evidence'])<3:person['evidence'].append(row['conversation'])
            if row['conversation_type']=='group':
                rid=identity('group',row['conversation_id'])
                groups.setdefault(rid,dict(id=rid,name=row['conversation'],kind='group',native_id=row['conversation_id'],account=account,
                    aliases=[row['conversation']],evidence=[row['conversation']],conversation_ids=[row['conversation_id']]))
        if plan is None:
            # An existing direct chat can contain only our outgoing messages.
            # Its exact native conversation ID still identifies the peer.
            with self.store.connect() as db:
                chats=db.execute('SELECT conversation_id,name,kind FROM wecom_chat_index').fetchall()
            for chat in chats:
                cid=chat['conversation_id'];account=cid.split(':',1)[0]
                if chat['kind']=='group':
                    rid=identity('group',cid)
                    groups.setdefault(rid,dict(id=rid,name=chat['name'],kind='group',native_id=cid,account=account,aliases=[chat['name']],evidence=[chat['name']],conversation_ids=[cid]))
                    continue
                match=re.fullmatch(r'(\d+):S:(\d+)_(\d+)',cid)
                if not match or account not in match.groups()[1:]:continue
                peer=next((v for v in match.groups()[1:] if v!=account),None)
                if not peer:continue
                rid=identity('person',account+':'+peer)
                person=people.setdefault(rid,dict(id=rid,name=chat['name'],kind='private',native_id=peer,account=account,aliases=[chat['name']],evidence=[chat['name']],conversation_ids=[]))
                if cid not in person['conversation_ids']:person['conversation_ids'].append(cid)
        result=list(people.values())+list(groups.values())
        # Manually verified names remain useful even before their messages have
        # been imported. A restricted MCP grant cannot discover those targets.
        if plan is None:
            cfg=configuration(self.store.path.parent/'wecom-adapter.json')
            for key,target in cfg['targets'].items():
                if any(target['name'] in x['aliases'] and target['kind']==x['kind'] for x in result):continue
                result.append(dict(id='target-'+key,name=target['name'],kind=target['kind'],native_id=key,
                    account='',aliases=[target['name']],evidence=['手工核对的接收目标'],conversation_ids=[]))
        return result

    def search(self,query,plan=None):
        if not isinstance(query,str) or not 1<=len(query.strip())<=100:raise ValueError('请提供姓名、昵称或群名，最多100字')
        query=query.strip().casefold()
        rows=[r for r in self.rows(plan) if any(query in n.casefold() for n in r['aliases'])]
        rows.sort(key=lambda r:(query not in [n.casefold() for n in r['aliases']],r['kind']!='private',r['name']))
        return dict(candidates=rows[:20],has_more=len(rows)>20,
            note='候选来自本机历史记录，昵称可能已变化；同名账号不可合并。确认具体接收人后再发送。')

    def get(self,rid,plan=None,*,hint=None):
        # Chat sends already identify an exact conversation. Resolve existing
        # direct/group identities from the small metadata index, not a GROUP BY
        # over every historical sender. Restricted MCP scopes keep their path.
        if plan is None:
            with self.store.connect() as db:
                chats=db.execute('SELECT conversation_id,name,kind FROM wecom_chat_index ORDER BY latest DESC,latest_id DESC').fetchall()
            matches=[]
            for chat in chats:
                cid=chat['conversation_id'];account=cid.split(':',1)[0]
                if chat['kind']=='group' and rid==identity('group',cid):
                    return dict(id=rid,name=chat['name'],kind='group',native_id=cid,account=account,aliases=[chat['name']],evidence=[chat['name']],conversation_ids=[cid])
                match=re.fullmatch(r'(\d+):S:(\d+)_(\d+)',cid)
                if not match or account not in match.groups()[1:]:continue
                peer=next((v for v in match.groups()[1:] if v!=account),None)
                if peer and rid==identity('person',account+':'+peer):matches.append((chat,account,peer))
            if matches:
                chat,account,peer=matches[0];names=list(dict.fromkeys(c['name'] for c,_,_ in matches))
                return dict(id=rid,name=chat['name'],kind='private',native_id=peer,account=account,aliases=names,evidence=names[:3],conversation_ids=[c['conversation_id'] for c,_,_ in matches])
        row=next((r for r in self.rows(plan,hint=hint) if r['id']==rid),None)
        if row is None:raise ValueError('接收人不在当前允许的资料范围内，请重新搜索')
        return row

    def send_target(self,rid,plan=None,*,backend=None,hint=None):
        row=self.get(rid,plan,hint=hint)
        if plan is not None and plan.conversations and row['kind']=='private':
            # A member seen in an allowed group does not authorize private DM.
            import json
            allowed={json.loads(v)[1] for v in plan.conversations if json.loads(v)[0]=='wecom'}
            with self.store.connect() as db:
                direct={r[0] for r in db.execute("SELECT DISTINCT conversation_id FROM messages WHERE platform='wecom' AND conversation_type='direct' AND sender_id=?",(row['native_id'],))}
            if not (direct & allowed):raise ValueError('此连接只获群内范围，未获该联系人的单聊发送权限')
        name=row['name']
        if not 1<=len(name)<=100 or any(ord(c)<32 for c in name):raise ValueError('该历史显示名无法用于发送，请先在企微核对联系人显示名')
        from .wecom_desktop import display_name
        if backend!='native' and any(r['id']!=rid and display_name(r['name'])==display_name(name) for r in self.rows()):
            raise ValueError('发现不同账号使用同一显示名，当前桌面适配器无法安全区分，请先设置唯一备注名')
        return row
