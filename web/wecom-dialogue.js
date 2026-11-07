'use strict';
function dialogueActionState(action){
  const labels={cancel:'正在取消…',choose:'正在确认接收人…',confirm:'正在提交发送…',edit:'正在准备修改…',backend:'正在更新发送方式…',retry:'正在重新准备…',acknowledge:'正在记录核对结果…',message:'正在处理…'};
  const message=labels[action]||'正在处理…';
  document.querySelectorAll('[data-send-active]').forEach(answer=>{answer.querySelectorAll('button,select').forEach(n=>n.disabled=true);const status=el('p',message,'dialogue-pending pending-status');status.setAttribute('role','status');status.setAttribute('aria-busy','true');answer.append(status);});
  if(action!=='message')$('job-status').textContent=message;
}
function sendDialogueActive(){return ['choose','compose','confirm','sending','unknown'].includes(state.sendFlow?.phase);}
function sendDialogueInput(){return sendDialogueActive()||/^(?:(?:请|麻烦)(?:你)?\s*)?(?:(?:帮我|我想要|我想|我要|我需要)\s*)?(?:给|对|向).{1,100}?(?:发送|发)/s.test($('question').value.trim());}
async function submitSendDialogue(action='message',extra={},useIntent=false){
  if(state.busy)return;
  const body={action,text:action==='message'?$('question').value.trim():'',...extra};
  if(useIntent){body.use_model=true;body.model_id=$('model-choice').value||undefined;$('job-status').textContent='AI 正在理解你的意图…';}
  if(state.sid)body.session_id=state.sid;
  if(state.sendFlow)body.revision=state.sendFlow.revision;
  state.busy=true;controls();renderSessions();dialogueActionState(action);
  try{
    const data=await consoleApi('send-dialogue',body);
    if(!data.handled){state.intentResult=data.intent||null;return false;}
    state.sid=data.session_id;state.sendFlow=data.flow;remember();$('question').value='';
    if(data.sessions){state.sessions=data.sessions;renderSessions();}else await loadSessions();await loadSession(state.sid,true,data.session);
    if(data.flow?.phase==='sending')pollSendDialogue(state.sid);
    return true;
  }finally{document.querySelectorAll('[data-send-active] button,[data-send-active] select').forEach(n=>n.disabled=false);document.querySelectorAll('.dialogue-pending').forEach(n=>n.remove());state.busy=state.sendFlow?.phase==='sending';if(!state.busy)$('job-status').textContent='';renderSessions();controls();}
}
async function pollSendDialogue(sid){
  try{
    const data=await consoleApi('send-dialogue/'+encodeURIComponent(sid));
    if(state.sid!==sid)return;
    state.sendFlow=data.flow;
    if(data.flow?.phase==='sending'){
      $('job-status').textContent=data.flow.message;
      state.busy=true;controls();renderSessions();setTimeout(()=>pollSendDialogue(sid),1500);
    }else{
      state.busy=false;$('job-status').textContent='';await loadSession(sid);controls();
    }
  }catch(e){state.busy=false;$('job-status').textContent='未取得发送状态，请刷新后核对回执；不要重复发送。';controls();}
}
function renderSendFlow(answer,flow){
  const active=state.sendFlow?.id===flow.id&&state.sendFlow?.revision===flow.revision;
  answer.append(el('p',flow.message));
  if(flow.intent)answer.append(el('small',`AI 理解：发送消息给 ${flow.query} · ${flow.intent.model}`));
  if(flow.recipient)answer.append(el('strong',`${flow.recipient.kind==='group'?'群聊':'联系人'}：${flow.recipient.name}`));
  if(flow.text&&flow.recipient)answer.append(el('pre',flow.text,'send-preview-text'));
  if(flow.receipt?.operation_id)answer.append(el('small','操作编号：'+flow.receipt.operation_id));
  if(!active)return;answer.setAttribute('data-send-active','true');
  const act=(action,extra)=>safe(()=>submitSendDialogue(action,extra));
  if(flow.phase==='choose'){
    flow.candidates.forEach((candidate,index)=>{
      const card=el('div',undefined,'send-candidate');
      card.append(button(`${index+1}. 是这位：${candidate.name}（${candidate.kind==='group'?'群聊':'联系人'}）`,act('choose',{recipient_id:candidate.id})));
      card.append(el('small',`账号标识 ${candidate.native_id} · 出现在：${candidate.evidence.join('、')}`));
      answer.append(card);
    });
  }
  if(flow.phase==='confirm'){
    const label=el('label','发送方式 ');const mode=el('select');mode.setAttribute('aria-label','本次发送方式');
    for(const [value,text] of [['native','原生调用（无需切换窗口）'],['background_window','后台窗口（请先打开目标会话）'],['desktop_ocr','前台桌面（可搜索联系人，将切换到企微）']]){const option=el('option',text);option.value=value;mode.append(option);}
    mode.value=flow.backend;
    mode.onchange=()=>act('backend',{backend:mode.value})();label.append(mode);const advanced=el('details',undefined,'send-method');advanced.append(el('summary',flow.backend==='native'?'原生调用 · 无需切换窗口':'窗口发送 · 查看或修改方式'),label);answer.append(advanced);if(flow.backend==='native')answer.append(el('p','适用于企微 5.0.11.6029 / 5.0.12.6018 / 5.0.13.6005 x86 的已有单聊；版本不匹配会停止并提示。','small'));
    answer.append(button('确认发送',act('confirm'),'primary'),button('修改正文',act('edit')));
  }
  if(['choose','compose','confirm'].includes(flow.phase))answer.append(button('取消发送',act('cancel')));
  if(flow.phase==='finished'&&flow.receipt?.state==='NOT_SENT'){
    answer.append(el('p','本次未执行发送。可重新核对正文，再用前台模式搜索联系人并发送。','small'));
    answer.append(button('改用前台，重新确认',act('retry')));
  }
  if(flow.phase==='unknown'){
    answer.append(el('p','结果未知的消息不会自动重试，请到企业微信核对。','small'));
    answer.append(button('已核对企微，开始下一条',act('acknowledge')));
  }
}
