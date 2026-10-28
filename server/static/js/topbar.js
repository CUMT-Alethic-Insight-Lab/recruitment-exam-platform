/* 招新站统一顶栏逻辑（随 _topbar.html 注入各页）：
   1. 主题：localStorage 'theme'（与官网同键），无则跟系统；
   2. 旧键迁移：aletnic_*（历史拼写）→ alethic_*，一次性搬运；
   3. 登录态：读统一键 → GET /api/auth/me → 渲染姓名/入口/退出。 */
(function () {
  'use strict';

  var bar = document.querySelector('.topbar');
  if (!bar) return;
  var PREFIX = bar.dataset.prefix || '';
  var ADMIN_PATH = bar.dataset.adminPath || (PREFIX + '/admin');
  var root = document.documentElement;

  /* ---------- 主题 ---------- */
  function currentTheme() {
    return root.dataset.theme === 'dark' ? 'dark' : 'light';
  }
  function paintThemeBtn() {
    // 主站 track/thumb 结构：只改文案与 aria 态，不整颗替换按钮内容（会抹掉 track/thumb）
    var btn = document.getElementById('topbar-theme-btn');
    if (!btn) return;
    var dark = currentTheme() === 'dark';
    var label = document.getElementById('topbar-theme-label');
    if (label) label.textContent = dark ? '浅色' : '深色';
    btn.setAttribute('aria-pressed', String(!dark));
  }
  try {
    var saved = localStorage.getItem('theme');
    if (saved === 'dark' || saved === 'light') {
      root.dataset.theme = saved;
    } else if (window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches) {
      root.dataset.theme = 'dark';
    }
  } catch (e) { /* 无痕/禁用存储时保持默认浅色 */ }
  paintThemeBtn();
  var themeBtn = document.getElementById('topbar-theme-btn');
  if (themeBtn) {
    themeBtn.addEventListener('click', function () {
      var next = currentTheme() === 'dark' ? 'light' : 'dark';
      root.dataset.theme = next;
      try { localStorage.setItem('theme', next); } catch (e) {}
      paintThemeBtn();
    });
  }

  /* ---------- 登录键统一 + 旧键迁移 ---------- */
  var KEYS = ['alethic_session', 'alethic_student_id', 'alethic_cid', 'alethic_admin_token', 'alethic_admin_username'];
  var LEGACY = {
    aletnic_student_id: 'alethic_student_id',
    aletnic_cid: 'alethic_cid',
    aletnic_admin_token: 'alethic_admin_token',
    aletnic_admin_username: 'alethic_admin_username'
  };
  function store(k) {
    try { return localStorage.getItem(k) || ''; } catch (e) { return ''; }
  }
  try {
    Object.keys(LEGACY).forEach(function (oldKey) {
      var v = localStorage.getItem(oldKey);
      if (v && !localStorage.getItem(LEGACY[oldKey])) {
        localStorage.setItem(LEGACY[oldKey], v);
      }
      if (v) localStorage.removeItem(oldKey);
    });
  } catch (e) {}

  function clearSession() {
    try {
      KEYS.forEach(function (k) { localStorage.removeItem(k); });
    } catch (e) {}
  }

  /* ---------- 登录态渲染 ---------- */
  var authBox = document.getElementById('topbar-auth');
  if (!authBox) return;

  function renderGuest() {
    authBox.innerHTML = '';
    var login = document.createElement('a');
    login.className = 'topbar-btn primary';
    login.href = '/login';
    login.textContent = '登录';
    authBox.appendChild(login);
    // 有开放届次才显示报名入口
    fetch(PREFIX + '/api/campaigns/latest-open').then(function (res) {
      if (!res.ok) return;
      return res.json();
    }).then(function (data) {
      if (!data || !data.id) return;
      var apply = document.createElement('a');
      apply.className = 'topbar-btn';
      apply.href = PREFIX + '/apply/' + encodeURIComponent(data.id);
      apply.textContent = '报名';
      authBox.appendChild(apply);
    }).catch(function () {});
  }

  function renderUser(me, token) {
    authBox.innerHTML = '';
    var name = document.createElement('span');
    name.className = 'topbar-user';
    var display = me.name || me.student_id || '';
    name.textContent = me.role === 'admin' ? '管理员' : ('欢迎，' + display);
    authBox.appendChild(name);
    var center = document.createElement('a');
    center.className = 'topbar-btn';
    center.href = '/account';
    center.textContent = '个人中心';
    authBox.appendChild(center);
    if (me.role === 'admin') {
      var admin = document.createElement('a');
      admin.className = 'topbar-btn';
      admin.href = ADMIN_PATH;
      admin.textContent = '管理端';
      authBox.appendChild(admin);
    }
    var out = document.createElement('button');
    out.className = 'topbar-btn';
    out.type = 'button';
    out.textContent = '退出';
    out.addEventListener('click', function () {
      fetch(PREFIX + '/api/auth/logout', {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + token }
      }).catch(function () {}).finally(function () {
        clearSession();
        location.href = '/login';
      });
    });
    authBox.appendChild(out);
  }

  var adminToken = store('alethic_admin_token');
  var sessionToken = store('alethic_session');
  var token = adminToken || sessionToken;
  if (!token) {
    renderGuest();
    return;
  }
  fetch(PREFIX + '/api/auth/me', {
    headers: { Authorization: 'Bearer ' + token }
  }).then(function (res) {
    if (!res.ok) throw new Error('invalid session');
    return res.json();
  }).then(function (me) {
    renderUser(me, token);
  }).catch(function () {
    clearSession();
    renderGuest();
  });
})();
