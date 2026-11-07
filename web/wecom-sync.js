'use strict';
(() => {
  const label = document.getElementById('sync-state');
  const toggle = document.getElementById('sync-toggle');
  let enabled = true, busy = false, changing = false, revision;
  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      const response = await fetch('/api/wecom/sync', {cache: 'no-store'});
      if (!response.ok) throw Error('同步服务暂时不可用');
      const value = await response.json();
      enabled = value.enabled;
      let text = value.message;
      if (value.last_sync_at) text += ' · 最近同步 ' + new Date(value.last_sync_at * 1000).toLocaleTimeString();
      if (value.error) text += ' · ' + value.error;
      label.textContent = text;
      toggle.textContent = enabled ? '暂停自动同步' : '开启自动同步';
      toggle.disabled = changing;
      const next = value.data.revision + ':' + value.data.messages + ':' + (value.data.names_revision||0);
      if (revision !== next) {
        document.dispatchEvent(new CustomEvent('wecom-data-changed', {detail: value.data}));
        revision = next;
      }
    } catch (error) {
      label.textContent = error.message + '，正在重新连接…';
    } finally {
      busy = false;
    }
  }
  toggle.onclick = async () => {
    changing = true;
    toggle.disabled = true;
    try {
      const response = await fetch('/api/wecom/sync', {method: 'PUT',
        headers: {'Content-Type': 'application/json', 'X-ChatWeave-UI': '1'},
        body: JSON.stringify({enabled: !enabled})});
      if (!response.ok) {
        const result = await response.json();
        throw Error(result.detail || '同步设置保存失败');
      }
      await refresh();
    } catch (error) {
      label.textContent = error.message;
    } finally {
      changing = false;
      toggle.disabled = false;
    }
  };
  async function poll() {
    await refresh();
    window.setTimeout(poll, document.hidden ? 15000 : 3000);
  }
  poll();
})();
