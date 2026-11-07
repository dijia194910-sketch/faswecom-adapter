'use strict';
// Keep navigation independent of data loading and the workbench's main script.
for(const link of document.querySelectorAll('a[data-wecom-nav]')){
  link.addEventListener('click',event=>{
    if(event.button!==0||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey)return;
    const page=link.dataset.wecomNav;
    if(!['console','data'].includes(page))return;
    event.preventDefault();
    const base=location.pathname.match(/^\/w\/(legacy|[a-f0-9]{64})(?=\/)/)?.[0]||'';
    const target=base+(page==='console'?'/console':'/');
    const original=link.textContent;
    link.textContent=page==='console'?'正在返回操作台…':'正在打开数据与同步…';
    link.setAttribute('aria-busy','true');
    const restore=()=>{link.textContent=original;link.removeAttribute('aria-busy');};
    window.addEventListener('pageshow',restore,{once:true});
    // Native host handles the same-window transition. Older hosts and browsers
    // retain the normal URL fallback; a completed navigation clears its timer.
    if(window.chrome?.webview){
      try{window.chrome.webview.postMessage('faswecom:navigate:'+page);}catch{}
      setTimeout(()=>window.location.assign(target),250);
    }else window.location.assign(target);
    setTimeout(restore,4000);
  });
}
