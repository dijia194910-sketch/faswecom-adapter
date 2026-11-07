'use strict';
const $=id=>document.getElementById(id);
const state={archived:false,sendFlow:null,sid:null,job:null,busy:false,status:null,conversations:[],sessions:[],scope:new Set(),reply:null,recordOffset:0,preview:null,sending:false};
let toastTimer,recordRequest=0,modelSaving=false,modelEditorDirty=false,modelDialogEpoch=0,statusRequest=null,catalogRevision=0;
function el(tag,text,className){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(className)n.className=className;return n;}
function button(text,fn,cls='quiet'){const n=el('button',text,cls);n.type='button';n.onclick=fn;return n;}
function toast(message){$('toast').textContent=message;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,6500);}
async function api(path,body,method){const opts={cache:'no-store'};if(body!==undefined){opts.method=method||'POST';opts.headers={'Content-Type':'application/json','X-ChatWeave-UI':'1'};opts.body=JSON.stringify(body);}const r=await fetch(path,opts);let data;try{data=await r.json();}catch{throw Error('服务没有返回可用结果，请刷新状态。');}if(!r.ok)throw Error(data.detail||data.message||'操作失败');return data;}
const consoleApi=(path,body,method)=>api('/api/wecom/console/'+path,body,method);
function safe(fn){return async(...args)=>{try{await fn(...args);}catch(e){toast(e.message);}};}
function remember(){try{sessionStorage.setItem('wecom-console.sid',state.sid||'');}catch{}}
function controls(){const dialogue=sendDialogueInput();const aiIntent=$('intent-model').checked&&!sendDialogueActive()&&!state.reply;const configured=state.status?.model.has_api_key&&state.status?.agent_available;const filled=!!$('question').value.trim();$('ask-model').disabled=state.busy||!filled||(aiIntent?!state.status?.model.has_api_key:(!dialogue&&(!configured||!$('use-model').checked)));$('local-search').disabled=state.busy||!filled||!!state.reply;$('stop').hidden=!state.job;$('ask-model').hidden=state.busy;$('new-chat').disabled=state.busy;$('use-model').disabled=state.busy;$('intent-model').disabled=state.busy;$('model-choice').disabled=state.busy;$('model-note').textContent=!state.status?.agent_available?'Agent 依赖尚未安装；本地检索仍可使用。':!state.status?.model.has_api_key?'尚未配置模型。可以使用本地检索，或打开模型设置。':$('use-model').checked?'点击开始分析才会请求模型。生成回复草稿不会自动发送。':'模型调用已关闭。可以先使用本地检索。';$('ask-model').textContent=aiIntent?'理解并继续 ↑':dialogue?'继续发送对话 ↑':state.reply?'生成回复草稿 ↑':'开始分析 ↑';if(aiIntent)$('model-note').textContent='先用 AI 识别意图；发送需要核对接收人和正文。查询记录是否用模型分析由下方授权控制。';else if(dialogue)$('model-note').textContent='联系人选择和发送确认在本机完成。';$('reply-context').hidden=!state.reply;if(state.archived&&state.sid){$('ask-model').disabled=true;$('local-search').disabled=true;$('model-note').textContent='此对话已归档，请在左侧恢复后继续。';}}
async function refreshStatus(){const revision=catalogRevision;if(!statusRequest)statusRequest=consoleApi('status').finally(()=>{statusRequest=null;});const fresh=await statusRequest;state.status=revision===catalogRevision||!state.status?fresh:{...fresh,model_catalog:state.status.model_catalog,model:state.status.model};const m=state.status.model;$('model-state').textContent=m.has_api_key?m.model+' · 已配置':'模型未配置 · 本地检索可用';if(!$('model-dialog').open){$('api-base').value=m.api_base||'https://api.deepseek.com';$('model-name').value=m.model||'deepseek-flash';$('key-state').textContent=m.has_api_key?'已保存密钥；不会回传或显示。':'尚未保存密钥。';}const old=$('profile').value;$('profile').replaceChildren(...state.status.profiles.profiles.map(p=>{const o=el('option',p.label);o.value=p.id;return o;}));$('profile').value=old||'deep';renderModelChoices();renderTargets();controls();}
function renderModelChoices(){
  const catalog=state.status.model_catalog||{models:[],active:null};
  const choices=catalog.models.map(m=>{const o=el('option',m.name+(m.has_api_key?'':' · 待补密钥'));o.value=m.id;return o;});
  if(!choices.length){const o=el('option',state.status.model.model||'请添加模型');o.value='';choices.push(o);}
  $('model-choice').replaceChildren(...choices);$('model-choice').value=catalog.active||'';
  const editor=$('model-edit').value;const fresh=el('option','＋ 新模型');fresh.value='__new__';
  $('model-edit').replaceChildren(...catalog.models.map(m=>{const o=el('option',m.name);o.value=m.id;return o;}),fresh);
  $('model-edit').value=editor==='__new__'||catalog.models.some(m=>m.id===editor)?editor:(catalog.active||'__new__');if(!$('model-dialog').open)editModel();
}
function editModel(){
  const m=state.status.model_catalog?.models.find(m=>m.id===$('model-edit').value);
  $('model-delete').hidden=!m;$('api-base').readOnly=false;$('model-name').readOnly=false;
  $('model-label').value=m?.name||'';$('model-provider').value=m?.provider||'openai-compatible';
  $('api-base').value=m?.api_base||'';$('model-name').value=m?.model||'';$('api-key').value='';
  $('key-state').textContent=m?.has_api_key?'密钥已加密保存，留空保留。修改 API 地址或接口类型后须重新填写密钥。':'可先保存模型信息，调用前再补充 API Key。';
  if(m&&!m.credential_id)$('key-state').textContent+=' 此导入模型与同接口组共用密钥。';
  $('model-parameters').textContent=m?`输出上限 ${m.max_tokens?.toLocaleString()||'默认'} tokens；本轮实际预算由分析强度控制。`:'新模型的密钥独立保存，不会复用其他接口的密钥。';
}
$('model-edit').onchange=()=>{modelEditorDirty=true;editModel();};
$('model-form').addEventListener('input',()=>{modelEditorDirty=true;});
$('model-add').onclick=()=>{if(modelSaving)return;modelEditorDirty=true;$('model-edit').value='__new__';editModel();$('model-save-state').textContent='填写新模型信息后，点击保存设置。';$('model-label').focus();};
$('model-delete').onclick=()=>{const m=state.status.model_catalog?.models.find(m=>m.id===$('model-edit').value);if(!m)return;state.deleteModelId=m.id;$('model-delete-title').textContent=m.name;$('model-delete-dialog').showModal();};
$('model-delete-confirm').onclick=safe(async()=>{const id=state.deleteModelId;if(!id)return;$('model-delete-confirm').disabled=true;try{const catalog=await consoleApi('models/'+encodeURIComponent(id)+'/delete',{});state.deleteModelId=null;$('model-delete-dialog').close();applyModelCatalog(catalog);editModel();toast('模型配置已删除，历史对话保留。');}finally{$('model-delete-confirm').disabled=false;}});
$('model-choice').onchange=safe(async()=>{applyModelCatalog(await consoleApi('model-selection',{model_id:$('model-choice').value},'PUT'));toast('已切换模型；未调用模型。');});
function renderTargets(){$('send-status').textContent=`发送${state.status.send_enabled?'已启用':'已关闭'} · ${state.status.targets.filter(t=>t.enabled).length} 个手工授权目标`;}

