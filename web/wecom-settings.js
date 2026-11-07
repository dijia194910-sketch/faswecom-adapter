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
  const port = data.port || 18778;
  $('mcp-port').value = port;
  $('mcp-port-info').textContent = 'MCP 端口：' + port + '（全局，所有连接共用）';
  const list = $('connections');
  list.replaceChildren();
  if (!(data.connections || []).length) {
    const p = document.createElement('p');
    p.className = 'hint';
    p.textContent = '还没有创建过连接。';
    list.appendChild(p);
    return data;
  }
  for (const row of data.connections || []) {
    const div = document.createElement('div');
    div.className = 'conn-row' + (row.revoked ? ' revoked' : '');
    const main = document.createElement('div');
    const name = document.createElement('div');
    name.className = 'conn-name';
    name.textContent = row.name;
    const meta = document.createElement('div');
    meta.className = 'conn-meta';
    const created = row.created_at ? new Date(row.created_at * 1000).toLocaleString() : '—';
    meta.textContent = '创建于 ' + created + ' · 调用 ' + (row.calls || 0) + ' 次 · 端口 ' + port;
    main.append(name, meta);
    const flags = document.createElement('div');
    flags.className = 'conn-flags';
    const flag = (on, label) => {
      const span = document.createElement('span');
      span.className = 'conn-flag' + (on ? '' : ' off');
      span.textContent = label + '：' + (on ? '是' : '否');
      return span;
    };
    const scope = row.scope || {};
    const allConversations = scope.all_conversations === undefined
      ? !(scope.conversations && scope.conversations.length)
      : scope.all_conversations;
    flags.append(flag(allConversations, '查询全部会话'), flag(scope.send, '直接发送'));
    const state = document.createElement('div');
    state.className = 'conn-state' + (row.revoked ? ' revoked' : '');
    state.textContent = row.revoked ? '已撤销' : (row.valid ? '有效' : '已失效');
    const button = document.createElement('button');
    button.className = 'secondary';
    button.textContent = '撤销连接';
    button.disabled = !!row.revoked;
    button.addEventListener('click', async () => {
      try {
        await api('/api/mcp/connections/' + encodeURIComponent(row.id) + '/revoke', 'POST', {});
        $('config').textContent = '';
        $('connection-result').hidden = true;
        await mcpStatus();
      } catch (error) { notice(error.message); }
    });
    div.append(main, flags, state, button);
    list.appendChild(div);
  }
  return data;
}

