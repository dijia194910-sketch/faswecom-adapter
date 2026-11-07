'use strict';
// 统一外壳：窄侧边栏（图标+文字：聊天/助手/导入导出/设置）+ 右侧内容区
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

  async function api(path) {
    const response = await fetch(path, {cache: 'no-store'});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(data.detail || ('HTTP ' + response.status));
    return data;
  }

  function ensureSidebar() {
    let sidebar = document.querySelector('.sidebar');
    if (sidebar) return sidebar;
    sidebar = document.createElement('aside');
    sidebar.className = 'sidebar';
    const main = document.createElement('div');
    main.className = 'shell-main';
    const notice = document.getElementById('notice');
    const movable = Array.from(document.body.children)
      .filter(el => el.tagName !== 'SCRIPT' && el !== notice && el !== sidebar);
    movable.forEach(el => main.appendChild(el));
    document.body.insertBefore(sidebar, document.body.firstChild);
    document.body.insertBefore(main, sidebar.nextSibling);
    return sidebar;
  }

  function buildRail(sidebar) {
    // 品牌
    let brand = sidebar.querySelector('.brand');
    if (!brand) {
      brand = document.createElement('div');
      brand.className = 'brand';
    }
    if (!brand.querySelector('img')) {
      const img = document.createElement('img');
      img.src = '/ui/wecom/wecom-logo.jpg';
      img.alt = 'faswecom';
      brand.insertBefore(img, brand.firstChild);
    }
    const copy = brand.querySelector('.brand-copy');
    if (copy) {
      const h1 = document.createElement('h1');
      h1.innerHTML = 'faswecom<span>企业微信</span>';
      copy.replaceWith(h1);
    }
    if (!brand.querySelector('h1')) {
      const h1 = document.createElement('h1');
      h1.innerHTML = 'faswecom<span>企业微信</span>';
      brand.appendChild(h1);
    }

    // 账号槽位（固定高度，保证切换页面位置不变）
    let slot = sidebar.querySelector('.rail-account');
    if (!slot) {
      slot = document.createElement('div');
      slot.className = 'rail-account';
    }
    let switcher = document.querySelector('.account-switcher');
    if (switcher) slot.appendChild(switcher);

    // 导航
    const nav = document.createElement('nav');
    nav.className = 'side-nav';
    nav.innerHTML = pages.map(p =>
      '<a href="' + p.href + '"' + (p.key === active ? ' class="active" aria-current="page"' : '') +
      '><span class="dot"></span>' + p.label + '</a>').join('');

    // 底部
    const bottom = document.createElement('div');
    bottom.className = 'shell-bottom';
    const takeover = document.createElement('label');
    takeover.className = 'takeover';
    const takeoverBox = document.createElement('input');
    takeoverBox.type = 'checkbox';
    takeoverBox.id = 'rail-takeover';
    const takeoverText = document.createElement('span');
    takeoverText.textContent = '智能客服接管';
    takeover.append(takeoverBox, takeoverText);
    const takeoverState = document.createElement('p');
    takeoverState.className = 'takeover-state';
    takeoverState.id = 'rail-takeover-state';
    takeoverState.textContent = '正在读取…';
    bottom.append(takeover, takeoverState);
    const applyTakeover = enabled => {
      takeoverState.textContent = enabled ? '已接管：客户消息自动回复' : '已关闭：消息留给人工';
    };
    api('/api/wecom/console/status').then(data => {
      const enabled = data.ai_takeover !== false;
      takeoverBox.checked = enabled;
      applyTakeover(enabled);
    }).catch(() => { takeoverState.textContent = '状态读取失败'; });
    takeoverBox.addEventListener('change', async () => {
      const enabled = takeoverBox.checked;
      takeoverBox.disabled = true;
      try {
        const response = await fetch('/api/wecom/console/ai-takeover', {
          method: 'PUT',
          headers: {'Content-Type': 'application/json', 'X-ChatWeave-UI': '1'},
          body: JSON.stringify({enabled})
        });
        if (!response.ok) throw Error('切换失败');
        applyTakeover(enabled);
      } catch (error) {
        takeoverBox.checked = !enabled;
        takeoverState.textContent = '切换失败：' + error.message;
      } finally {
        takeoverBox.disabled = false;
      }
    });

    const send = document.createElement('a');
    send.href = '/console?send=1';
    send.textContent = '发送权限设置';
    send.addEventListener('click', event => {
      const button = document.getElementById('open-send');
      if (button) { event.preventDefault(); button.click(); }
    });
    bottom.appendChild(send);
    const modelButton = document.getElementById('open-model');
    if (modelButton) {
      const model = document.createElement('a');
      model.href = '/console';
      model.textContent = '模型设置';
      model.addEventListener('click', event => { event.preventDefault(); modelButton.click(); });
      bottom.appendChild(model);
    }
    const meta = document.createElement('p');
    meta.className = 'meta';
    meta.textContent = '本地运行 · 仅本机访问';
    bottom.appendChild(meta);

    // 统一顺序：品牌 → 账号 → 导航 → 底部
    sidebar.append(brand, slot, nav, bottom);
    sidebar.dataset.shellMounted = '1';

    // 侧边栏与内容之间的拖动条
    let splitter = sidebar.querySelector('.rail-splitter');
    if (!splitter) {
      splitter = document.createElement('div');
      splitter.className = 'rail-splitter';
      splitter.setAttribute('role', 'separator');
      splitter.setAttribute('aria-orientation', 'vertical');
      splitter.tabIndex = 0;
      splitter.title = '拖动调整侧边栏宽度';
      sidebar.appendChild(splitter);
    }
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
      if (!dragging) return;
      apply(event.clientX);
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
      const current = document.querySelector('.sidebar').getBoundingClientRect().width;
      const next = apply(current + (event.key === 'ArrowRight' ? 16 : -16));
      try { localStorage.setItem('railW', String(next)); } catch (e) {}
    });
  }

  function setupChatSplitter() {
    const splitter = document.getElementById('chat-splitter');
    const list = document.querySelector('.list-panel');
    if (!splitter || !list) return;
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
    if (!splitter || !list) return;
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
    // 左侧收起箭头
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
    // 点击抽屉外任意位置收起
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
    buildRail(sidebar);
    if (active === 'chat') {
      const head = document.querySelector('.list-head');
      if (head && !head.querySelector('.new-chat')) {
        const newChat = document.createElement('button');
        newChat.type = 'button';
        newChat.className = 'new-chat';
        newChat.textContent = '＋ 新对话';
        newChat.addEventListener('click', () => { location.href = '/console'; });
        head.appendChild(newChat);
      }
    }
    setupChatSplitter();
    setupConsoleSplitter();
    setupScope();
    document.documentElement.classList.remove('shell-boot');
    if (active === 'console' && new URLSearchParams(location.search).has('send')) {
      setTimeout(() => { const button = document.getElementById('open-send'); if (button) button.click(); }, 700);
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
  else mount();
})();