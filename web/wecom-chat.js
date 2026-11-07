'use strict';
(()=>{
const $=id=>document.getElementById(id),storage='wecom-chat:'+location.pathname.replace(/\/chat$/,'');
const state={chats:[],cid:'',name:'',kind:'',messages:[],media:{},offset:0,cursor:null,listOffset:0,listCursor:null,listEpoch:0,messageEpoch:0,target:null,file:null,busy:false,uploading:false,query:'',drafts:{},outbox:[],auto:null,autoBusy:false,suggestBusy:false,suggestion:null};
try{const saved=JSON.parse(sessionStorage.getItem(storage)||'{}');state.outbox=Array.isArray(saved.outbox)?saved.outbox.slice(-50):[];state.drafts=saved.drafts||{};state.cid=saved.cid||'';}catch{}
const el=(tag,text,cls)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node;};
const chatCache=new Map();let reads=new AbortController(),autoPolling=false,listReads;
let renderedContext='';
const cursorParams=(cursor,offset)=>cursor?{before_timestamp:cursor.timestamp,before_id:cursor.id}:{offset};
function unseen(existing,incoming,key){const seen=new Set(existing.map(row=>row[key]));return incoming.filter(row=>{if(seen.has(row[key]))return false;seen.add(row[key]);return true;});}
const mediaKind=message=>({14:'图片',101:'图片',15:'文件',102:'文件',16:'语音',103:'视频'}[message.content?.match(/^\[企业微信非文本消息，类型 (\d+)；/)?.[1]]);
const localURL=url=>{const base=location.pathname.match(/^\/w\/(legacy|[a-f0-9]{64})(?=\/)/)?.[0]||'';return base+url;};
function bubble(message){
 const box=el('div',undefined,'bubble'),label=mediaKind(message);if(!label){box.textContent=message.content;return box;}
 const item=state.media[message.id];box.classList.add('attachment');
 if(!item){box.append(el('strong','['+label+']'),el('small','正在读取本机附件…'));return box;}
 if(item.status!=='available'){box.append(el('strong',item.name||'['+label+']'),el('small',item.reason||'本机没有可用附件'));return box;}
 if(item.kind==='image'){
  const image=el('img');image.src=localURL(item.url);image.alt=item.name||'聊天图片';image.loading='lazy';image.width=item.width;image.height=item.height;
  const button=el('button',undefined,'image-preview');button.type='button';button.setAttribute('aria-label','查看大图');button.append(image);
  button.onclick=()=>{const dialog=$('image-viewer');$('full-image').src=image.src;$('full-image').alt=image.alt;dialog.showModal();};
  image.onerror=()=>{button.replaceChildren(el('span','图片暂不可用，请刷新聊天'));};box.append(button);
 }else if(item.preview&&['video','voice'].includes(item.kind)){
  const player=el(item.kind==='video'?'video':'audio');player.controls=true;player.preload='none';player.src=localURL(item.url);box.append(player);
 }else box.append(el('strong',item.name||'['+label+']'));
 const link=el('a','保存'+label+' · '+(item.size<1024?item.size+' B':(item.size/1024).toFixed(0)+' KB'),'media-download');link.href=localURL(item.download_url);link.download=item.name;box.append(link);
 if(item.reason)box.append(el('small',item.reason));return box;
}
async function attachments(seq,cid){
 const ids=state.messages.filter(m=>mediaKind(m)&&(!state.media[m.id]||state.media[m.id].status!=='available'&&Date.now()-(state.media[m.id]._at||0)>15000)).map(m=>m.id);if(!ids.length)return;
 const updated=new Set();
 for(let offset=0;offset<ids.length;offset+=50){
  let data;try{data=(await api('chat/media',{ids:ids.slice(offset,offset+50)},{signal:reads.signal})).attachments;}
  catch(e){if(e.name==='AbortError')return;data=Object.fromEntries(ids.slice(offset,offset+50).map(id=>[id,{status:'unavailable',reason:e.message}]));}
  if(seq!==state.messageEpoch||cid!==state.cid)return;for(const [id,item] of Object.entries(data)){item._at=Date.now();updated.add(Number(id));}Object.assign(state.media,data);rememberChat();
 }
 const box=$('message-scroll'),bottom=box.scrollHeight-box.scrollTop-box.clientHeight<120,top=box.scrollTop;
 // Replace only media bubbles. Avatar nodes, help buttons and text stay put.
 for(const message of state.messages.filter(m=>updated.has(m.id))){const article=$('chat-messages').querySelector('[data-message-id="'+message.id+'"]');if(article)article.querySelector('.bubble')?.replaceWith(bubble(message));}
 if(bottom)box.scrollTop=box.scrollHeight;else box.scrollTop=top;
}
function save(){sessionStorage.setItem(storage,JSON.stringify({cid:state.cid,drafts:state.drafts,outbox:state.outbox.slice(-50)}));}
let toastTimer;function toast(text){$('chat-toast').textContent=text;$('chat-toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('chat-toast').hidden=true,7000);}
const guard=fn=>async event=>{event?.preventDefault();try{await fn(event);}catch(e){if(e.name!=='AbortError')toast(e.message);}};
async function api(path,body,options={}){const r=await fetch('/api/wecom/'+path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-ChatWeave-UI':'1'}:{},body:body?JSON.stringify(body):undefined,priority:'high',...options});const data=await r.json();if(!r.ok)throw Error(data.detail||'请求未完成');return data;}
function rememberChat(){if(!state.cid||state.query||state.offset>50||!state.messages.length)return;chatCache.delete(state.cid);chatCache.set(state.cid,{messages:state.messages.slice(),media:{...state.media},offset:state.offset,cursor:state.cursor,hasMore:!$('older-messages').hidden,until:Date.now()+30000});while(chatCache.size>12)chatCache.delete(chatCache.keys().next().value);}
function highlightChats(){for(const item of $('chat-list').querySelectorAll('.chat-item'))item.classList.toggle('active',item.dataset.cid===state.cid);}

function controls(){const locked=state.busy||state.uploading;const uncertain=state.outbox.some(o=>o.cid===state.cid&&!o.ack&&['UNKNOWN','SENDING','PENDING'].includes(o.state));$('chat-text').disabled=!state.target||locked;$('chat-text').readOnly=!!state.file;$('chat-text').placeholder=state.file?'已选择文件，按 Enter 发送…':'输入消息…';$('choose-file').disabled=!state.target||locked||!!$('chat-text').value.trim();$('chat-send').disabled=!state.target||locked||uncertain||!(state.file||$('chat-text').value.trim());$('chat-backend').disabled=locked||!!state.file;$('remove-file').disabled=locked;$('chat-send').textContent=state.busy?'发送中…':'发送';$('send-mode-note').textContent=state.file?'文件通过前台企微发送 · 会切换窗口':'文字默认通过原生接口发送';}
function avatarNode(cid,sender,label,group=false,mid=null){return window.WeComAvatars.node(cid,sender,label,group,mid);}
function autoControls(){if(!$('auto-reply'))return;const auto=state.auto;if(!state.autoBusy)$('auto-reply').checked=!!auto?.enabled;$('auto-reply').disabled=!state.target||!state.auto||state.autoBusy;$('auto-reply-status').textContent=auto?.error|| (auto?.enabled?'已开启 · 新消息直接发送，末尾标注（AI回复）'+(auto.backend==='desktop_ocr'?' · 会切换企微窗口':''):'仅回复开启后的新消息 · 自动发送并标注（AI回复）');}
async function autoStatus(){if(!$('auto-reply'))return;if(!state.cid||state.autoBusy||autoPolling)return;autoPolling=true;const cid=state.cid,controller=reads;try{const result=await api('chat/auto-reply?conversation_id='+encodeURIComponent(cid),null,{signal:controller.signal});if(state.cid===cid&&reads===controller){state.auto=result;autoControls();}}finally{autoPolling=false;}}

async function suggest(message,button){if(state.suggestBusy)throw Error('正在生成回复，请稍候');const cid=state.cid,before=$('chat-text').value;state.suggestBusy=true;button.disabled=true;button.textContent='正在生成…';try{const data=await api('chat/suggest',{conversation_id:cid,message_id:message.id});if(cid!==state.cid)return;if(!before&&!$('chat-text').value&&!state.file&&!state.busy){$('chat-text').value=data.message;state.drafts[cid]=data.message;save();controls();$('chat-text').focus();toast('回复草稿已填入，可以修改后发送');}else{state.suggestion={cid,message:data.message};$('reply-suggestion-text').textContent=data.message;$('reply-suggestion').hidden=false;}}finally{state.suggestBusy=false;button.disabled=false;button.textContent='帮我回复';}}
function list(){const box=$('chat-list'),previous=new Map([...box.querySelectorAll('.chat-item')].map(node=>[node.dataset.cid,node]));box.replaceChildren(...state.chats.map(row=>{const signature=JSON.stringify([row.name,row.kind,row.preview,row.is_self,row.latest]);let b=previous.get(row.conversation_id);if(b&&b.dataset.signature===signature){b.classList.toggle('active',row.conversation_id===state.cid);return b;}b=el('button',undefined,'chat-item'+(row.conversation_id===state.cid?' active':''));b.dataset.cid=row.conversation_id;b.dataset.signature=signature;const avatar=avatarNode(row.conversation_id,'',row.kind==='group'?'群':row.name.slice(0,1),row.kind==='group');const copy=el('div',undefined,'chat-copy');copy.append(el('strong',row.name),el('small',(row.is_self?'我：':'')+(mediaKind({content:row.preview})?'['+mediaKind({content:row.preview})+']':(row.preview||'暂无文本'))));const date=new Date(row.latest);b.append(avatar,copy,el('time',date.toLocaleDateString()===new Date().toLocaleDateString()?date.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}):date.toLocaleDateString([], {month:'2-digit',day:'2-digit'})));b.title=row.name;b.onclick=guard(()=>select(row));return b;}));$('chat-count').textContent=state.chats.length+(state.hasMore?'+':'');if(!state.chats.length)box.append(el('p','没有找到会话。可调整搜索，或先同步聊天记录。','empty'));}

async function conversations(more=false){
 listReads?.abort();listReads=new AbortController();const seq=++state.listEpoch;
 const params=new URLSearchParams({query:$('chat-query').value,...(more?cursorParams(state.listCursor,state.listOffset):{offset:0})});
 $('more-chats').disabled=true;
 try{
  const data=await api('chat/conversations?'+params,null,{signal:listReads.signal});if(seq!==state.listEpoch)return;
  state.chats=more?[...state.chats,...unseen(state.chats,data.conversations,'conversation_id')]:data.conversations;
  state.listOffset=state.chats.length;state.listCursor=data.next_cursor;state.hasMore=data.has_more;$('more-chats').hidden=!data.has_more;list();
 }finally{if(seq===state.listEpoch)$('more-chats').disabled=false;}
}
function row(message,outgoing=false){const r=el('article',undefined,'message'+(message.is_self?' self':''));const avatar=avatarNode(message.conversation_id||state.cid,message.sender_id,message.is_self?'我':(message.sender||'?').slice(0,1),outgoing,message.id),main=el('div',undefined,'message-main');if(message.id)r.dataset.messageId=message.id;main.append(el('div',(message.is_self?'我':message.sender)+' · '+(message.time||new Date(message.timestamp).toLocaleString()),'message-meta'),bubble(message));if(!message.is_self&&!outgoing){const actions=el('div',undefined,'message-actions'),button=el('button','帮我回复','help-reply');button.type='button';button.onclick=guard(()=>suggest(message,button));actions.append(button);main.append(actions);}if(outgoing){const status=el('div',message.note||message.state,'outcome'+(['UNKNOWN','NOT_SENT','PENDING'].includes(message.state)?' uncertain':''));if(['UNKNOWN','PENDING','NOT_SENT'].includes(message.state)&&!message.ack){const b=el('button','已在企微核对，继续聊天');b.type='button';b.onclick=()=>{message.ack=true;save();renderOutbox();controls();};status.append(el('br'),b);}main.append(status);}r.append(avatar,main);return r;}
function messageSignature(message){return JSON.stringify([message.id,message.content,message.sender,message.sender_id,message.is_self,message.time,message.timestamp,message.conversation_id]);}
function messageNode(message){const node=row(message);node.dataset.signature=messageSignature(message);return node;}
function renderMessages(scroll=false,added=null){
 const box=$('chat-messages'),context=JSON.stringify([state.cid,state.query]);
 if(added&&renderedContext===context&&box.querySelector('article')){
  const fragment=document.createDocumentFragment();for(const message of [...added].reverse())fragment.append(messageNode(message));box.prepend(fragment);
 }else{
  const previous=renderedContext===context?new Map([...box.querySelectorAll('article[data-message-id]')].map(node=>[node.dataset.messageId,node])):new Map();
  const desired=[...state.messages].reverse();
  for(let i=0;i<desired.length;i++){
   const message=desired[i],old=previous.get(String(message.id)),node=old?.dataset.signature===messageSignature(message)?old:messageNode(message);
   if(box.children[i]!==node)box.insertBefore(node,box.children[i]||null);
  }
  while(box.children.length>desired.length)box.lastElementChild.remove();
  if(!desired.length)box.append(el('p',state.query?'没有匹配的消息。':'当前聊天暂无已导入消息。','empty'));
 }
 renderedContext=context;renderOutbox();if(scroll)$('message-scroll').scrollTop=$('message-scroll').scrollHeight;
}
function renderOutbox(){$('outbox').replaceChildren(...state.outbox.filter(o=>o.cid===state.cid&&!state.query&&!(o.native_message_id&&state.messages.some(m=>m.source_id==='message_table:'+o.native_message_id))).map(o=>row(o,true)));}
async function messages(more=false,scroll=false){
 if(!state.cid)return;const cid=state.cid,seq=++state.messageEpoch;
 const oldHeight=$('message-scroll').scrollHeight,oldTop=$('message-scroll').scrollTop;
 const params=new URLSearchParams({conversation_id:cid,query:state.query,...(more?cursorParams(state.cursor,state.offset):{offset:0})});
 $('older-messages').disabled=true;
 try{
  const data=await api('messages?'+params,null,{signal:reads.signal});if(seq!==state.messageEpoch||cid!==state.cid)return;
  const added=more?unseen(state.messages,data.messages,'id'):data.messages;
  state.messages=more?[...state.messages,...added]:added;state.offset=state.messages.length;state.cursor=data.next_cursor;
  $('older-messages').hidden=!data.has_more;renderMessages(scroll,more?added:null);rememberChat();void attachments(seq,cid);
  if(more)$('message-scroll').scrollTop=oldTop+$('message-scroll').scrollHeight-oldHeight;
 }finally{if(seq===state.messageEpoch)$('older-messages').disabled=false;}
}
async function select(chat){
 if(state.busy||state.uploading)throw Error('正在发送或上传，请稍候切换会话');
 if(state.cid&&state.target)state.drafts[state.cid]=$('chat-text').value;rememberChat();reads.abort();reads=new AbortController();const controller=reads;
 state.cid=chat.conversation_id;state.name=chat.name;state.kind=chat.kind;state.target=null;state.auto=null;state.query='';state.messageEpoch++;
 const cached=chatCache.get(state.cid);const warm=cached&&cached.until>Date.now();state.messages=warm?cached.messages.slice():[];state.media=warm?{...cached.media}:{};state.offset=warm?cached.offset:0;state.cursor=warm?cached.cursor:null;state.file=null;
 $('reply-suggestion').hidden=true;state.suggestion=null;$('file-chip').hidden=true;$('history-form').hidden=true;$('history-query').value='';$('chat-text').value=state.drafts[state.cid]||'';$('chat-title').textContent=chat.name;$('chat-subtitle').textContent=(chat.kind==='group'?'群聊':'联系人')+' · 正在核对接收人…';$('toggle-history').disabled=false;$('chat-send-status').textContent='';$('older-messages').hidden=warm?!cached.hasMore:true;
 highlightChats();controls();autoControls();save();try{localStorage.setItem('wecom-chat.last',JSON.stringify({cid:state.cid,name:chat.name,kind:chat.kind,count:chat.count||0}));}catch(e){}renderMessages(true);if(!warm)$('chat-messages').replaceChildren(el('p','正在加载当前聊天…','empty'));
 const cid=state.cid;
 const target=api('chat/recipient?conversation_id='+encodeURIComponent(cid),null,{signal:controller.signal}).then(value=>{if(cid!==state.cid||reads!==controller)return;state.target=value;$('chat-subtitle').textContent=(chat.kind==='group'?'群聊':'联系人')+' · '+chat.count.toLocaleString()+' 条已导入消息';$('chat-backend').value=chat.kind==='group'?'desktop_ocr':'native';controls();autoControls();void autoStatus().catch(()=>{});}).catch(error=>{if(cid===state.cid&&reads===controller&&error.name!=='AbortError'){$('chat-subtitle').textContent=error.message;controls();}});
 await Promise.allSettled([target,messages(false,true).catch(error=>{if(cid===state.cid&&reads===controller&&error.name!=='AbortError'){$('chat-messages').replaceChildren(el('p',error.message,'empty'));toast(error.message);}})]);
}

async function choose(file){if(!file)return;if(!state.target||state.busy||state.uploading)throw Error('请先选择可发送的会话');if($('chat-text').value.trim())throw Error('请先发送文字，再选择文件');if(!file.size||file.size>50*1048576)throw Error('文件限 1 字节至 50 MB');state.uploading=true;controls();$('chat-send-status').textContent='正在准备文件：'+file.name;try{const r=await fetch('/api/wecom/chat/files?'+new URLSearchParams({name:file.name}),{method:'POST',headers:{'Content-Type':'application/octet-stream','X-ChatWeave-UI':'1'},body:file});const data=await r.json();if(!r.ok)throw Error(data.detail||'文件上传未完成');state.file=data;$('file-label').textContent='文件 · '+data.name+' · '+(data.size/1048576).toFixed(2)+' MB';$('file-chip').hidden=false;$('chat-send-status').textContent='点击发送后，将切换到企微窗口发送所选文件。';}finally{state.uploading=false;controls();$('file-input').value='';if(state.file)$('chat-text').focus();}}
async function send(){if($('chat-send').disabled)return;const content=$('chat-text').value,file=state.file,cid=state.cid;state.busy=true;controls();let entry,timer;try{const prepared=await api('chat/prepare',{conversation_id:cid,message:file?'':content,file_id:file?.file_id,backend:$('chat-backend').value,key:'chat-'+crypto.randomUUID()});entry={cid,key:prepared.key,dispatch_id:prepared.dispatch_id,content:file?'文件：'+file.name:content,is_self:true,timestamp:Date.now(),state:'SENDING',note:'正在提交…'};state.outbox.push(entry);save();renderOutbox();$('chat-text').value='';state.drafts[cid]='';state.file=null;$('file-chip').hidden=true;$('message-scroll').scrollTop=$('message-scroll').scrollHeight;let polling=false;timer=setInterval(async()=>{if(polling)return;polling=true;try{const r=await api('console/send-status?key='+encodeURIComponent(entry.key));if(r.data?.state==='SENDING'){entry.note=(r.data.stage||'正在发送')+'…';renderOutbox();}}catch{}finally{polling=false;}},1500);const response=await api('chat/send',{dispatch_id:prepared.dispatch_id});const data=response.data||{};entry.state=['SERVER_RECORDED','VISIBLE_OUTGOING','NOT_SENT','UNKNOWN'].includes(data.state)?data.state:'UNKNOWN';entry.native_message_id=data.native_message_id;entry.note=data.note||response.message||'结果未知，请核对企微，不要重发';if(entry.state==='SERVER_RECORDED')entry.note='已发送 · 服务器回执已核对';if(entry.state==='NOT_SENT'&&!file){state.drafts[cid]=content;$('chat-text').value=content;}$('chat-send-status').textContent=entry.note;}catch(e){if(entry){entry.state='UNKNOWN';entry.note=e.message+' 请核对企微，不要重发';}else toast(e.message);}finally{clearInterval(timer);state.busy=false;save();renderOutbox();controls();}}
async function resumePending(){const pending=state.outbox.filter(o=>o.state==='SENDING'&&!o.ack);if(!pending.length)return;for(const o of pending){try{const r=await api('console/send-status?key='+encodeURIComponent(o.key));o.state=r.data?.state==='PENDING'?'UNKNOWN':r.data?.state||'UNKNOWN';o.note=r.data?.note||r.data?.stage||'结果待核对';o.native_message_id=r.data?.native_message_id;}catch{o.state='UNKNOWN';o.note='连接中断，请核对企微，不会自动重发';}}save();renderOutbox();controls();if(state.outbox.some(o=>o.state==='SENDING'&&!o.ack))setTimeout(resumePending,1500);}
$('use-suggestion').onclick=guard(async()=>{if(!state.suggestion||state.suggestion.cid!==state.cid)return;if(state.busy||state.uploading||state.file)throw Error('请先完成当前发送或移除附件');if($('chat-text').value.trim())throw Error('输入框已有草稿，请先保存或清空，再填入新回复');$('chat-text').value=state.suggestion.message;state.drafts[state.cid]=state.suggestion.message;save();controls();$('reply-suggestion').hidden=true;$('chat-text').focus();});$('close-suggestion').onclick=()=>{$('reply-suggestion').hidden=true;};
setInterval(()=>{if(!document.hidden)autoStatus().catch(()=>{});},3000);
$('close-image').onclick=()=>$('image-viewer').close();$('image-viewer').onclick=e=>{if(e.target===$('image-viewer'))$('image-viewer').close();};
$('chat-composer').onsubmit=guard(send);$('chat-text').oninput=()=>{if(state.cid&&state.target)state.drafts[state.cid]=$('chat-text').value;save();controls();};$('choose-file').onclick=()=>$('file-input').click();$('file-input').onchange=guard(()=>choose($('file-input').files[0]));$('remove-file').onclick=()=>{state.file=null;$('file-chip').hidden=true;$('chat-send-status').textContent='';controls();};
for(const target of [$('chat-composer')]){target.ondragover=e=>{e.preventDefault();};target.ondrop=guard(e=>choose(e.dataTransfer.files[0]));target.onpaste=event=>{if(event.clipboardData.files.length){event.preventDefault();choose(event.clipboardData.files[0]).catch(e=>toast(e.message));}};}
$('toggle-history').onclick=()=>{$('history-form').hidden=!$('history-form').hidden;if(!$('history-form').hidden)$('history-query').focus();};$('history-form').onsubmit=guard(async()=>{state.query=$('history-query').value.trim();await messages(false,false);});$('clear-history').onclick=guard(async()=>{state.query='';$('history-query').value='';$('history-form').hidden=true;await messages(false,true);});$('older-messages').onclick=guard(()=>messages(true));$('refresh-chats').onclick=guard(async()=>{await conversations();await messages(false,true);});$('more-chats').onclick=guard(()=>conversations(true));let searchTimer;$('chat-query').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(guard(()=>conversations()),180);};
$('new-messages').onclick=guard(async()=>{$('new-messages').hidden=true;await messages(false,true);});let refreshBusy=false,initialized=false;document.addEventListener('wecom-data-changed',guard(async()=>{if(refreshBusy||!initialized)return;refreshBusy=true;try{await conversations();const box=$('message-scroll');if(state.cid){if(!state.query&&state.offset<=50&&box.scrollHeight-box.scrollTop-box.clientHeight<120)await messages(false,true);else $('new-messages').hidden=false;}}finally{refreshBusy=false;}}));
guard(async()=>{await conversations();let previous=state.chats.find(c=>c.conversation_id===state.cid)||state.chats.find(c=>{try{return c.conversation_id===JSON.parse(localStorage.getItem('wecom-chat.last')||'null')?.cid;}catch(e){return false;}});if(!previous){try{const last=JSON.parse(localStorage.getItem('wecom-chat.last')||'null');if(last&&last.cid)previous={conversation_id:last.cid,name:last.name||'聊天',kind:last.kind||'direct',count:last.count||0};}catch(e){}}if(previous)await select(previous);else state.cid='';for(const o of state.outbox.filter(o=>['SENDING','PENDING','UNKNOWN'].includes(o.state)&&!o.ack)){try{const r=await api('console/send-status?key='+encodeURIComponent(o.key));o.state=r.data?.state==='PENDING'?'UNKNOWN':r.data?.state||'UNKNOWN';o.note=r.data?.note||r.data?.stage||'上次发送结果待核对，不会自动重发';o.native_message_id=r.data?.native_message_id;}catch{o.state='UNKNOWN';o.note='上次发送结果待核对，不会自动重发';}}save();renderOutbox();controls();initialized=true;resumePending();})();
})();