const mcpDialog = $('mcp-create-dialog');
function openCreateDialog(){
  mcpDialog.showModal();
  $('connection-name').focus();
}
$('connect').addEventListener('click', openCreateDialog);
$('mcp-create-cancel').addEventListener('click', () => mcpDialog.close());
$('mcp-create-cancel2').addEventListener('click', () => mcpDialog.close());
$('mcp-create-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const port = Number($('mcp-port').value);
    await api('/api/mcp', 'PUT', {enabled: true, port});
    const result = await api('/api/mcp/connections', 'POST', {
      name: $('connection-name').value,
      send: $('grant-send').checked,
      platforms: ['wecom'],
      conversations: [],
      all_conversations: $('grant-all').checked
    });
    $('config').textContent = '[mcp_servers.faswecom]\nurl = "http://127.0.0.1:' + port + '/mcp"\nhttp_headers = { Authorization = "Bearer ' + result.token + '" }\ntool_timeout_sec = 240';
    $('connection-result').hidden = false;
    mcpDialog.close();
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
// 模型设置（列表 + 当前模型 + 余额）
let modelCatalog = {models: [], active: null};
let modelEditing = null;

function modelRow(m){
  const row = document.createElement('div');
  row.className = 'model-row' + (m.id === modelCatalog.active ? ' current' : '');
  const info = document.createElement('div');
  const name = document.createElement('div');
  name.className = 'model-name';
  name.textContent = m.name;
  if (m.id === modelCatalog.active) { const t = document.createElement('span'); t.className='model-tag'; t.textContent='使用中'; name.appendChild(t); }
  const enabled = m.enabled !== false;
  const state = document.createElement('span');
  state.className = 'model-tag' + (enabled ? '' : ' off');
  state.textContent = enabled ? '已启用' : '已停用';
  name.appendChild(state);
  if (!m.has_api_key) { const k = document.createElement('span'); k.className='model-tag off'; k.textContent='待补密钥'; name.appendChild(k); }
  const meta = document.createElement('div');
  meta.className = 'model-meta';
  meta.textContent = (m.provider || '') + ' · ' + (m.model || '') + ' · ' + (m.api_base || '');
  info.append(name, meta);
  const actions = document.createElement('div');
  actions.className = 'model-actions';
  const balance = document.createElement('span');
  balance.className = 'model-balance';
  balance.textContent = m.has_api_key ? '余额查询中…' : '余额：未配置密钥';
  actions.appendChild(balance);
  if (m.id !== modelCatalog.active && enabled) {
    const use = document.createElement('button');
    use.className = 'secondary';
    use.textContent = '使用';
    use.onclick = async () => {
      try { await api('/api/wecom/console/model-selection', 'PUT', {model_id: m.id}); await modelStatus(); notice('已切换当前模型。'); }
      catch (error) { notice('切换失败：' + error.message); }
    };
    actions.appendChild(use);
  }
  const toggle = document.createElement('button');
  toggle.className = 'secondary';
  toggle.textContent = enabled ? '停用' : '启用';
  toggle.onclick = async () => {
    try { await api('/api/wecom/console/models/' + encodeURIComponent(m.id) + '/enabled', 'POST', {enabled: !enabled}); await modelStatus(); }
    catch (error) { notice('操作失败：' + error.message); }
  };
  actions.appendChild(toggle);
  const edit = document.createElement('button');
  edit.className = 'secondary';
  edit.textContent = '编辑';
  edit.onclick = () => openEditor(m);
  actions.appendChild(edit);
  row.append(info, actions);
  if (m.has_api_key && m.provider === 'deepseek') {
    api('/api/wecom/console/models/' + encodeURIComponent(m.id) + '/balance').then(data => {
      if (data.supported) balance.textContent = '余额：' + data.total + ' ' + (data.currency || 'CNY') + (data.available === false ? '（不足）' : '');
      else balance.textContent = '余额：' + (data.reason || '不可查询');
    }).catch(error => { balance.textContent = '余额：查询失败'; });
  } else if (m.has_api_key) {
    balance.textContent = '余额：该接口不提供查询';
  }
  return row;
}

async function modelStatus(){
  const data = await api('/api/wecom/console/status');
  modelCatalog = data.model_catalog || {models: [], active: null};
  const current = modelCatalog.models.find(m => m.id === modelCatalog.active);
  $('model-current').textContent = current
    ? '当前使用：' + current.name + '（' + (current.provider || '') + ' · ' + (current.model || '') + '）'
    : '当前使用：未配置';
  $('model-state').textContent = current ? (current.has_api_key ? '已配置' : '待补密钥') : '未配置';
  $('model-list').replaceChildren(...modelCatalog.models.map(modelRow));
}

function openEditor(m){
  modelEditing = m || null;
  $('model-editor').hidden = false;
  $('model-label').value = m ? m.name : '';
  $('model-provider').value = m ? (m.provider || 'openai-compatible') : 'openai-compatible';
  $('model-base').value = m ? (m.api_base || '') : '';
  $('model-name').value = m ? (m.model || '') : '';
  $('model-key').value = '';
  $('model-delete').hidden = !m;
  $('model-msg').textContent = '';
  $('model-editor').scrollIntoView({block:'nearest'});
}
function closeEditor(){ $('model-editor').hidden = true; modelEditing = null; }

$('model-add').addEventListener('click', () => openEditor(null));
$('model-cancel').addEventListener('click', closeEditor);
$('model-save').addEventListener('click', async () => {
  const body = {
    name: $('model-label').value.trim(),
    provider: $('model-provider').value,
    api_base: $('model-base').value.trim(),
    model: $('model-name').value.trim(),
    api_key: $('model-key').value
  };
  if (modelEditing) body.id = modelEditing.id;
  try {
    await api('/api/wecom/console/models', 'POST', body);
    $('model-msg').textContent = '模型设置已保存。';
    closeEditor();
    await modelStatus();
    notice('模型设置已保存。');
  } catch (error) { $('model-msg').textContent = '保存失败：' + error.message; }
});
$('model-delete').addEventListener('click', async () => {
  if (!modelEditing) return;
  if (!confirm('确定删除模型「' + modelEditing.name + '」？')) return;
  try {
    await api('/api/wecom/console/models/' + encodeURIComponent(modelEditing.id) + '/delete', 'POST', {});
    $('model-msg').textContent = '模型已删除。';
    closeEditor();
    await modelStatus();
  } catch (error) { $('model-msg').textContent = '删除失败：' + error.message; }
});

modelStatus().catch(error => { $('model-msg').textContent = '读取模型失败：' + error.message; });
// 发送总开关 / 不可发送名单
let sendState = {enabled: false, blocked: []};
const kindLabel = kind => kind === 'group' ? '群聊' : '单聊';

function blockedChecks(){ return [...document.querySelectorAll('.blocked-pick')]; }
function selectedBlockedIds(){ return blockedChecks().filter(c => c.checked).map(c => c.dataset.id); }
function updateRemoveSelected(){
  const n = selectedBlockedIds().length;
  const btn = $('blocked-remove-selected');
  if (btn) { btn.disabled = !n; btn.textContent = n ? '移出选中（' + n + '）' : '移出选中'; }
  const all = $('blocked-all');
  if (all) all.checked = n > 0 && n === blockedChecks().length;
}
async function removeBlockedIds(ids){
  for (const id of ids) {
    await api('/api/wecom/console/blocked/' + encodeURIComponent(id) + '/delete', 'POST', {});
  }
}
function renderBlocked(){
  const tbody = $('blocked-list');
  tbody.replaceChildren();
  if (!sendState.blocked.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 4;
    td.className = 'empty-cell';
    td.textContent = '名单为空：默认所有联系人都可以发送。';
    tr.appendChild(td);
    tbody.appendChild(tr);
    updateRemoveSelected();
    return;
  }
  for (const b of sendState.blocked) {
    const tr = document.createElement('tr');
    const check = document.createElement('td');
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.className = 'blocked-pick';
    box.dataset.id = b.id;
    check.appendChild(box);
    const name = document.createElement('td');
    name.textContent = b.name;
    const kind = document.createElement('td');
    kind.textContent = kindLabel(b.kind);
    const action = document.createElement('td');
    const btn = document.createElement('button');
    btn.className = 'secondary';
    btn.textContent = '移出名单';
    btn.onclick = async () => {
      try { await removeBlockedIds([b.id]); await sendStatus(); }
      catch (error) { $('send-msg').textContent = '操作失败：' + error.message; }
    };
    action.appendChild(btn);
    tr.append(check, name, kind, action);
    tbody.appendChild(tr);
  }
  updateRemoveSelected();
}

async function sendStatus(){
  const data = await api('/api/wecom/console/status');
  sendState = {enabled: data.send_enabled === true, blocked: data.blocked || []};
  $('send-enabled').checked = sendState.enabled;
  $('send-state').textContent = sendState.enabled ? '已开启' : '已关闭';
  renderBlocked();
  if (!$('blocked-options').hidden) renderOptions($('blocked-search').value);
}

$('send-enabled').addEventListener('change', async () => {
  const enabled = $('send-enabled').checked;
  try {
    await api('/api/wecom/console/send-enabled', 'PUT', {enabled});
    $('send-msg').textContent = enabled ? '发送总开关已打开：默认所有联系人可发送。' : '发送总开关已关闭，MCP 代发权限已撤销。';
    await sendStatus();
  } catch (error) {
    $('send-enabled').checked = !enabled;
    $('send-msg').textContent = '操作失败：' + error.message;
  }
});

let recipientChoices = [];
const chosen = new Map();

function updateChosen(){
  const names = [...chosen.values()].map(r => r.name);
  $('blocked-selected').textContent = names.length ? ('已选择 ' + names.length + ' 个：' + names.join('、')) : '尚未选择联系人';
  $('blocked-add').disabled = !names.length;
  $('blocked-add').textContent = names.length ? ('添加到不可发送名单（' + names.length + '）') : '添加到不可发送名单';
}

function renderOptions(keyword){
  const box = $('blocked-options');
  const kw = (keyword || '').trim().toLowerCase();
  const blockedIds = new Set((sendState.blocked || []).map(b => b.id));
  const rows = recipientChoices
    .filter(r => !blockedIds.has(r.id))
    .filter(r => !kw || r.name.toLowerCase().includes(kw))
    .slice(0, 50);
  box.replaceChildren();
  if (!rows.length) {
    const d = document.createElement('div');
    d.className = 'combo-item';
    const remaining = recipientChoices.filter(r => !blockedIds.has(r.id));
    d.textContent = remaining.length ? '没有匹配的联系人' : '没有可添加的联系人（都已在名单中，或还没同步聊天）';
    box.appendChild(d);
  } else {
    for (const r of rows) {
      const item = document.createElement('div');
      item.className = 'combo-item';
      const box2 = document.createElement('input');
      box2.type = 'checkbox';
      box2.checked = chosen.has(r.id);
      box2.style.marginRight = '8px';
      const label = document.createElement('span');
      label.textContent = r.name;
      label.style.flex = '1';
      const tag = document.createElement('span');
      tag.className = 'tag';
      tag.textContent = r.kind === 'group' ? '群聊' : '单聊';
      item.append(box2, label, tag);
      item.onclick = (event) => {
        event.preventDefault();
        if (chosen.has(r.id)) chosen.delete(r.id); else chosen.set(r.id, r);
        box2.checked = chosen.has(r.id);
        updateChosen();
      };
      box.appendChild(item);
    }
  }
  box.hidden = false;
}

async function loadRecipients(){
  try {
    const data = await api('/api/wecom/console/recipients');
    recipientChoices = data.recipients || [];
    for (const id of [...chosen.keys()]) {
      if (!recipientChoices.some(r => r.id === id)) chosen.delete(id);
    }
    updateChosen();
    if (!$('blocked-options').hidden) renderOptions($('blocked-search').value);
  } catch (error) {
    recipientChoices = [];
    $('send-msg').textContent = '读取联系人失败：' + error.message;
  }
}

$('blocked-search').addEventListener('input', () => { renderOptions($('blocked-search').value); });
$('blocked-search').addEventListener('focus', () => { renderOptions($('blocked-search').value); });
$('blocked-search').addEventListener('keydown', event => { if (event.key === 'Escape') $('blocked-options').hidden = true; });
document.addEventListener('pointerdown', event => {
  if (!$('blocked-options').hidden && !event.target.closest('.combo')) $('blocked-options').hidden = true;
});

$('blocked-add').addEventListener('click', async () => {
  const chosenRows = [...chosen.values()];
  if (!chosenRows.length) { $('send-msg').textContent = '请先搜索并选择联系人。'; return; }
  try {
    for (const r of chosenRows) {
      await api('/api/wecom/console/blocked', 'POST', {id: r.id});
    }
    $('send-msg').textContent = '已加入不可发送名单：' + chosenRows.map(r => r.name).join('、');
    chosen.clear();
    $('blocked-search').value = '';
    $('blocked-options').hidden = true;
    updateChosen();
    await sendStatus();
    await loadRecipients();
  } catch (error) { $('send-msg').textContent = '添加失败：' + error.message; }
});

$('blocked-all').addEventListener('change', () => {
  const on = $('blocked-all').checked;
  blockedChecks().forEach(c => { c.checked = on; });
  updateRemoveSelected();
});
$('blocked-list').addEventListener('change', event => {
  if (event.target.classList.contains('blocked-pick')) updateRemoveSelected();
});
$('blocked-remove-selected').addEventListener('click', async () => {
  const ids = selectedBlockedIds();
  if (!ids.length) return;
  try {
    await removeBlockedIds(ids);
    $('send-msg').textContent = '已移出 ' + ids.length + ' 个联系人。';
    await sendStatus();
    await loadRecipients();
  } catch (error) { $('send-msg').textContent = '移出失败：' + error.message; }
});

loadRecipients();
sendStatus().catch(error => { $('send-msg').textContent = '读取发送状态失败：' + error.message; });
