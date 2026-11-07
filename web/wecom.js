'use strict';
const $=id=>document.getElementById(id);
let catalog=[], chosen=new Set(), currentChat='', offset=0, messageCursor=null, messageEpoch=0, messageReads, loading=false, noticeTimer, polling, lastJob='', unlocking=false;
function notice(message){$('notice').textContent=message;$('notice').hidden=false;clearTimeout(noticeTimer);noticeTimer=setTimeout(()=>$('notice').hidden=true,7000);}
async function api(path,method='GET',body,options={}){const response=await fetch(path,{method,headers:body?{'Content-Type':'application/json','X-Chatweave-UI':'1'}:{},body:body?JSON.stringify(body):undefined,...options});const data=await response.json();if(!response.ok)throw Error(data.detail||'请求失败');return data;}
function run(fn){return async event=>{event?.preventDefault();try{await fn();}catch(error){if(error.name!=='AbortError')notice(error.message);}};}
function option(value,text){const el=document.createElement('option');el.value=value;el.textContent=text;return el;}
function selectedAccount(){return $('account').selectedOptions[0]?.dataset.account||'';}
function selectedSource(){return {account:selectedAccount(),source_path:$('account').value};}
function renderAccounts(accounts,bound){
  const selected=$('account').value, counts=new Map();
  for(const row of accounts)counts.set(row.account,(counts.get(row.account)||0)+1);
  $('account').replaceChildren(...accounts.map(row=>{
    const label=row.account+(row.unlocked?' · 已解锁':'')+(row.active?' · 当前使用':'')+(counts.get(row.account)>1?' · '+row.path:'');
    const item=option(row.path,label);item.dataset.account=row.account;return item;
  }));
  if(accounts.some(row=>row.path===selected))$('account').value=selected;
  else {const preferred=accounts.find(row=>row.account===bound&&row.selected)||accounts.find(row=>row.account===bound)||accounts.find(row=>row.selected&&row.active)||accounts.find(row=>row.selected);if(preferred)$('account').value=preferred.path;}
  const row=accounts.find(row=>row.path===$('account').value);
  $('source-hint').textContent=!accounts.length?'未找到账号数据库。请保持企微已登录；如果数据保存在其他位置，可在下方指定目录。':`已发现 ${counts.size} 个本机账号。`+(accounts.length>counts.size?' 同一账号有多个目录，请选择正在使用的目录。':'')+(row?' 当前目录：'+row.path:'');
}
async function status(refresh=false){const value=await api('/api/wecom/status'+(refresh?'?refresh=true':''));renderAccounts(value.accounts,value.workspace_account||value.bound_account);$('process-state').textContent=value.processes.length?`自动检测：${value.processes.length} 个 WXWork.exe`:'等待企业微信启动';$('message-count').textContent=value.data.messages.toLocaleString();$('conversation-count').textContent=value.data.conversations.toLocaleString();$('source-status').textContent=value.accounts.some(x=>x.path===$('account').value&&x.unlocked)?'已解锁':value.processes.length?(value.accounts.length?'待解锁':'未找到数据目录'):'请登录企微';const job=value.job;const busy=job.state==='running';$('import').disabled=busy;$('unlock').disabled=busy||unlocking||!value.accounts.length;$('catalog').disabled=busy||unlocking||!value.accounts.length;$('account').disabled=busy||unlocking;const p=job.progress||{};if(busy)$('progress').textContent=`正在导入：新增 ${p.imported||0} 条，已有 ${p.duplicate||0} 条…`;if(job.state==='complete')$('progress').textContent=`导入完成：新增 ${p.imported||0} 条，已有 ${p.duplicate||0} 条；文本 ${p.text||0} 条，未解析消息 ${p.placeholders||0} 条。`;if(job.state==='failed')$('progress').textContent=job.error;if(lastJob==='running'&&!busy){clearInterval(polling);polling=null;await conversations();await messages(true);}lastJob=job.state;if(busy&&!polling)polling=setInterval(()=>status().catch(e=>notice(e.message)),1500);return value;}
function renderCatalog(){const query=$('catalog-filter').value.toLocaleLowerCase();$('catalog-list').replaceChildren();for(const row of catalog.filter(x=>x.name.toLocaleLowerCase().includes(query))){const label=document.createElement('label'),input=document.createElement('input'),span=document.createElement('span');input.type='checkbox';input.checked=chosen.has(row.id);input.disabled=$('all-chats').checked;input.addEventListener('change',()=>{if(input.checked)chosen.add(row.id);else chosen.delete(row.id);selectionHint();});span.textContent=row.name+' · '+row.count+' 条';label.append(input,span);$('catalog-list').append(label);}selectionHint();}
function selectionHint(){$('selection-hint').textContent=$('all-chats').checked?`将读取全部 ${catalog.length} 个本机会话`:`已选择 ${chosen.size} 个会话`;}
async function loadCatalog(){const data=await api('/api/wecom/catalog?'+new URLSearchParams(selectedSource()));catalog=data.conversations;chosen=new Set([...chosen].filter(id=>catalog.some(r=>r.id===id)));$('catalog-area').hidden=false;renderCatalog();}
async function conversations(){const data=await api('/api/wecom/conversations');$('conversations').replaceChildren();for(const row of data.conversations){const button=document.createElement('button');button.className='conversation'+(row.conversation_id===currentChat?' active':'');const name=document.createElement('strong'),count=document.createElement('span');name.textContent=row.name;count.textContent=row.count+' 条消息';button.append(name,count);button.addEventListener('click',run(async()=>{currentChat=row.conversation_id;$('query').value='';$('chat-heading').textContent=row.name;await messages(true);await conversations();}));$('conversations').append(button);}}
async function messages(reset=false,anchor=null){
 if(loading&&!reset)return;
 messageReads?.abort();messageReads=new AbortController();const seq=++messageEpoch;
 loading=true;$('more').disabled=true;
 try{
  if(reset){offset=0;messageCursor=null;$('messages').replaceChildren();$('live-refresh').hidden=true;}
  const page=messageCursor?{before_timestamp:messageCursor.timestamp,before_id:messageCursor.id}:{offset};
  const params=new URLSearchParams({conversation_id:currentChat,query:$('query').value,...page});if(anchor)params.set('anchor',anchor);
  const data=await api('/api/wecom/messages?'+params,'GET',undefined,{signal:messageReads.signal});if(seq!==messageEpoch)return;
  if(anchor){currentChat=data.conversation_id;$('query').value='';$('chat-heading').textContent=data.messages[0]?.conversation||'消息来源';await conversations();if(seq!==messageEpoch)return;}
  const fragment=document.createDocumentFragment();
  for(const row of data.messages){
   if($('message-'+row.id))continue;
   const card=document.createElement('article');card.id='message-'+row.id;card.className='message'+(row.is_self?' self':'')+(row.id===Number(anchor)?' cited':'');
   const meta=document.createElement('div'),sender=document.createElement('span'),time=document.createElement('time'),text=document.createElement('div');
   meta.className='meta';sender.className='sender';sender.textContent=row.sender+(row.is_self?'（我）':'');time.textContent=row.time;meta.append(sender,time);
   text.className='text';text.textContent=row.content;card.append(meta);
   if(!currentChat){const chat=document.createElement('div');chat.className='chat-name';chat.textContent=row.conversation;card.append(chat);}
   card.append(text);fragment.append(card);offset++;
  }
  $('messages').append(fragment);
  if(reset&&!data.messages.length){const empty=document.createElement('p');empty.className='empty';empty.textContent='没有找到消息。可以调整关键词，或先导入所需会话。';$('messages').append(empty);}
  $('more').hidden=!data.has_more;messageCursor=data.next_cursor;if(anchor)$('message-'+anchor)?.scrollIntoView({block:'center'});
 }finally{if(seq===messageEpoch){loading=false;$('more').disabled=false;}}
}
async function mcpStatus(){if(!$('mcp-state'))return {};const data=await api('/api/mcp');$('mcp-state').textContent=data.running?'MCP 已开启':'尚未开启';$('mcp-port').value=data.port||18778;$('connections').replaceChildren();for(const row of data.connections||[]){const div=document.createElement('div'),name=document.createElement('span'),button=document.createElement('button');div.className='connection-row';name.textContent=row.name+(row.revoked?' · 已撤销':'');button.textContent='撤销连接';button.className='secondary';button.disabled=!!row.revoked;button.addEventListener('click',run(async()=>{await api('/api/mcp/connections/'+encodeURIComponent(row.id)+'/revoke','POST',{});$('config').textContent='';$('connection-result').hidden=true;await mcpStatus();}));div.append(name,button);$('connections').append(div);}return data;}
$('detect').addEventListener('click',run(()=>status(true)));$('account').addEventListener('change',()=>{catalog=[];chosen.clear();$('all-chats').checked=false;$('catalog-area').hidden=true;selectionHint();});$('unlock').addEventListener('click',run(async()=>{unlocking=true;$('account').disabled=true;$('catalog').disabled=true;$('unlock').disabled=true;$('unlock').textContent='正在解锁…';try{await api('/api/wecom/unlock','POST',selectedSource());notice('已从正在运行的企业微信解锁读取器。');await status(true);await loadCatalog();}finally{unlocking=false;$('unlock').disabled=false;$('unlock').textContent='解锁读取器';$('account').disabled=false;$('catalog').disabled=false;}}));$('catalog').addEventListener('click',run(loadCatalog));$('all-chats').addEventListener('change',renderCatalog);$('catalog-filter').addEventListener('input',renderCatalog);
$('import').addEventListener('click',run(async()=>{if(!$('all-chats').checked&&!chosen.size)throw Error('请先选择要读取的会话。');await api('/api/wecom/import','POST',{...selectedSource(),conversations:[...chosen],all_conversations:$('all-chats').checked,start:$('start').value,end:$('end').value});await status();}));$('search-form').addEventListener('submit',run(()=>messages(true)));$('all-imported').addEventListener('click',run(async()=>{currentChat='';$('chat-heading').textContent='最近消息';await conversations();await messages(true);}));$('more').addEventListener('click',run(()=>messages()));
if($('connect'))$('connect').addEventListener('click',run(async()=>{const all=$('grant-all').checked;if(!all&&!chosen.size)throw Error('请选择授权会话，或勾选允许查询全部已导入会话。');const port=Number($('mcp-port').value);await api('/api/mcp','PUT',{enabled:true,port});const result=await api('/api/mcp/connections','POST',{name:$('connection-name').value,send:$('grant-send').checked,platforms:['wecom'],conversations:all?[]:[...chosen].map(id=>['wecom',selectedAccount()+':'+id]),all_conversations:all,start:$('start').value,end:$('end').value});$('config').textContent='[mcp_servers.faswecom]\nurl = "http://127.0.0.1:'+port+'/mcp"\nhttp_headers = { Authorization = "Bearer '+result.token+'" }\ntool_timeout_sec = 240';$('connection-result').hidden=false;await mcpStatus();}));if($('copy-config'))$('copy-config').addEventListener('click',run(async()=>{await navigator.clipboard.writeText($('config').textContent);notice('配置已复制。');}));if($('hide-config'))$('hide-config').addEventListener('click',()=>{$('config').textContent='';$('connection-result').hidden=true;});
run(async()=>{const anchor=new URLSearchParams(location.search).get('anchor');const results=await Promise.allSettled([status(),conversations(),messages(true,/^[1-9][0-9]*$/.test(anchor||'')?anchor:null),mcpStatus(),api('/api/wecom/summary').then(data=>{$('message-count').textContent=data.messages.toLocaleString();$('conversation-count').textContent=data.conversations.toLocaleString();})]);for(const result of results)if(result.status==='rejected')notice(result.reason.message);})();

let liveBrowserBusy=false;
document.addEventListener('wecom-data-changed',async event=>{
  if(liveBrowserBusy)return;
  liveBrowserBusy=true;
  try{
    const data=event.detail;
    $('message-count').textContent=data.messages.toLocaleString();
    $('conversation-count').textContent=data.conversations.toLocaleString();
    await conversations();
    if(!loading&&offset<=50&&document.querySelector('.timeline').scrollTop<80)await messages(true);
    else $('live-refresh').hidden=false;
  }catch(error){notice(error.message);}finally{liveBrowserBusy=false;}
});
$('live-refresh').addEventListener('click',run(()=>messages(true)));

setInterval(()=>{if(!document.hidden&&!polling)status().catch(()=>{});},5000);

$('source-save').addEventListener('click',run(async()=>{await api('/api/wecom/source','POST',{path:$('source-path').value});await status(true);document.dispatchEvent(new Event('wecom-accounts-changed'));notice('数据目录已识别，请选择账号后解锁读取器。');}));
