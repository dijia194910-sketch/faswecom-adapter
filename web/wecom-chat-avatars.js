'use strict';
// At most two optional image requests. Text and recipient requests keep their
// browser connections even if the avatar CDN is slow or unreachable.
(()=>{
 const cache=new Map(),waiting=new Map(),metadata=new WeakMap(),observed=new Set();let active=0,timer;
 const base=location.pathname.match(/^\/w\/(legacy|[a-f0-9]{64})(?=\/)/)?.[0]||'';
 const observer=new IntersectionObserver(entries=>{for(const entry of entries)if(entry.isIntersecting){observer.unobserve(entry.target);observed.delete(entry.target);enqueue(entry.target);}},{rootMargin:'60px'});
 function prune(){for(const node of observed)if(!node.isConnected){observer.unobserve(node);observed.delete(node);}}
 function apply(node,item){if(!node.isConnected||!item.url)return;const image=document.createElement('img');image.alt='';image.decoding='async';image.src=item.url;image.onerror=()=>image.remove();node.append(image);}
 function trim(){while(cache.size>256){const [key,item]=cache.entries().next().value;if(item.url)URL.revokeObjectURL(item.url);cache.delete(key);}}
 function enqueue(node){
  if(!node.isConnected)return;const info=metadata.get(node),old=cache.get(info.key);
  if(old&&old.until>Date.now()){apply(node,old);return;}
  if(old?.url)URL.revokeObjectURL(old.url);cache.delete(info.key);
  let task=waiting.get(info.key);if(task){task.nodes.add(node);return;}
  task={...info,nodes:new Set([node]),running:false};waiting.set(info.key,task);schedule();
 }
 function schedule(){clearTimeout(timer);timer=setTimeout(pump,60);}
 function pump(){
  for(const [key,task] of waiting){
   if(active>=2)break;if(task.running)continue;
   task.nodes=new Set([...task.nodes].filter(node=>node.isConnected));if(!task.nodes.size){waiting.delete(key);continue;}
   task.running=true;active++;void load(task).finally(()=>{active--;waiting.delete(key);schedule();});
  }
 }
 async function load(task){
  let item={until:Date.now()+60000};const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),6500);
  try{
   const response=await fetch(base+'/api/wecom/chat/avatar?'+new URLSearchParams({conversation_id:task.cid,sender_id:task.sender||'',...(task.mid?{message_id:task.mid}:{})}),{signal:controller.signal,priority:'low'});
   if(response.ok&&response.headers.get('content-type')?.startsWith('image/png'))item={url:URL.createObjectURL(await response.blob()),until:Date.now()+1800000};
  }catch{}finally{clearTimeout(timeout);}
  cache.set(task.key,item);trim();for(const node of task.nodes)apply(node,item);
 }
 function node(cid,sender,label,group=false,mid=null){
  const span=document.createElement('span');span.textContent=label;span.className='avatar'+(group?' group':'');
  if(!group&&cid){const match=cid.match(/^(\d+):S:(\d+)_(\d+)$/);if(!sender&&match)sender=match.slice(2).find(v=>v!==match[1])||'';
   const key=cid.split(':',1)[0]+':'+(sender||cid),info={key,cid,sender,mid};metadata.set(span,info);
   const old=cache.get(key);if(old&&old.until>Date.now()){if(old.url){const image=document.createElement('img');image.alt='';image.src=old.url;span.append(image);}}else{observer.observe(span);observed.add(span);queueMicrotask(prune);}
  }
  return span;
 }
 window.WeComAvatars={node};
})();
