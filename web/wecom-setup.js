'use strict';
// Installed app only. Source/browser editions retain their manual import flow.
if (new URLSearchParams(location.search).get('desktop') === '1') {
  const panel=document.createElement('dialog');
  panel.setAttribute('aria-label','首次使用准备');
  const heading=document.createElement('h2');heading.textContent='正在准备你的企微工作台';
  const info=document.createElement('p');info.textContent='自动创建数据库、连接适配器并检测本机账号。';
  const progress=document.createElement('p');progress.className='small';
  const account=document.createElement('select');account.setAttribute('aria-label','本机企微账号');
  const retry=document.createElement('button');retry.className='primary';retry.textContent='重新检测';
  const enter=document.createElement('button');enter.className='quiet';enter.textContent='先进入操作台';
  const details=document.createElement('p');details.className='small';
  details.textContent='首次自动导入本机已有记录。模型密钥可在进入后设置，调用模型和发送消息仍由你操作。';
  const actions=document.createElement('div');actions.className='dialog-actions';actions.append(enter,retry);
  panel.append(heading,info,progress,account,details,actions);document.body.append(panel);
  let current=null, shown=false, dismissed=false;
  const fill=(select,values)=>{const previous=select.value;select.replaceChildren(...values.map(v=>{const o=document.createElement('option');o.value=String(v);o.textContent=String(v);return o;}));if(values.map(String).includes(previous))select.value=previous;};
  enter.onclick=()=>{dismissed=true;panel.close();};
  retry.onclick=async()=>{
    retry.disabled=true;
    try {
      const body={};
      if(current.stage==='select_account')body.account=account.value;
      if(current.stage==='select_account'&&/^\/w\/(legacy|[a-f0-9]{64})\//.test(location.pathname)){
        const response=await fetch('/api/wecom/profiles/switch',{method:'POST',headers:{'Content-Type':'application/json','X-ChatWeave-UI':'1'},body:JSON.stringify(body)});
        const result=await response.json();if(!response.ok)throw Error(result.detail||'无法切换账号');
        const url=new URL(result.url,location.origin);if(url.origin!==location.origin||!/^\/w\/(legacy|[a-f0-9]{64})\/console$/.test(url.pathname))throw Error('账号页面地址无效');window.location.assign(url.href);return;
      }
      const response=await fetch('/api/wecom/setup',{method:'POST',headers:{'Content-Type':'application/json','X-ChatWeave-UI':'1'},body:JSON.stringify(body)});
      if(!response.ok)throw Error((await response.json()).detail||'无法启动初始化');
      await poll();
    } catch(e){info.textContent=e.message;retry.disabled=false;}
  };
  async function poll(){
    try {
      const response=await fetch('/api/wecom/setup',{cache:'no-store'});
      if(response.status===404)return;
      if(!response.ok)throw Error('暂时无法取得初始化状态');
      current=await response.json();
      const ready=current.stage==='ready', busy=['starting','unlocking','importing'].includes(current.stage);
      if(!shown&&!ready&&!dismissed){panel.showModal();shown=true;}
      heading.textContent=ready?'工作台已准备好':'首次使用准备';
      info.textContent=current.message;
      progress.textContent=current.progress?.total?`已处理 ${current.progress.total.toLocaleString()} 条记录`:'';
      account.hidden=current.stage!=='select_account';
      if(!account.hidden)fill(account,current.accounts);
      retry.hidden=ready;retry.disabled=busy;retry.textContent=busy?'正在准备…':(current.stage.startsWith('select_')?'使用所选账号':'重新检测');
      enter.textContent=ready?'进入操作台':'先进入操作台';
      if(ready){if(typeof loadConversations==='function')await loadConversations();return;}
      if(busy||current.stage==='waiting_login')setTimeout(poll,busy?1500:5000);
    } catch(e){info.textContent=e.message;retry.disabled=false;}
  }
  poll();
}
