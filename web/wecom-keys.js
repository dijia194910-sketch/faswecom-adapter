'use strict';
// Only message composers use Enter to submit. IME commits remain untouched.
for(const [inputId,formId,buttonId,fallbackId] of [['chat-text','chat-composer','chat-send'],['question','ask-form','ask-model','local-search']]){
  const input=document.getElementById(inputId),form=document.getElementById(formId);if(!input||!form)continue;
  let composing=false;input.addEventListener('compositionstart',()=>{composing=true;});input.addEventListener('compositionend',()=>{setTimeout(()=>{composing=false;},0);});
  input.addEventListener('keydown',event=>{
    if(event.key!=='Enter'||event.shiftKey||event.isComposing||event.keyCode===229||composing)return;
    event.preventDefault();if(event.repeat)return;
    const submit=document.getElementById(buttonId),fallback=document.getElementById(fallbackId);
    if(submit&&!submit.disabled)form.requestSubmit(submit);else if(fallback&&!fallback.disabled)fallback.click();
  });
}
