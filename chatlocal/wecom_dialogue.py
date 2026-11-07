"""Persistent send dialogue: locate -> choose -> compose -> confirm -> receipt."""
import json
import re
import threading
import uuid
from datetime import datetime
from .normalize import TZ
from .wecom_recipients import Recipients
from .wecom_dispatch import Dispatches
from .wecom_adapter import configuration, text_message


def intent(text):
    match=re.match(r'^(?:(?:请|麻烦)(?:你)?\s*)?(?:(?:帮我|我想要|我想|我要|我需要)\s*)?(?:给|对|向)(.{1,100}?)(?:发送|发)(?:一条|一则|一个|条|则|个)?(?:消息|信息|微信)?[：:，,\s]*(.*)$',text.strip(),re.S)
    if not match:return None
    return match[1].strip(' ：:，,'),match[2].strip()


class SendDialogue:
    def __init__(self,console):
        self.console=console;self.sessions=console.sessions;self.store=console.store
        self.guard=threading.RLock();self.running=set()
        with self.sessions.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS send_flows(session_id TEXT PRIMARY KEY,flow TEXT NOT NULL)')

    def get(self,sid):
        with self.sessions.connect() as db:row=db.execute('SELECT flow FROM send_flows WHERE session_id=?',(sid,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self,sid,flow,question=None):
        # One commit per button action. Previously start, flow and finish each
        # opened a separate write transaction, multiplying slow-disk latency.
        with self.sessions.connect() as db:
            if question is not None:
                row=db.execute('SELECT archived FROM sessions WHERE id=?',(sid,)).fetchone()
                if row is None or row['archived']:raise ValueError('对话已删除或归档，请新建或恢复对话。')
                count=db.execute('SELECT count(*) FROM turns WHERE session_id=?',(sid,)).fetchone()[0]
                now=datetime.now(TZ).isoformat();flow['turn_id']=uuid.uuid4().hex
                db.execute('UPDATE sessions SET updated=?,title=CASE WHEN ?=0 THEN ? ELSE title END WHERE id=?',(now,count,question[:35],sid))
                db.execute('INSERT INTO turns VALUES(?,?,?,?,?,?,?)',(flow['turn_id'],sid,question,'completed',now,json.dumps(dict(send_dialogue=True)),'{}'))
            db.execute('INSERT INTO send_flows VALUES(?,?) ON CONFLICT(session_id) DO UPDATE SET flow=excluded.flow',(sid,json.dumps(flow,ensure_ascii=False)))
            if flow.get('turn_id'):
                db.execute("UPDATE turns SET status='completed',record=? WHERE id=?",(json.dumps(dict(result=dict(send_dialogue=dict(flow))),ensure_ascii=False),flow['turn_id']))
        return dict(handled=True,session_id=sid,flow=flow)

    def preview(self,flow):
        if flow.get('text'):
            text_message(flow['text'])
            flow.update(phase='confirm',message='请核对接收人、正文和发送方式。点击“确认发送”或回复“确认发送”才会执行。')
        else:flow.update(phase='compose',message='已选中 '+flow['recipient']['name']+'。你想发送什么内容？请直接输入正文。')

    def turn(self,body):
        if set(body)-{'session_id','text','action','recipient_id','revision','backend','use_model','model_id'}:raise ValueError('发送对话参数无效')
        sid=body.get('session_id');text=body.get('text','');action=body.get('action','message')
        if sid is not None and (not isinstance(sid,str) or not re.fullmatch('[a-f0-9]{32}',sid)):raise ValueError('对话编号无效')
        if action not in ('message','choose','confirm','cancel','edit','backend','acknowledge','retry'):raise ValueError('发送动作无效')
        if not isinstance(text,str) or len(text)>2200:raise ValueError('输入内容过长')
        text=text.strip()
        initial=self.get(sid) if sid else None
        decision=None
        if body.get('use_model') is True and action=='message' and (not initial or initial['phase'] in ('finished','cancelled')):
            from .wecom_intent import recognize
            decision=recognize(text,self.console.model_config(body.get('model_id')))
        with self.guard:
            if sid:
                with self.sessions.connect() as db:
                    row=db.execute('SELECT archived FROM sessions WHERE id=?',(sid,)).fetchone()
                if row is None or row['archived']:
                    raise ValueError('对话已删除或归档，请新建或恢复对话。')
            flow=self.get(sid) if sid else None
            if decision and flow!=initial:raise ValueError('对话已更新，请重新提交')
            parsed=intent(text) if action=='message' else None
            if decision:
                if decision['action']=='query':return dict(handled=False,intent=decision)
                if decision['action'] in ('clarify','contacts'):
                    sid=sid or self.sessions.create()
                    result=dict(intent_message=decision.get('question') or '请说明你希望执行的操作。',intent=decision)
                    if decision['action']=='contacts':
                        result['contacts']=Recipients(self.store).search(decision['recipient'])['candidates']
                        result['intent_message']='找到以下联系人或群：' if result['contacts'] else '没有找到匹配的人或群，请提供更完整的名字。'
                    tid=self.sessions.start(sid,text,dict(intent_only=True))
                    self.sessions.finish(tid,'completed',dict(result=result,requests=1))
                    return dict(handled=True,session_id=sid,flow=flow)
                parsed=(decision['recipient'],decision['text'])
            if action=='retry':
                if not flow or flow.get('receipt',{}).get('state')!='NOT_SENT' or flow['phase']!='finished':
                    raise ValueError('只有明确未执行发送的消息可以重新准备')
                if body.get('revision')!=flow['revision']:raise ValueError('发送对话已更新，请刷新后再确认')
                flow=dict(flow,id=uuid.uuid4().hex,backend='desktop_ocr')
                flow.pop('receipt',None);flow.pop('dispatch_id',None)
                flow['recipient']=Recipients(self.store).send_target(flow['recipient']['id'])
                self.preview(flow);flow['revision']+=1
                return self.save(sid,flow,'改用前台，重新核对正文')
            if not flow or flow['phase'] in ('finished','cancelled'):
                if not parsed:return dict(handled=False)
                sid=sid or self.sessions.create()
                flow=dict(id=uuid.uuid4().hex,revision=0,phase='choose',query=parsed[0],text=parsed[1],
                    backend='native')
                found=Recipients(self.store).search(flow['query'])
                flow.update(candidates=found['candidates'],message='找到以下候选，请确认接收人。' if found['candidates'] else '没有找到匹配的人或群。请提供更完整的昵称，或先刷新企微资料。')
                if decision:flow['intent']=decision
            else:
                if body.get('revision')!=flow['revision']:raise ValueError('发送对话已更新，请刷新后再确认')
                if flow['phase']=='sending':return dict(handled=True,session_id=sid,flow=flow)
                if flow['phase']=='unknown':
                    if action=='acknowledge' or text in ('我已核对','已核对企微'):
                        flow.update(phase='finished',message='已记录你已核对企微。可以开始新的发送对话。')
                    else:raise ValueError('上次发送结果未知。请先到企微核对，再点击“已核对企微，开始下一条”。')
                elif action=='cancel' or text in ('取消','取消发送','不发了'):
                    flow.update(phase='cancelled',message='已取消本次发送。')
                elif parsed and flow['phase']!='compose':
                    flow.update(id=uuid.uuid4().hex,phase='choose',query=parsed[0],text=parsed[1])
                    flow.pop('recipient',None);flow.pop('dispatch_id',None)
                    found=Recipients(self.store).search(parsed[0]);flow.update(candidates=found['candidates'],message='请确认新的接收人。')
                elif flow['phase']=='choose':
                    candidates=flow['candidates'];rid=body.get('recipient_id') if action=='choose' else None
                    if text in ('是','是的','对','就是这个','就是她','就是他','确认') and len(candidates)==1:rid=candidates[0]['id']
                    number=re.fullmatch(r'(?:第)?([1-9]|1[0-9]|20)(?:个|位)?',text)
                    if number and 0<int(number[1])<=len(candidates):rid=candidates[int(number[1])-1]['id']
                    if rid:
                        if rid not in {c['id'] for c in candidates}:raise ValueError('请选择本次搜索返回的接收人')
                        flow['recipient']=Recipients(self.store).send_target(rid,backend=flow['backend'])
                        self.preview(flow)
                    else:
                        flow.update(query=text,candidates=Recipients(self.store).search(text)['candidates'],message='请确认搜索结果中的接收人。')
                elif flow['phase']=='compose':
                    flow['text']=text_message(text);self.preview(flow)
                elif flow['phase']=='confirm':
                    if action=='backend':
                        if body.get('backend') not in ('native','desktop_ocr','background_window'):raise ValueError('发送方式无效')
                        flow['backend']=body['backend']
                    elif action=='confirm' or text=='确认发送':
                        permit=Dispatches(self.store).issue(flow['recipient']['id'],flow['text'],'dialogue-'+flow['id'],backend=flow['backend'])
                        flow.update(phase='sending',dispatch_id=permit['id'],message='正在准备发送，结果会显示在这条对话中。')
                    elif action=='edit':flow.update(phase='compose',text='',message='请输入修改后的完整正文。')
                    elif text:
                        flow['text']=text_message(re.sub(r'^(?:改成|改为|内容改为)[：:，,\s]*','',text));self.preview(flow)
                    else:raise ValueError('请确认发送，或编辑正文')
            flow['revision']+=1
            answer=self.save(sid,flow,text or {'choose':'确认接收人','confirm':'确认发送','cancel':'取消发送','edit':'修改正文','backend':'修改发送方式','acknowledge':'已核对企微'}.get(action,action))
            if flow['phase']=='sending':
                self.running.add(flow['id'])
                threading.Thread(target=self.execute,args=(sid,dict(flow)),daemon=True,name='wecom-dialogue-send').start()
            return answer

    def execute(self,sid,flow):
        try:
            result=self.console.adapter_call('send_dispatch',dict(dispatch_id=flow['dispatch_id']))
            self.complete(sid,flow,result.get('data') or dict(state='NOT_SENT',note=result.get('message','发送请求未被接受')))
        except Exception:
            self.complete(sid,flow,dict(state='UNKNOWN',note='未取得确定的发送回执。请到企微核对；本次不会自动重发。'))
        finally:self.running.discard(flow['id'])

    def complete(self,sid,flow,receipt):
        with self.guard:
            current=self.get(sid)
            if not current or current['id']!=flow['id']:return
            state=receipt.get('state','UNKNOWN')
            if state=='SERVER_RECORDED':message='原生发送完成，已核对企微服务器消息回执。'
            elif state=='VISIBLE_OUTGOING':message='企微已显示新增的本人消息气泡。'
            elif state=='NOT_SENT':message=receipt.get('note') or '未点击发送，请核对企微草稿。'
            else:message=receipt.get('note') or '发送结果未知，请核对企微，不要重发。'
            current.update(phase='finished' if state in ('SERVER_RECORDED','VISIBLE_OUTGOING','NOT_SENT') else 'unknown',message=message,receipt=receipt)
            current['revision']+=1;self.save(sid,current)

    def poll(self,sid):
        flow=self.get(sid)
        if flow and flow['phase']=='sending':
            try:
                result=self.console.adapter_call('get_operation_by_key',dict(idempotency_key='dispatch-'+flow['dispatch_id']))
                receipt=result.get('data') or {}
                if receipt.get('state') in ('SERVER_RECORDED','VISIBLE_OUTGOING','NOT_SENT','UNKNOWN'):self.complete(sid,flow,receipt);flow=self.get(sid)
                elif flow['id'] not in self.running and receipt.get('state')!='SENDING':
                    self.complete(sid,flow,dict(state='UNKNOWN',note='服务曾中断，未取得确定回执。请核对企微；不会自动重发。'));flow=self.get(sid)
                elif receipt.get('stage'):flow['message']=receipt['stage']+'…'
            except ValueError:flow['message']='正在等待适配器回执；请勿重复发送。'
        return dict(flow=flow)
