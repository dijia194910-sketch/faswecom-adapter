"""WeCom workbench using Tulpa's scoped retrieval, Agent and session storage.

Model runs require an explicit per-request opt-in. Sending is a separate UI
action; the read-only Agent never receives a send or authorization tool.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

from .agent_sessions import Sessions
from .config import ROOT, settings
from .agent_profiles import profile_catalog, profile_config
from .desktop_routes import read_product_settings, save_product_settings
from .normalize import display_time
from .retrieval import make_plan, retrieve
from .wecom_adapter import configuration, initialize_config, text_message
from .wecom_models import ModelCatalog
from .wecom_recipients import Recipients


class Console:
    def __init__(self, store, *, root=ROOT, runner=None, adapter_call=None, adapter_url='http://127.0.0.1:19091', isolated_settings=False):
        self.store, self.root = store, Path(root)
        self.isolated_settings=isolated_settings
        self.sessions = Sessions(store.path.parent / 'wecom-agent-sessions.sqlite3')
        self.config_path = store.path.parent / 'wecom-adapter.json'
        self.models = ModelCatalog(store.path.parent / 'wecom-models.json')
        initialize_config(self.config_path)
        self.runner = runner
        self.adapter_call = adapter_call or self.call_adapter
        self.adapter_url = adapter_url
        self.jobs, self.lock, self.config_lock = {}, threading.Lock(), threading.Lock()
        with self.sessions.connect() as db:
            db.execute("UPDATE turns SET status='interrupted' WHERE status='running'")
        from .wecom_dialogue import SendDialogue
        self.dialogue=SendDialogue(self)

    def call_adapter(self, action, params):
        cfg = configuration(self.config_path)
        try:
            with httpx.Client(base_url=self.adapter_url, timeout=105, trust_env=False) as client:
                response = client.post('/'+action, json=params, headers={'Authorization':'Bearer '+cfg['token']})
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError):
            # A timeout may follow a successful send. The browser keeps its key.
            raise ValueError('适配器未返回可用结果。若已发起发送，请核对企微，保留本次编号，勿新建编号重发。') from None

    def model_config(self,model_id=None):
        if self.root == ROOT:
            base=settings()
        else:
            from dotenv import dotenv_values
            base=settings()
            if self.isolated_settings:base.update(API_BASE='',API_KEY='',MODEL='')
            base.update({k:v for k,v in dotenv_values(self.root/'.env').items() if v is not None})
        return self.models.config(base,model_id)

    def status(self):
        cfg = configuration(self.config_path)
        product = read_product_settings(self.root,inherit_environment=not self.isolated_settings)
        catalog=self.models.public()
        active=next((m for m in catalog['models'] if m['id']==catalog['active']),None)
        model={k:product[k] for k in ('api_base','model','has_api_key','configured')} if active is None else {k:active[k] for k in ('api_base','model','has_api_key','configured')}
        if active is None and catalog.get('managed'):
            model=dict(api_base='',model='',has_api_key=False,configured=False)
        return dict(model=model,model_catalog=catalog,
            agent_available=self.runner is not None or importlib.util.find_spec('deepseek_harness') is not None,
            profiles=profile_catalog(), send_enabled=cfg.get('send_enabled') is True,
            ai_takeover=cfg.get('ai_takeover') is not False,
            backend=cfg.get('backend'), targets=[dict(id=k, **v) for k,v in cfg['targets'].items()],
            blocked=[dict(b) for b in (cfg.get('blocked') or []) if isinstance(b,dict)],
            active_job=next((j['id'] for j in self.jobs.values() if j['state']=='running'), None))

    def model_balance(self, model_id):
        catalog=self.models.public()
        entry=next((m for m in catalog['models'] if m['id']==model_id),None)
        if entry is None:raise ValueError('模型不存在')
        if not entry.get('has_api_key'):
            return dict(supported=False,reason='尚未配置 API Key')
        if entry.get('provider')!='deepseek':
            return dict(supported=False,reason='该接口类型不提供余额查询')
        data=self.models.read()
        key=self.models.key(data,entry.get('credential_id') or entry['provider'])
        import httpx
        try:
            with httpx.Client(timeout=8,trust_env=False) as client:
                response=client.get(entry['api_base'].rstrip('/')+'/user/balance',headers={'Authorization':'Bearer '+key})
                response.raise_for_status()
                payload=response.json()
        except (httpx.HTTPError,ValueError):
            raise ValueError('余额查询失败，请检查网络或 API 地址') from None
        infos=payload.get('balance_infos') or []
        info=next((x for x in infos if x.get('currency')=='CNY'),infos[0] if infos else {})
        return dict(supported=True,available=payload.get('is_available'),currency=info.get('currency',''),total=info.get('total_balance'),granted=info.get('granted_balance'),topped_up=info.get('topped_up_balance'))

    def plan(self, body):
        question = body.get('question', '')
        ids = body.get('conversations', [])
        if not isinstance(ids, list) or len(ids)>500 or any(not isinstance(v,str) or len(v)>400 for v in ids):
            raise ValueError('会话范围无效')
        known = {r['conversation_id'] for r in self.store.conversations() if r['platform']=='wecom'}
        if not set(ids)<=known: raise ValueError('所选会话不在企微资料中')
        for field in ('question','start','end','keywords'):
            if not isinstance(body.get(field,''),str) or len(body.get(field,''))>2000:
                raise ValueError('问题或筛选参数无效')
        selected = [json.dumps(['wecom', cid]) for cid in ids]
        return make_plan(question, ['wecom'], selected, body.get('start',''), body.get('end',''), body.get('keywords',''))

    def start(self, body):
        if set(body)-{'question','conversations','start','end','keywords','session_id','profile','use_model','reply_message_id','model_id','intent_model'}:
            raise ValueError('不支持的查询参数')
        if 'intent_model' in body and (not isinstance(body['intent_model'],str) or len(body['intent_model'])>200):
            raise ValueError('意图识别模型名称无效')
        plan = self.plan(body)
        use_model = body.get('use_model') is True
        cfg = profile_config(self.model_config(body.get('model_id')) if use_model else {}, body.get('profile','deep'))
        if cfg.get('MODEL_OUTPUT_LIMIT'):
            cfg['AGENT_MAX_OUTPUT_TOKENS']=str(min(int(cfg['AGENT_MAX_OUTPUT_TOKENS']),int(cfg['MODEL_OUTPUT_LIMIT'])))
        if use_model and not cfg.get('API_KEY'): raise ValueError('尚未配置模型，请先保存模型设置；可先使用本地检索')
        if use_model and self.runner is None and importlib.util.find_spec('deepseek_harness') is None:
            raise ValueError('Agent 运行依赖尚未安装，请运行启动脚本更新依赖')
        context = None
        mid = body.get('reply_message_id')
        if mid is not None:
            if not use_model or type(mid) is not int: raise ValueError('回复起草需要选择原始消息并明确调用模型')
            with self.store.connect() as db:
                row = db.execute("SELECT conversation_id FROM messages WHERE id=? AND platform='wecom'", (mid,)).fetchone()
            if row is None or body.get('conversations') != [row['conversation_id']]:
                raise ValueError('回复草稿仅允许读取所选消息所在的一个会话')
            context = dict(message_id=mid, instruction=body['question'], tulpa_disabled=True)
        sid = body.get('session_id')
        if sid is not None and (not isinstance(sid,str) or not re.fullmatch('[a-f0-9]{32}',sid)):
            raise ValueError('对话编号无效')
        with self.lock:
            if any(j['state']=='running' for j in self.jobs.values()): raise ValueError('已有任务运行中，请等待或停止')
            sid = sid or self.sessions.create()
            memory = {} if context is not None else self.sessions.memory(sid)
            memory.update(read_only=True, keyword_hint=body.get('keywords',''))
            tid = self.sessions.start(sid, body['question'].strip(), dict(body, platforms=['wecom']))
            jid = uuid.uuid4().hex
            job = dict(id=jid, session_id=sid, turn_id=tid, state='running', steps=[], stop=threading.Event(), created=time.time())
            self.jobs[jid] = job
            for old in list(self.jobs):
                if len(self.jobs)>50 and self.jobs[old]['state']!='running': self.jobs.pop(old)

        def work():
            try:
                if not use_model:
                    bundle = retrieve(self.store, plan)
                    record = dict(result=dict(claims=[], local_only=True, api_called=False), bundle=bundle, requests=0)
                    state = 'completed'
                else:
                    if self.runner is None:
                        from .agent import run_agent
                        runner = run_agent
                    else: runner = self.runner
                    record, state = None, 'error'
                    for event in runner(self.store, plan, body['question'], memory, job['stop'], config=cfg,
                                        reply_context=context, include_reasoning=False):
                        if event['type']=='done':
                            record, state = event['record'], event['status']
                            # The Agent also sets its stop event during cleanup.
                            # Capture user cancellation before advancing the generator.
                            if job['stop'].is_set(): state='cancelled'
                        elif event['type']=='started':
                            job['steps'].append('正在启动分析 Agent…')
                        elif event['type']=='model_start':
                            job['steps'].append('正在分析并核对证据…')
                        elif event['type']=='tool_start':
                            from .agent_tools import LABELS
                            job['steps'].append(LABELS.get(event.get('name'),'正在读取资料'))
                        job['steps'] = job['steps'][-30:]
                    if record is None: raise ValueError('本轮未取得完整结果')
                if not use_model and job['stop'].is_set(): state='cancelled'
                self.sessions.finish(tid, state, record)
                job['state'] = state
            except Exception as exc:
                message = str(exc) if isinstance(exc,ValueError) else '本轮运行失败，请检查模型配置或缩小范围后重试'
                self.sessions.finish(tid, 'error', dict(result=dict(error=message, claims=[])))
                job.update(state='error', error=message)
        threading.Thread(target=work, daemon=True, name='wecom-console').start()
        return dict(job_id=jid, session_id=sid)

    def history(self, sid):
        turns = []
        for turn in self.sessions.history(sid):
            record = turn['record']; bundle = record.get('bundle', {})
            messages = [dict(id=m['id'], sender=m.get('sender',''), conversation=m.get('conversation',''),
                content=m.get('content',''), time=display_time(m['timestamp']), is_self=m.get('is_self'),
                conversation_id=m['conversation_id'],conversation_type=m.get('conversation_type')) for m in bundle.get('messages',[]) if m.get('platform')=='wecom']
            turns.append(dict(id=turn['id'], question=turn['question'], status=turn['status'], options=turn['options'],
                result=record.get('result',{}), messages=messages, requests=record.get('requests',0),
                scope_count=bundle.get('scope_count'), incomplete=bundle.get('incomplete',True)))
        return turns

    def manage_session(self, sid, action):
        if action not in ('archive','restore','delete'):
            raise ValueError('对话操作无效')
        with self.lock, self.dialogue.guard, self.sessions.connect() as db:
            if any(j['state']=='running' and j['session_id']==sid for j in self.jobs.values()):
                raise ValueError('请先停止当前分析，再管理这条对话。')
            flow=self.dialogue.get(sid)
            if flow and flow['phase']=='sending':
                raise ValueError('消息正在发送，请等待回执后再管理对话。')
            if db.execute('SELECT 1 FROM sessions WHERE id=?',(sid,)).fetchone() is None:
                raise ValueError('对话不存在，可能已被删除。')
            if action=='delete':
                db.execute('DELETE FROM send_flows WHERE session_id=?',(sid,))
                db.execute('DELETE FROM turns WHERE session_id=?',(sid,))
                db.execute('DELETE FROM sessions WHERE id=?',(sid,))
                for jid in list(self.jobs):
                    if self.jobs[jid]['session_id']==sid:self.jobs.pop(jid)
            else:
                db.execute('UPDATE sessions SET archived=? WHERE id=?',(int(action=='archive'),sid))
        return dict(ok=True,action=action)

    def authorize(self, body):
        if set(body)-{'name','kind','backend','approved'} or body.get('approved') is not True:
            raise ValueError('请先确认接收目标和发送方式')
        name, kind, backend = body.get('name'), body.get('kind'), body.get('backend')
        if not isinstance(name,str) or not 1<=len(name.strip())<=100 or any(ord(c)<32 for c in name):
            raise ValueError('请填写准确的企微显示名')
        if kind not in ('private','group') or backend not in ('native','desktop_ocr','background_window'):
            raise ValueError('目标类型或发送方式无效')
        name=name.strip(); key='ui-'+hashlib.sha256((kind+':'+name).encode()).hexdigest()[:20]
        with self.config_lock:
            cfg=configuration(self.config_path)
            from .wecom_desktop import display_name
            if any(k!=key and display_name(v['name'])==display_name(name) for k,v in cfg['targets'].items()):
                raise ValueError('已有同名目标，无法唯一核对；请检查本地目标配置')
            cfg['targets'][key]=dict(name=name, kind=kind, enabled=True)
            cfg.update(send_enabled=True, backend=backend)
            self.save_adapter(cfg)
        return dict(target_id=key)

    def set_ai_takeover(self, body):
        """智能客服接管总开关：关掉后自动回复系统不再回复客户。"""
        if set(body) - {'enabled'} or not isinstance(body.get('enabled'), bool):
            raise ValueError('接管开关参数无效')
        with self.config_lock:
            cfg = configuration(self.config_path)
            cfg['ai_takeover'] = body['enabled']
            self.save_adapter(cfg)
        return dict(ai_takeover=body['enabled'])

    def save_adapter(self, cfg):
        temporary=self.config_path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
        try:
            temporary.write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding='utf-8')
            os.replace(temporary,self.config_path)
        finally: temporary.unlink(missing_ok=True)

    def set_send_enabled(self, body):
        """全局发送总开关：关闭时同时撤销所有 MCP 连接的代发权限。"""
        if set(body) - {'enabled'} or type(body.get('enabled')) is not bool:
            raise ValueError('发送开关参数无效')
        enabled = body['enabled']
        with self.config_lock:
            cfg = configuration(self.config_path)
            if enabled:
                if cfg.get('backend') not in ('native', 'desktop_ocr', 'background_window'):
                    raise ValueError('请先添加一个手动发送目标并选择发送方式')
                cfg['send_enabled'] = True
            else:
                cfg['send_enabled'] = False
                cfg['send_generation'] = cfg.get('send_generation', 0) + 1
                from .mcp_access import MCPAccess
                access = MCPAccess(self.store)
                with access.connect() as db:
                    for row in db.execute('SELECT id,scope FROM grants WHERE revoked=0').fetchall():
                        scope = json.loads(row['scope'])
                        if 'wecom' in scope.get('platforms', []) and scope.get('send'):
                            scope['send'] = False
                            db.execute('UPDATE grants SET scope=? WHERE id=?', (json.dumps(scope, ensure_ascii=False), row['id']))
            self.save_adapter(cfg)
        return dict(send_enabled=enabled)

    def set_target_enabled(self, target_id, body):
        if set(body) - {'enabled'} or type(body.get('enabled')) is not bool:
            raise ValueError('目标状态参数无效')
        with self.config_lock:
            cfg = configuration(self.config_path)
            target = cfg['targets'].get(target_id)
            if target is None:
                raise ValueError('目标不存在')
            target['enabled'] = body['enabled']
            self.save_adapter(cfg)
        return dict(target_id=target_id, enabled=body['enabled'])

    def delete_target(self, target_id, body):
        if body:
            raise ValueError('参数无效')
        with self.config_lock:
            cfg = configuration(self.config_path)
            if target_id not in cfg['targets']:
                raise ValueError('目标不存在')
            cfg['targets'].pop(target_id)
            self.save_adapter(cfg)
        return dict(deleted=True)

    def recipient_choices(self):
        from .wecom_desktop import display_name
        rows = Recipients(self.store).rows()
        out = [dict(id=r['id'], name=r['name'], kind=r['kind'], account=r.get('account') or '')
               for r in rows if r.get('id') and r.get('name')]
        out.sort(key=lambda x: (x['kind'] != 'private', display_name(x['name'])))
        return dict(recipients=out[:2000], total=len(out))

    def add_blocked(self, body):
        from .wecom_desktop import display_name
        rows = Recipients(self.store).rows()
        if set(body) == {'id'}:
            match = next((r for r in rows if r.get('id') == body['id']), None)
            if match is None:
                raise ValueError('联系人不存在，请刷新列表后重试')
        elif set(body) - {'name', 'kind'} == set() and body.get('kind') in ('private', 'group'):
            name = body.get('name')
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100 or any(ord(c) < 32 for c in name):
                raise ValueError('请填写准确的企微显示名')
            matches = [r for r in rows if display_name(r['name']) == display_name(name.strip()) and r['kind'] == body['kind']]
            if not matches:
                raise ValueError('未找到该联系人，请先同步聊天或填写准确的显示名')
            if len(matches) > 1:
                raise ValueError('存在同名联系人，请先在企微里设置唯一备注名')
            match = matches[0]
        else:
            raise ValueError('参数无效')
        entry = dict(id=match['id'], name=match['name'], kind=match['kind'], native_id=match.get('native_id') or '')
        with self.config_lock:
            cfg = configuration(self.config_path)
            blocked = [b for b in (cfg.get('blocked') or []) if isinstance(b, dict) and b.get('id') != entry['id']]
            blocked.append(entry)
            cfg['blocked'] = blocked
            self.save_adapter(cfg)
        return dict(blocked=blocked)

    def remove_blocked(self, target_id, body):
        if body:
            raise ValueError('参数无效')
        with self.config_lock:
            cfg = configuration(self.config_path)
            cfg['blocked'] = [b for b in (cfg.get('blocked') or []) if isinstance(b, dict) and b.get('id') != target_id]
            self.save_adapter(cfg)
            blocked = cfg['blocked']
        return dict(blocked=blocked)

    def send(self, body):
        if set(body)-{'target_id','message','idempotency_key','approved'} or body.get('approved') is not True:
            raise ValueError('请在操作台核对接收人和正文后点击确认发送')
        cfg=configuration(self.config_path)
        target=cfg['targets'].get(body.get('target_id'))
        if not target or target.get('enabled') is not True or cfg.get('send_enabled') is not True:
            raise ValueError('当前接收目标未授权发送')
        message=text_message(body.get('message'))
        private=target['kind']=='private'
        return self.adapter_call('send_private_msg' if private else 'send_group_msg',
            dict({'user_id' if private else 'group_id':body['target_id']},message=message,idempotency_key=body.get('idempotency_key')))


def install_console_routes(app, store, local, *, root=ROOT, runner=None, adapter_call=None, adapter_url='http://127.0.0.1:19091', isolated_settings=False):
    console=Console(store,root=root,runner=runner,adapter_call=adapter_call,adapter_url=adapter_url,isolated_settings=isolated_settings)
    app.state.wecom_console=console
    def guarded(fn,*args):
        try: return fn(*args)
        except (ValueError,TypeError,KeyError) as exc:
            raise HTTPException(400,str(exc) if isinstance(exc,ValueError) else '参数无效') from None

    @app.get('/console')
    def page(request:Request):
        local(request)
        return FileResponse(ROOT/'web/wecom-console.html',headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer',
            'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'"})

    @app.get('/api/wecom/console/status')
    def status(request:Request): local(request);return guarded(console.status)

    @app.put('/api/wecom/console/ai-takeover')
    def ai_takeover(request:Request,body:dict):
        local(request);return guarded(lambda: console.set_ai_takeover(body))

    @app.put('/api/wecom/console/model')
    def model(request:Request,body:dict):
        local(request,True)
        if set(body)-{'api_base','model','api_key'}: raise HTTPException(400,'模型配置字段无效')
        guarded(lambda:save_product_settings(body,console.root,inherit_environment=not console.isolated_settings))
        return console.status()['model']

    @app.get('/api/wecom/console/sessions')
    def sessions(request:Request,archived:bool=False): local(request);return dict(sessions=console.sessions.choices(archived))

    @app.post('/api/wecom/console/sessions/{sid}/manage')
    def manage_session(request:Request,sid:str,body:dict):
        local(request,True)
        if set(body)!={'action'}:raise HTTPException(400,'对话操作参数无效')
        return guarded(console.manage_session,sid,body['action'])

    @app.put('/api/wecom/console/model-selection')
    def model_selection(request:Request,body:dict):
        local(request,True)
        if set(body)!={'model_id'}:raise HTTPException(400,'模型选择参数无效')
        return guarded(console.models.select,body['model_id'])

    @app.put('/api/wecom/console/model-key')
    def model_key(request:Request,body:dict):
        local(request,True)
        if set(body)!={'model_id','api_key'}:raise HTTPException(400,'模型认证参数无效')
        return guarded(console.models.save_key,body['model_id'],body['api_key'])

    @app.post('/api/wecom/console/models')
    def save_model(request:Request,body:dict):
        local(request,True)
        return guarded(console.models.save_model,body)

    @app.post('/api/wecom/console/models/{model_id}/enabled')
    def model_enabled(request:Request,model_id:str,body:dict):
        local(request,True)
        if set(body)!={'enabled'}:raise HTTPException(400,'启用状态参数无效')
        return guarded(console.models.set_enabled,model_id,body['enabled'])

    @app.get('/api/wecom/console/models/{model_id}/balance')
    def model_balance(request:Request,model_id:str):
        local(request);return guarded(console.model_balance,model_id)

    @app.post('/api/wecom/console/models/{model_id}/delete')
    def delete_model(request:Request,model_id:str,body:dict):
        local(request,True)
        if body:raise HTTPException(400,'模型删除参数无效')
        return guarded(console.models.delete_model,model_id)

    @app.get('/api/wecom/console/sessions/{sid}')
    def history(request:Request,sid:str): local(request);return dict(turns=console.history(sid),send_flow=console.dialogue.get(sid))

    @app.post('/api/wecom/console/send-dialogue')
    def dialogue(request:Request,body:dict):
        local(request,True)
        result=guarded(console.dialogue.turn,body)
        if result.get('handled'):
            result['sessions']=console.sessions.choices()
            result['session']=dict(turns=console.history(result['session_id']),send_flow=result['flow'])
        return result

    @app.get('/api/wecom/console/send-dialogue/{sid}')
    def dialogue_status(request:Request,sid:str): local(request);return guarded(console.dialogue.poll,sid)

    @app.post('/api/wecom/console/jobs')
    def start(request:Request,body:dict): local(request,True);return guarded(console.start,body)

    @app.get('/api/wecom/console/jobs/{jid}')
    def job(request:Request,jid:str):
        local(request)
        value=console.jobs.get(jid)
        if value is None: raise HTTPException(404,'任务不存在，可能已重启；请从历史对话查看已保存结果')
        return {k:v for k,v in value.items() if k!='stop'}

    @app.post('/api/wecom/console/jobs/{jid}/stop')
    def stop(request:Request,jid:str,body:dict):
        local(request,True)
        if jid in console.jobs: console.jobs[jid]['stop'].set()
        return dict(stopping=True)

    @app.get('/api/wecom/console/recipients')
    def recipients(request:Request):
        local(request);return guarded(console.recipient_choices)

    @app.post('/api/wecom/console/blocked')
    def blocked_add(request:Request,body:dict):
        local(request,True);return guarded(console.add_blocked,body)

    @app.post('/api/wecom/console/blocked/{target_id}/delete')
    def blocked_remove(request:Request,target_id:str,body:dict):
        local(request,True);return guarded(console.remove_blocked,target_id,body)

    @app.post('/api/wecom/console/targets')
    def authorize(request:Request,body:dict): local(request,True);return guarded(console.authorize,body)

    @app.put('/api/wecom/console/send-enabled')
    def send_enabled(request:Request,body:dict):
        local(request,True);return guarded(console.set_send_enabled,body)

    @app.post('/api/wecom/console/targets/{target_id}/enabled')
    def target_enabled(request:Request,target_id:str,body:dict):
        local(request,True);return guarded(console.set_target_enabled,target_id,body)

    @app.post('/api/wecom/console/targets/{target_id}/delete')
    def target_delete(request:Request,target_id:str,body:dict):
        local(request,True);return guarded(console.delete_target,target_id,body)

    @app.post('/api/wecom/console/disable-sending')
    def disable(request:Request,body:dict):
        local(request,True)
        with console.config_lock:
            cfg=configuration(console.config_path);cfg['send_enabled']=False
            cfg['send_generation']=cfg.get('send_generation',0)+1
            console.save_adapter(cfg)
            # Stop both outstanding exact-message permits and persistent MCP
            # send grants. Read access remains available on those connections.
            from .mcp_access import MCPAccess
            access=MCPAccess(console.store)
            with access.connect() as db:
                for row in db.execute('SELECT id,scope FROM grants WHERE revoked=0').fetchall():
                    scope=json.loads(row['scope'])
                    if 'wecom' in scope['platforms'] and scope.get('send'):
                        scope['send']=False;db.execute('UPDATE grants SET scope=? WHERE id=?',(json.dumps(scope,ensure_ascii=False),row['id']))
        return dict(send_enabled=False)

    @app.get('/api/wecom/console/send-status')
    def send_status(request:Request,key:str):
        local(request)
        if not re.fullmatch(r'[a-zA-Z0-9_.:-]{8,100}',key):raise HTTPException(400,'发送编号无效')
        return guarded(console.adapter_call,'get_operation_by_key',dict(idempotency_key=key))

    @app.post('/api/wecom/console/send')
    def send(request:Request,body:dict): local(request,True);return guarded(console.send,body)