async function loadSessions(){state.sessions=(await consoleApi('sessions?archived='+state.archived)).sessions;renderSessions();}
function renderSessions(){
  const q=$('history-search').value.toLowerCase();$('history').replaceChildren();
  $('history-label').textContent=state.archived?'已归档对话':'最近对话';
  $('history-archive').textContent=state.archived?'返回最近':'已归档';
  for(const [label,id] of state.sessions){
    if(!label.toLowerCase().includes(q))continue;
    const title=label.replace(/^\d{2}-\d{2}[T ]\d{2}:\d{2} · /,'');
    const row=el('div',undefined,'history-row');
    const b=button(title,safe(()=>loadSession(id)),'history-item'+(id===state.sid?' active':''));b.title=label;b.disabled=state.busy;
    const actions=el('div',undefined,'history-actions');
    for(const [action,text] of [[state.archived?'restore':'archive',state.archived?'恢复':'归档'],['delete','删除']]){
      const control=button(text,safe(()=>manageSession(id,action,title)),'link'+(action==='delete'?' danger':''));
      control.setAttribute('aria-label',text+'对话 '+title);control.disabled=state.busy;actions.append(control);
    }
    row.append(b,actions);$('history').append(row);
  }
  if(!$('history').childElementCount)$('history').append(el('p',q?'没有匹配的对话':state.archived?'没有已归档对话':'还没有对话','small'));
}
async function manageSession(id,action,title){
  if(state.busy)return;
  if(action==='delete'){state.deleteId=id;$('delete-title').textContent=title;$('delete-dialog').showModal();return;}
  await consoleApi('sessions/'+encodeURIComponent(id)+'/manage',{action});
  if(state.sid===id)resetConversation();
  await loadSessions();toast(action==='delete'?'对话已删除':action==='archive'?'对话已归档，可在已归档列表恢复':'对话已恢复到最近对话');
}
$('delete-confirm').onclick=safe(async()=>{
  const id=state.deleteId;if(!id||state.busy)return;
  $('delete-confirm').disabled=true;
  try{await consoleApi('sessions/'+encodeURIComponent(id)+'/manage',{action:'delete'});state.deleteId=null;$('delete-dialog').close();if(state.sid===id)resetConversation();await loadSessions();toast('对话已删除');}
  finally{$('delete-confirm').disabled=false;}
});
$('history-archive').onclick=safe(async()=>{if(state.busy)return;state.archived=!state.archived;resetConversation();await loadSessions();});

