'use strict';
// 统一外壳：侧边栏在 HTML 里预渲染（首屏即完整），这里只做交互与兜底
(() => {
  const pages = [
    { key: 'chat', label: '聊天', href: '/chat' },
    { key: 'console', label: '助手', href: '/console' },
    { key: 'data', label: '导入/导出', href: '/' },
    { key: 'settings', label: '设置', href: '/settings' }
  ];
  const path = location.pathname.replace(/^\/w\/(legacy|[a-f0-9]{64})/, '') || '/';
  const active = path.startsWith('/chat') ? 'chat'
    : path.startsWith('/console') ? 'console'
    : path.startsWith('/settings') ? 'settings' : 'data';

  async function api(url) {
    const response = await fetch(url, {cache: 'no-store'});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(data.detail || ('HTTP ' + response.status));
    return data;
  }

  function ensureSidebar() {
    let sidebar = document.querySelector('.sidebar');
    if (sidebar) return sidebar;
    sidebar = document.createElement('aside');
    sidebar.className = 'sidebar';
    sidebar.innerHTML = '<div class="brand"><img src="/ui/wecom/wecom-logo.jpg" alt="faswecom"><h1>faswecom<span>企业微信</span></h1></div>'
      + '<div class="rail-account"></div>'
      + '<nav class="side-nav"></nav>'
      + '<div class="shell-bottom"></div>';
    document.body.insertBefore(sidebar, document.body.firstChild);
    return sidebar;
  }

  function buildMissing(sidebar) {
    let brand = sidebar.querySelector('.brand');
    if (!brand) {
      brand = document.createElement('div');
      brand.className = 'brand';
      brand.innerHTML = '<img src="/ui/wecom/wecom-logo.jpg" alt="faswecom"><h1>faswecom<span>企业微信</span></h1>';
      sidebar.insertBefore(brand, sidebar.firstChild);
    }
    let slot = sidebar.querySelector('.rail-account');
    if (!slot) {
      slot = document.createElement('div');
      slot.className = 'rail-account';
      brand.after(slot);
    }
    let nav = sidebar.querySelector('.side-nav');
    if (!nav) {
      nav = document.createElement('nav');
      nav.className = 'side-nav';
      slot.after(nav);
    }
    if (!nav.children.length) {
      nav.innerHTML = pages.map(p =>
        '<a href="' + p.href + '"' + (p.key === active ? ' class="active" aria-current="page"' : '') +
        '><span class="dot"></span>' + p.label + '</a>').join('');
    }
    let bottom = sidebar.querySelector('.shell-bottom');
    if (!bottom) {
      bottom = document.createElement('div');
      bottom.className = 'shell-bottom';
      sidebar.appendChild(bottom);
    }
    if (!bottom.querySelector('.takeover')) {
      const label = document.createElement('label');
      label.className = 'takeover';
      label.innerHTML = '<input type="checkbox" id="rail-takeover"><span>智能客服接管</span>';
      const state = document.createElement('p');
      state.className = 'takeover-state';
      state.id = 'rail-takeover-state';
      state.textContent = '正在读取…';
      const send = document.createElement('a');
      send.href = '/console?send=1';
      send.className = 'shell-send';
      send.textContent = '发送权限设置';
      const meta = document.createElement('p');
      meta.className = 'meta';
      meta.textContent = '本地运行 · 仅本机访问';
      bottom.append(label, state, send, meta);
    }
    if (!sidebar.querySelector('.rail-splitter')) {
      const splitter = document.createElement('div');
      splitter.className = 'rail-splitter';
      splitter.setAttribute('role', 'separator');
      splitter.setAttribute('aria-orientation', 'vertical');
      splitter.tabIndex = 0;
      splitter.title = '拖动调整侧边栏宽度';
      sidebar.appendChild(splitter);
    }
  }

  function wireAccount(sidebar) {
    let slot = sidebar.querySelector('.rail-account');
    if (!slot) return;
    const switcher = document.querySelector('.account-switcher');
    if (switcher && switcher.parentElement !== slot) slot.appendChild(switcher);
  }

  function wireTakeover(sidebar) {
    const box = sidebar.querySelector('#rail-takeover');
    const state = sidebar.querySelector('#rail-takeover-state');
    if (!box || !state) return;
    const apply = (enabled) => { state.textContent = enabled ? '已接管：客户消息自动回复' : '已关闭：消息留给人工'; };
    api('/api/wecom/console/status').then(data => {
      const enabled = data.ai_takeover !== false;
      box.checked = enabled;
      apply(enabled);
    }).catch(() => { state.textContent = '状态读取失败'; });
    box.addEventListener('change', async () => {
      const enabled = box.checked;
      box.disabled = true;
      try {
        const response = await fetch('/api/wecom/console/ai-takeover', {
          method: 'PUT',
          headers: {'Content-Type': 'application/json', 'X-ChatWeave-UI': '1'},
          body: JSON.stringify({enabled})
        });
        if (!response.ok) throw Error('切换失败');
        apply(enabled);
      } catch (error) {
        box.checked = !enabled;
        state.textContent = '切换失败：' + error.message;
      } finally {
        box.disabled = false;
      }
    });
  }

  function wireBottom(sidebar) {
    const send = sidebar.querySelector('.shell-send');
    if (send) {
      send.addEventListener('click', event => {
        const button = document.getElementById('open-send');
        if (button) { event.preventDefault(); button.click(); }
      });
    }
    
  }

  function wireRailSplitter(sidebar) {
    const splitter = sidebar.querySelector('.rail-splitter');
    if (!splitter || splitter.dataset.wired) return;
    splitter.dataset.wired = '1';
    try {
      const saved = parseInt(localStorage.getItem('railW') || '', 10);
      if (saved >= 150 && saved <= 280) document.body.style.setProperty('--rail-w', saved + 'px');
    } catch (e) {}
    const apply = width => {
      const clamped = Math.max(150, Math.min(280, Math.round(width)));
      document.body.style.setProperty('--rail-w', clamped + 'px');
      return clamped;
    };
    let dragging = false;
    splitter.addEventListener('pointerdown', event => {
      dragging = true;
      splitter.setPointerCapture(event.pointerId);
      splitter.classList.add('dragging');
      document.body.style.cursor = 'col-resize';
      event.preventDefault();
    });
    splitter.addEventListener('pointermove', event => {
      if (dragging) apply(event.clientX);
    });
    splitter.addEventListener('pointerup', () => {
      if (!dragging) return;
      dragging = false;
      splitter.classList.remove('dragging');
      document.body.style.cursor = '';
      try { localStorage.setItem('railW', document.body.style.getPropertyValue('--rail-w').replace('px', '')); } catch (e) {}
    });
    splitter.addEventListener('keydown', event => {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      const current = sidebar.getBoundingClientRect().width;
      const next = apply(current + (event.key === 'ArrowRight' ? 16 : -16));
      try { localStorage.setItem('railW', String(next)); } catch (e) {}
    });
  }

  function setupChatSplitter() {
    const splitter = document.getElementById('chat-splitter');
    const list = document.querySelector('.list-panel');
    if (!splitter || !list || splitter.dataset.wired) return;
    splitter.dataset.wired = '1';
    try {
      const saved = parseInt(localStorage.getItem('chatListW') || '', 10);
      if (saved >= 220 && saved <= 420) document.body.style.setProperty('--chat-list-w', saved + 'px');
    } catch (e) {}
    const apply = width => {
      const clamped = Math.max(220, Math.min(420, Math.round(width)));
      document.body.style.setProperty('--chat-list-w', clamped + 'px');
      return clamped;
    };
    let dragging = false;
    splitter.addEventListener('pointerdown', event => {
      dragging = true;
      splitter.setPointerCapture(event.pointerId);
      splitter.classList.add('dragging');
      document.body.style.cursor = 'col-resize';
      event.preventDefault();
    });
    splitter.addEventListener('pointermove', event => {
      if (!dragging) return;
      const rail = document.querySelector('.sidebar');
      apply(event.clientX - (rail ? rail.getBoundingClientRect().width : 0));
    });
    splitter.addEventListener('pointerup', () => {
      if (!dragging) return;
      dragging = false;
      splitter.classList.remove('dragging');
      document.body.style.cursor = '';
      try { localStorage.setItem('chatListW', document.body.style.getPropertyValue('--chat-list-w').replace('px', '')); } catch (e) {}
    });
    splitter.addEventListener('keydown', event => {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      const current = list.getBoundingClientRect().width;
      const next = apply(current + (event.key === 'ArrowRight' ? 24 : -24));
      try { localStorage.setItem('chatListW', String(next)); } catch (e) {}
    });
  }

  function setupConsoleSplitter() {
    const splitter = document.getElementById('list-splitter');
    const list = document.querySelector('.list-panel');
    if (!splitter || !list || splitter.dataset.wired) return;
    splitter.dataset.wired = '1';
    try {
      const saved = parseInt(localStorage.getItem('consoleListW') || '', 10);
      if (saved >= 200 && saved <= 560) document.body.style.setProperty('--console-list-w', saved + 'px');
    } catch (e) {}
    const apply = width => {
      const clamped = Math.max(200, Math.min(560, Math.round(width)));
      document.body.style.setProperty('--console-list-w', clamped + 'px');
      return clamped;
    };
    let dragging = false;
    splitter.addEventListener('pointerdown', event => {
      dragging = true;
      splitter.setPointerCapture(event.pointerId);
      splitter.classList.add('dragging');
      document.body.style.cursor = 'col-resize';
      event.preventDefault();
    });
    splitter.addEventListener('pointermove', event => {
      if (!dragging) return;
      const rail = document.querySelector('.sidebar');
      apply(event.clientX - (rail ? rail.getBoundingClientRect().width : 0));
    });
    splitter.addEventListener('pointerup', () => {
      if (!dragging) return;
      dragging = false;
      splitter.classList.remove('dragging');
      document.body.style.cursor = '';
      try { localStorage.setItem('consoleListW', document.body.style.getPropertyValue('--console-list-w').replace('px', '')); } catch (e) {}
    });
    splitter.addEventListener('keydown', event => {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      const current = list.getBoundingClientRect().width;
      const next = apply(current + (event.key === 'ArrowRight' ? 24 : -24));
      try { localStorage.setItem('consoleListW', String(next)); } catch (e) {}
    });
  }

  function setupScope() {
    if (active !== 'console' || !document.getElementById('scope')) return;
    document.body.classList.add('scope-hidden');
    const scope = document.getElementById('scope');
    if (!scope.querySelector('.scope-collapse')) {
      const collapse = document.createElement('button');
      collapse.type = 'button';
      collapse.className = 'scope-collapse';
      collapse.title = '收起搜索范围';
      collapse.setAttribute('aria-label', '收起搜索范围');
      collapse.textContent = '＞';
      collapse.addEventListener('click', () => document.body.classList.add('scope-hidden'));
      scope.appendChild(collapse);
    }
    document.addEventListener('pointerdown', event => {
      if (document.body.classList.contains('scope-hidden')) return;
      if (scope.contains(event.target)) return;
      if (event.target.closest && event.target.closest('#toggle-scope')) return;
      document.body.classList.add('scope-hidden');
    }, true);
  }

  function mount() {
    document.body.classList.add(active === 'console' ? 'shell-console' : active === 'chat' ? 'shell-chat' : 'shell-page');
    const sidebar = ensureSidebar();
    buildMissing(sidebar);
    wireAccount(sidebar);
    wireTakeover(sidebar);
    wireBottom(sidebar);
    wireRailSplitter(sidebar);
    setupChatSplitter();
    setupConsoleSplitter();
    setupScope();
    document.documentElement.classList.remove('shell-boot');
    if (active === 'console' && new URLSearchParams(location.search).has('send')) {
      setTimeout(() => { const button = document.getElementById('open-send'); if (button) button.click(); }, 700);
    }
    if (active === 'console' && new URLSearchParams(location.search).has('model')) {
      setTimeout(() => { const button = document.getElementById('open-model'); if (button) button.click(); }, 700);
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
  else mount();
})();