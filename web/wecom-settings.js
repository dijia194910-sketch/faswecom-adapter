'use strict';
const $ = id => document.getElementById(id);
function notice(text){const n=$('notice');n.textContent=text;n.hidden=false;clearTimeout(n._t);n._t=setTimeout(()=>{n.hidden=true;},6000);}
async function api(path, method, body){
  const opts = {method: method || 'GET', headers: {}};
  if (body !== undefined) { opts.headers['Content-Type']='application/json'; opts.headers['X-ChatWeave-UI']='1'; opts.body=JSON.stringify(body); }
  const response = await fetch(path, opts);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(data.detail || ('HTTP ' + response.status));
  return data;
}
async function mcpStatus(){
  const data = await api('/api/mcp');
  $('mcp-state').textContent = data.running ? 'MCP 已开启' : '尚未开启';
  $('mcp-port').value = data.port || 18778;
  $('connections').replaceChildren();
  for (const row of data.connections || []) {
    const div = document.createElement('div');
    const name = document.createElement('span');
    const button = document.createElement('button');
    div.className = 'connection-row';
    name.textContent = row.name + (row.revoked ? ' · 已撤销' : '');
    button.textContent = '撤销连接';
    button.className = 'secondary';
    button.disabled = !!row.revoked;
    button.addEventListener('click', async () => {
      try { await api('/api/mcp/connections/' + encodeURIComponent(row.id) + '/revoke', 'POST', {}); $('config').textContent=''; $('connection-result').hidden=true; await mcpStatus(); }
      catch (error) { notice(error.message); }
    });
    div.append(name, button);
    $('connections').append(div);
  }
  return data;
}
$('connect').addEventListener('click', async () => {
  try {
    const port = Number($('mcp-port').value);
    await api('/api/mcp', 'PUT', {enabled: true, port});
    const result = await api('/api/mcp/connections', 'POST', {
      name: $('connection-name').value,
      send: $('grant-send').checked,
      platforms: ['wecom'],
      conversations: [],
      all_conversations: true
    });
    $('config').textContent = '[mcp_servers.faswecom]\nurl = "http://127.0.0.1:' + port + '/mcp"\nhttp_headers = { Authorization = "Bearer ' + result.token + '" }\ntool_timeout_sec = 240';
    $('connection-result').hidden = false;
    await mcpStatus();
    notice('MCP 已开启，连接已创建。');
  } catch (error) { notice(error.message); }
});
$('copy-config').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText($('config').textContent); notice('配置已复制。'); }
  catch (error) { notice('复制失败，请手动选择文本。'); }
});
$('hide-config').addEventListener('click', () => { $('config').textContent=''; $('connection-result').hidden=true; });
mcpStatus().catch(error => notice(error.message));