async function loadSession(id,force=false,data=null){if(state.busy&&!force)return;state.sid=id;remember();data=data||await consoleApi('sessions/'+encodeURIComponent(id));state.sendFlow=data.send_flow||null;const options=data.turns.at(-1)?.options;if(options&&Array.isArray(options.conversations)){state.scope=new Set(options.conversations);$('scope-start').value=options.start||'';$('scope-end').value=options.end||'';$('keywords').value=options.keywords||'';if(options.profile)$('profile').value=options.profile;renderScope();}state.reply=null;renderTurns(data.turns);const title=state.sessions.find(s=>s[1]===id)?.[0].replace(/^\d{2}-\d{2}[T ]\d{2}:\d{2} · /,'');$('thread-title').textContent=title||'对话';renderSessions();controls();}
function lazySources(details,messages){
  let shown=0;
  const more=button('加载更多原文',()=>append(),'quiet');
  function append(){
    more.remove();const fragment=document.createDocumentFragment();
    for(const m of messages.slice(shown,shown+50)){
      const source=el('div',undefined,'source');
      source.append(el('small',`${m.conversation} · ${m.sender} · ${m.time}`),el('pre',m.content));
      source.append(button('查看上下文',safe(()=>openRecords(m.id))));fragment.append(source);
    }
    shown=Math.min(shown+50,messages.length);details.append(fragment);
    if(shown<messages.length){more.textContent=`加载更多原文（已显示 ${shown}/${messages.length}）`;details.append(more);}
  }
  details.addEventListener('toggle',()=>{if(details.open&&!shown)append();});
  if(details.open)append();
}
function renderTurns(turns){$('welcome').hidden=turns.length>0;$('turns').replaceChildren();for(const turn of turns){const item=el('article',undefined,'turn');item.append(el('div',turn.question,'user-message'));const answer=el('div',undefined,'assistant-message');const result=turn.result||{};const meta=result.send_dialogue?(result.send_dialogue.intent?'发送对话 · 已使用 AI 识别意图':'发送对话 · 未调用模型'):result.local_only?`本地检索 · ${turn.messages.length} 条相关原文 · 未调用模型`:`${turn.status==='running'?'正在处理…':turn.status==='interrupted'?'上次运行已中断':turn.status==='cancelled'?'已停止':turn.status==='error'?'分析失败':'本轮结果'} · ${turn.requests||0} 次模型请求`;answer.append(el('div',turn.options?.intent_model?'AI 已识别为查询 · '+meta.replace('未调用模型','记录未传给模型'):meta,'answer-meta'));if(result.send_dialogue)renderSendFlow(answer,result.send_dialogue);if(result.intent_message)answer.append(el('p',result.intent_message));for(const c of result.contacts||[])answer.append(el('p',`${c.name}（${c.kind==='group'?'群聊':'联系人'}）· ${c.evidence.join('、')}`));if(result.error)answer.append(el('p',result.error));if(result.local_only)answer.append(el('p','以下是按所选范围检索或抽样得到的原始记录，没有生成总结。可结合上下文阅读；启用模型后可分析这些资料。'));if(result.reply_text){answer.append(el('p',result.reply_text));answer.append(button('在对话中准备发送',()=>{const source=turn.messages.find(m=>m.id===turn.options?.reply_message_id)||turn.messages[0];const peer=source&&!source.is_self?source:turn.messages.find(m=>m.conversation_id===source?.conversation_id&&!m.is_self);const name=source?.conversation_type==='group'?source.conversation:(peer?.sender||'接收人');$('question').value='给'+name+'发消息：'+result.reply_text;state.reply=null;controls();$('question').focus();toast('请核对输入框中的接收人和正文，再继续发送对话。');}));}for(const claim of result.claims||[]){answer.append(el('p',claim.text));const allowed=new Set(turn.messages.map(m=>m.id));for(const mid of claim.evidence_ids||[]){if(allowed.has(mid))answer.append(button('原文 M'+mid,safe(()=>openRecords(mid)),'evidence-link'));}}if(!result.intent_message&&!result.send_dialogue&&!result.local_only&&!result.reply_text&&!result.error&&!(result.claims||[]).length&&turn.status==='completed')answer.append(el('p','当前资料不足以生成有依据的回答。可以调整范围后再查。'));if(turn.messages.length){const details=el('details',undefined,'sources');details.open=!!result.local_only;details.append(el('summary',`本轮读取的原文 · ${turn.messages.length} 条`));lazySources(details,turn.messages);answer.append(details);}item.append(answer);$('turns').append(item);}$('thread').scrollTop=$('thread').scrollHeight;}
function renderScope(){const q=$('scope-query').value.toLowerCase();$('scope-chats').replaceChildren();for(const c of state.conversations){if(!c.name.toLowerCase().includes(q))continue;const l=el('label',undefined,'chat-choice');const input=el('input');input.type='checkbox';input.checked=state.scope.has(c.conversation_id);input.onchange=()=>{input.checked?state.scope.add(c.conversation_id):state.scope.delete(c.conversation_id);scopeLabel();};l.append(input,el('span',c.name));l.title=c.name;$('scope-chats').append(l);}scopeLabel();}
function scopeLabel(){$('range-label').textContent=state.scope.size?`已选择 ${state.scope.size} 个会话`:'全部已导入企微会话';}
async function loadConversations(){const rows=(await api('/api/wecom/conversations')).conversations;const changed=JSON.stringify(rows.map(c=>[c.conversation_id,c.name]))!==JSON.stringify(state.conversations.map(c=>[c.conversation_id,c.name]));state.conversations=rows;if(changed){renderScope();const old=$('record-conversation').value;const all=el('option','全部已导入会话');all.value='';$('record-conversation').replaceChildren(all,...state.conversations.map(c=>{const o=el('option',c.name);o.value=c.conversation_id;return o;}));$('record-conversation').value=old;}const count=state.conversations.reduce((n,c)=>n+c.count,0);$('data-count').textContent=`${state.conversations.length} 个会话 · ${count.toLocaleString()} 条消息`;$('browse').title=$('data-count').textContent;}
function beginPending(question,message){
  document.querySelector('.pending-turn')?.remove();
  const item=el('article',undefined,'turn pending-turn');item.append(el('div',question,'user-message'));
  const answer=el('div',undefined,'assistant-message');const status=el('div',message,'answer-meta pending-status');
  status.setAttribute('role','status');status.setAttribute('aria-busy','true');answer.append(status);item.append(answer);
  $('welcome').hidden=true;$('turns').append(item);$('thread').scrollTop=$('thread').scrollHeight;
}
function pendingStatus(message,failed=false){
  const node=document.querySelector('.pending-status');if(!node)return;
  node.textContent=message;node.classList.toggle('failed',failed);node.setAttribute('aria-busy',String(!failed));
}
async function run(useModel){
  if(state.busy)return;if(state.archived&&state.sid)throw Error('请先恢复已归档对话');
  const question=$('question').value.trim();if(!question)return;
  state.archived=false;state.intentResult=null;
  const useIntent=useModel&&$('intent-model').checked&&!state.reply&&!sendDialogueActive();
  const dialogue=useIntent||sendDialogueInput();
  beginPending(question,useIntent?'正在理解你的意图…':useModel?'正在准备分析…':'正在本地检索…');$('question').value='';
  try{
    if(dialogue&&await submitSendDialogue('message',{text:question},useIntent))return;
    useModel=useModel&&$('use-model').checked;
    const body={question,conversations:[...state.scope],start:$('scope-start').value,end:$('scope-end').value,keywords:$('keywords').value,profile:$('profile').value,use_model:useModel};
    if(state.intentResult)body.intent_model=state.intentResult.model;
    if($('model-choice').value)body.model_id=$('model-choice').value;if(state.sid)body.session_id=state.sid;
    if(state.reply){body.reply_message_id=state.reply.id;body.conversations=[state.reply.conversation_id];}
    state.busy=true;controls();renderSessions();pendingStatus(useModel?'正在启动分析 Agent…':'正在本地检索…');
    const data=await consoleApi('jobs',body);state.sid=data.session_id;state.job=data.job_id;remember();controls();
    await loadSessions();await pollJob();
  }catch(e){
    state.busy=!!state.job;pendingStatus(state.job?'连接中断，任务状态待核对。请刷新页面恢复进度。':'提交失败：'+e.message,true);
    if(!state.job&&!$('question').value)$('question').value=question;controls();renderSessions();throw e;
  }
}
async function pollJob(){
  let failures=0;
  while(state.job){
    let job;try{job=await consoleApi('jobs/'+state.job);failures=0;}catch(e){
      if(++failures>=3)throw e;pendingStatus('连接暂时中断，正在恢复进度…');await new Promise(r=>setTimeout(r,1000));continue;
    }
    const message=job.steps?.at(-1)||'正在检索所选范围…';$('job-status').textContent=message;pendingStatus(message);
    if(job.state!=='running'){
      state.job=null;state.busy=false;$('job-status').textContent=job.error||(job.state==='completed'?'本轮已完成':job.state==='cancelled'?'已停止':'本轮未完成');
      state.reply=null;controls();await loadSessions();await loadSession(state.sid);return;
    }
    await new Promise(r=>setTimeout(r,750));
  }
}
async function openRecords(anchor){if(anchor){const data=await api('/api/wecom/messages?anchor='+encodeURIComponent(anchor));$('record-conversation').value=data.conversation_id;$('record-query').value='';state.recordOffset=data.next_offset;renderRecords(data.messages,false);$('record-more').hidden=!data.has_more;}else await loadRecords(false);$('records-dialog').showModal();}
function renderRecords(messages,append){if(!append)$('record-list').replaceChildren();for(const m of messages){const item=el('article',undefined,'record'+(m.is_self?' self':''));const head=el('div',undefined,'record-head');head.append(el('span',`${m.sender} · ${m.conversation}`),el('span',m.time));item.append(head,el('p',m.content));item.append(button('帮我拟回复',()=>{state.reply=m;state.scope=new Set([m.conversation_id]);renderScope();$('reply-label').textContent=`回复 ${m.sender} · ${m.conversation}`;$('question').value='帮我拟一条回复，参考当前会话中本人的表达习惯。';$('records-dialog').close();controls();$('question').focus();if(!state.status.model.has_api_key)toast('已选中回复上下文；生成草稿前需要配置模型。');}));$('record-list').append(item);}if(!$('record-list').childElementCount)$('record-list').append(el('p','当前范围暂无记录。请先刷新导入。','small'));}
async function loadRecords(append){const sequence=++recordRequest;const params=new URLSearchParams({conversation_id:$('record-conversation').value,query:$('record-query').value,offset:append?state.recordOffset:0});const data=await api('/api/wecom/messages?'+params);if(sequence!==recordRequest)return;state.recordOffset=data.next_offset;renderRecords(data.messages,append);if(!append)$('record-refresh').hidden=true;$('record-more').hidden=!data.has_more;}
function resetConversation(){state.sid=null;state.sendFlow=null;state.reply=null;remember();$('turns').replaceChildren();$('welcome').hidden=false;$('thread').scrollTop=0;$('thread-title').textContent='新对话';$('job-status').textContent='';$('question').value='';renderSessions();controls();}
$('new-chat').onclick=safe(async()=>{resetConversation();state.archived=false;await loadSessions();});
$('question').oninput=controls;$('use-model').onchange=controls;$('history-search').oninput=renderSessions;let scopeFilterTimer;$('scope-query').oninput=()=>{clearTimeout(scopeFilterTimer);scopeFilterTimer=setTimeout(renderScope,150);};$('clear-scope').onclick=()=>{state.scope.clear();renderScope();};$('toggle-scope').onclick=()=>document.body.classList.toggle('scope-hidden');
document.querySelectorAll('[data-prompt]').forEach(b=>b.onclick=()=>{$('question').value=b.dataset.prompt;controls();$('question').focus();});
$('ask-form').onsubmit=safe(async e=>{e.preventDefault();await run(true);});$('local-search').onclick=safe(()=>run(false));$('stop').onclick=safe(async()=>{if(state.job){await consoleApi('jobs/'+state.job+'/stop',{});$('job-status').textContent='正在停止…';}});$('clear-reply').onclick=()=>{state.reply=null;controls();};
function applyModelCatalog(catalog){
  catalogRevision++;if(!state.status)return;
  state.status.model_catalog=catalog;
  const active=catalog.models.find(m=>m.id===catalog.active);
  state.status.model=active||{api_base:'',model:'',has_api_key:false,configured:false};
  $('model-state').textContent=active?.has_api_key?active.model+' · 已配置':'模型未配置 · 本地检索可用';
  renderModelChoices();controls();
}
function modelBusy(value){
  $('model-form').setAttribute('aria-busy',String(value));
  $('model-form').querySelectorAll('input,select,button:not([data-close])').forEach(n=>{n.disabled=value;});
  $('model-save').textContent=value?'正在保存…':'保存设置';
}
document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>b.closest('dialog').close());
$('open-model').onclick=async()=>{
  const epoch=++modelDialogEpoch;
  if(!$('model-dialog').open)$('model-dialog').showModal();
  if(modelSaving){modelBusy(true);$('model-save-state').textContent='正在保存模型配置，请稍候…';return;}
  modelEditorDirty=false;modelBusy(!state.status);
  $('model-save-state').textContent=state.status?'':'正在加载模型配置…';
  if(state.status)editModel();
  try{
    await refreshStatus();
    if(epoch===modelDialogEpoch&&!modelEditorDirty&&!modelSaving){editModel();$('model-save-state').textContent='';}
  }catch(e){$('model-save-state').textContent='状态刷新失败：'+e.message;}
  finally{if(!modelSaving)modelBusy(false);}
};
$('model-form').onsubmit=async e=>{
  e.preventDefault();if(modelSaving)return;
  const id=$('model-edit').value;
  const body={name:$('model-label').value,provider:$('model-provider').value,api_base:$('api-base').value,model:$('model-name').value,api_key:$('api-key').value};
  if(id!=='__new__')body.id=id;
  const previousIds=new Set((state.status?.model_catalog?.models||[]).map(m=>m.id));
  modelSaving=true;modelBusy(true);$('model-save-state').textContent='正在保存模型配置，请稍候…';
  try{
    const catalog=await consoleApi('models',body);
    $('api-key').value='';applyModelCatalog(catalog);
    $('model-edit').value=id!=='__new__'?id:(catalog.models.find(m=>!previousIds.has(m.id))?.id||catalog.active||'__new__');
    $('model-save-state').textContent='已保存';$('model-dialog').close();
    toast('模型设置已保存；可在右侧切换使用。');
  }catch(e){$('model-save-state').textContent='保存失败：'+e.message;}
  finally{modelSaving=false;modelBusy(false);}
};
$('browse').onclick=safe(()=>openRecords());$('record-search').onclick=safe(()=>loadRecords(false));$('record-conversation').onchange=safe(()=>loadRecords(false));$('record-more').onclick=safe(()=>loadRecords(true));
$('open-send').onclick=safe(async()=>{await refreshStatus();$('send-dialog').showModal();});
$('target-form').onsubmit=safe(async e=>{e.preventDefault();await consoleApi('targets',{name:$('target-name').value,kind:$('target-kind').value,backend:$('target-backend').value,approved:$('target-approved').checked});await refreshStatus();$('target-approved').checked=false;toast('已保存目标授权。');});$('disable-sending').onclick=safe(async()=>{await consoleApi('disable-sending',{});await refreshStatus();toast('已有发送权限已撤销。');});
safe(async()=>{await Promise.all([refreshStatus(),loadSessions(),loadConversations()]);try{state.sid=sessionStorage.getItem('wecom-console.sid')||null;}catch{}if(state.status.active_job){state.job=state.status.active_job;const job=await consoleApi('jobs/'+state.job);state.sid=job.session_id;state.busy=true;await loadSession(state.sid,true);const running=document.querySelector('#turns .turn:last-child .answer-meta');if(running){running.classList.add('pending-status');running.setAttribute('aria-busy','true');}controls();await pollJob();}else if(state.sid&&state.sessions.some(s=>s[1]===state.sid)){await loadSession(state.sid);if(state.sendFlow?.phase==='sending')pollSendDialogue(state.sid);}controls();})();

let liveRecordsBusy=false;
document.addEventListener('wecom-data-changed',safe(async()=>{
  if(liveRecordsBusy)return;
  liveRecordsBusy=true;
  try{await loadConversations();if($('records-dialog').open){
    if(state.recordOffset<=50&&$('records-dialog').scrollTop<80)await loadRecords(false);
    else $('record-refresh').hidden=false;
  }}finally{liveRecordsBusy=false;}
}));
$('record-refresh').onclick=safe(()=>loadRecords(false));

$('intent-model').onchange=controls;
