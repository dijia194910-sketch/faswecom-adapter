'use strict';
// Each account has its own URL namespace. Old pages cannot act on a new account.
(()=>{
  const match=location.pathname.match(/^\/w\/(legacy|[a-f0-9]{64})(?=\/)/);
  if(!match)return;
  const base=match[0],originalFetch=window.fetch.bind(window);
  const scoped=value=>{
    const url=new URL(value,location.href);
    if(url.origin===location.origin&&url.pathname.startsWith('/api/'))url.pathname=base+url.pathname;
    return url.href;
  };
  window.fetch=(input,init)=>originalFetch(input instanceof Request?new Request(scoped(input.url),input):scoped(input),init);
  document.addEventListener('click',event=>{
    const link=event.target.closest('a[href]');
    if(!link)return;
    const url=new URL(link.href,location.href);
    if(url.origin===location.origin&&!url.pathname.startsWith('/w/')&&!url.pathname.startsWith('/ui/')){
      url.pathname=base+url.pathname;link.href=url.href;
    }
  },true);
  const box=document.createElement('section');box.className='account-switcher';box.setAttribute('aria-label','企微账号切换');
  const label=document.createElement('label');label.textContent='当前企微账号';
  const select=document.createElement('select');select.setAttribute('aria-label','切换企微账号');
  const button=document.createElement('button');button.type='button';button.textContent='切换账号';button.disabled=true;
  const refresh=document.createElement('button');refresh.type='button';refresh.className='account-refresh';refresh.textContent='重新检测';
  const info=document.createElement('p');info.setAttribute('role','status');info.textContent='每个账号独立保存聊天与配置。';
  label.append(select);box.append(label,button,refresh,info);
  const sidebar=document.querySelector('.sidebar');
  if(sidebar)sidebar.querySelector('.brand').after(box);
  else document.querySelector('main').prepend(box);
  let current=null,switching=false,needsSelection=false;
  function enable(){button.disabled=switching||!select.value||(!needsSelection&&select.value===current);}
  async function update(){
    const response=await fetch('/api/wecom/profiles',{cache:'no-store'});
    const data=await response.json();
    if(!response.ok)throw Error(data.detail||'账号列表暂时不可用');
    current=data.current_account;
    needsSelection=data.needs_selection;button.textContent=needsSelection?'使用所选账号':'切换账号';
    const previous=select.value;
    select.replaceChildren(...data.accounts.map(row=>{
      const item=document.createElement('option');item.value=row.account;
      item.textContent=row.account+(row.active?' · 当前账号':'')+(!row.available?' · 等待登录':'');return item;
    }));
    if(data.accounts.some(row=>row.account===previous)&&previous!==current)select.value=previous;
    else if(current)select.value=current;
    label.firstChild.textContent=current?'当前企微账号':'选择企微账号';enable();
    return data;
  }
  select.addEventListener('change',enable);
  refresh.addEventListener('click',()=>update().then(()=>{info.textContent='每个账号独立保存聊天与配置。';}).catch(error=>{info.textContent=error.message;}));
  document.addEventListener('wecom-accounts-changed',()=>update().catch(error=>{info.textContent=error.message;}));
  button.addEventListener('click',async()=>{
    switching=true;enable();select.disabled=true;refresh.disabled=true;button.textContent='正在切换…';
    try{
      const response=await fetch('/api/wecom/profiles/switch',{method:'POST',headers:{'Content-Type':'application/json','X-ChatWeave-UI':'1'},body:JSON.stringify({account:select.value})});
      const data=await response.json();if(!response.ok)throw Error(data.detail||'账号切换未完成');
      const url=new URL(data.url,location.origin);
      if(url.origin!==location.origin||!/^\/w\/(legacy|[a-f0-9]{64})\/console$/.test(url.pathname))throw Error('账号页面地址无效');
      window.location.assign(url.href);
    }catch(error){info.textContent=error.message;switching=false;select.disabled=false;refresh.disabled=false;button.textContent='切换账号';enable();}
  });
  update().catch(error=>{info.textContent=error.message;});
})();
