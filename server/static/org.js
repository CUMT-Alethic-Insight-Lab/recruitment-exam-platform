(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const rawPrefix = document.body.dataset.prefix || '';
  const prefix = rawPrefix.includes('__PREFIX__')
    ? (location.pathname.startsWith('/recruitment/') ? '/recruitment' : '')
    : (/^(\/[A-Za-z0-9_/-]+)?$/.test(rawPrefix) ? rawPrefix.replace(/\/$/, '') : '');
  const apiRoot = prefix + '/api/v1';
  $('login-link').href = prefix + '/login';
  let identity = null;
  let schema = null;
  let projects = [];
  let draft = null;
  let validated = false;
  let editVersion = 0;
  let pendingSubmission = null;
  let codeTimer = null;
  const controls = new Map();
  const pages = new Map();
  const labels = { project_id: '项目', motivation: '加入动机', weekly_hours: '每周可投入小时数', notes: '补充说明' };
  const statuses = { draft: '草稿 · 未提交', submitted: '已正式提交 · 待审批', in_review: '审批中', approved: '已批准', rejected: '已拒绝', needs_information: '需要补充信息', withdrawn: '已撤回' };

  function text(tag, value, className) {
    const el = document.createElement(tag);
    el.textContent = value == null ? '' : String(value);
    if (className) el.className = className;
    return el;
  }
  function status(id, value, error) {
    $(id).textContent = value;
    $(id).classList.toggle('error', Boolean(error));
  }
  function bearer() {
    try { return localStorage.getItem('alethic_admin_token') || localStorage.getItem('alethic_session') || localStorage.getItem('aletnic_admin_token') || ''; }
    catch (_) { return ''; }
  }
  async function api(path, options) {
    const token = bearer();
    if (!token) throw new Error('请先使用原网页登录，再返回成员事务页。');
    const opts = options || {};
    let response;
    try {
      response = await fetch(apiRoot + path, {
        method: opts.method || 'GET', credentials: 'omit', cache: 'no-store', redirect: 'error',
        headers: Object.assign({ Authorization: 'Bearer ' + token }, opts.body ? { 'Content-Type': 'application/json' } : {}, opts.headers || {}),
        body: opts.body ? JSON.stringify(opts.body) : undefined
      });
    } catch (_) { throw new Error('网络请求失败。请检查连接；正式提交请保留本页并用同一幂等键重试。'); }
    let data = {};
    try { if (response.status !== 204) data = await response.json(); } catch (_) { /* Non-JSON gateway failures must not be rendered as markup. */ }
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) {
        $('admin').hidden = true;
        invalidateDraft();
      }
      const message = response.status === 401 ? '会话已失效，请重新登录。' : response.status === 403 ? '当前账号、角色或授权不允许此操作。' :
        (data.error && data.error.message) || (typeof data.detail === 'string' ? data.detail : '请求未完成，请检查字段、状态或稍后重试。');
      throw new Error('HTTP ' + response.status + ' · ' + message);
    }
    return data;
  }
  function sourceLink(parent, value) {
    if (typeof value !== 'string') return;
    let url;
    try { url = new URL(value); } catch (_) { return; }
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return;
    const link = text('a', '查看权威来源');
    link.href = url.href;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    parent.appendChild(link);
  }
  function action(parent, label, handler) {
    const button = text('button', label);
    button.type = 'button';
    button.addEventListener('click', async () => {
      button.disabled = true;
      try { await handler(); } catch (e) { status('global-status', e.message, true); }
      finally { button.disabled = false; }
    });
    parent.appendChild(button);
    return button;
  }
  async function run(id, handler) {
    try { await handler(); } catch (e) { status(id, e.message, true); }
  }
  function wireForm(id, statusId, handler) {
    $(id).addEventListener('submit', async (event) => {
      event.preventDefault();
      const button = $(id).querySelector('button[type=submit]');
      if (button.disabled) return;
      button.disabled = true;
      try { await handler(); } catch (e) { status(statusId, e.message, true); }
      finally { button.disabled = false; syncSubmit(); }
    });
  }
  async function page(key, path, containerId, moreId, render, reset) {
    const previous = pages.get(key);
    if (!reset && previous && previous.busy) return;
    const state = reset || !previous ? { cursor: null, busy: false } : previous;
    pages.set(key, state);
    state.busy = true;
    $(moreId).disabled = true;
    try {
      const query = new URLSearchParams(path.includes('?') ? path.slice(path.indexOf('?') + 1) : '');
      query.set('limit', '50');
      if (state.cursor) query.set('cursor', state.cursor);
      const result = await api(path.split('?')[0] + '?' + query);
      if (pages.get(key) !== state) return;
      if (!Array.isArray(result.items)) throw new Error('列表响应格式不正确。');
      if (reset) $(containerId).replaceChildren();
      if (!result.items.length && !state.cursor) $(containerId).appendChild(text('p', '暂无记录。', 'muted'));
      result.items.forEach((item) => render(item, $(containerId)));
      state.cursor = result.next_cursor || null;
      $(moreId).hidden = !state.cursor;
    } finally { state.busy = false; $(moreId).disabled = false; }
  }
  function updateProjectSelects() {
    ['field-project_id', 'config-project'].forEach((id) => {
      const select = $(id);
      if (!select) return;
      const value = select.value;
      select.replaceChildren();
      const blank = text('option', '请选择项目');
      blank.value = '';
      select.appendChild(blank);
      projects.forEach((project) => {
        if (id === 'field-project_id' && !project.recruiting) return;
        const option = text('option', project.title || project.id);
        option.value = project.id;
        select.appendChild(option);
      });
      select.value = value;
    });
    updateConfig();
  }
  function renderProject(item, parent) {
    projects.push(item);
    const card = text('article', '', 'item');
    card.append(text('h3', item.title), text('p', item.description), text('p', (item.recruiting ? '招募开放' : '暂不招募') + ' · ' + (item.lifecycle || item.lifecycle_status || '未配置')));
    sourceLink(card, item.source_url || item.repo_url);
    parent.appendChild(card);
  }
  async function loadProjects(reset) {
    if (reset) projects = [];
    await page('projects', '/projects', 'project-list', 'projects-more', renderProject, reset);
    updateProjectSelects();
  }
  function invalidateDraft() {
    editVersion += 1;
    validated = false;
    pendingSubmission = null;
    $('confirm-check').checked = false;
    $('confirm-check').disabled = true;
    syncSubmit();
  }
  function syncSubmit() {
    $('submit-application').disabled = !validated || !$('confirm-check').checked;
  }
  function fieldsPayload() {
    const fields = {};
    controls.forEach((entry, name) => {
      const value = entry.input.value;
      fields[name] = entry.definition.type === 'integer' && value !== '' ? Number(value) : value;
    });
    return { kind: 'project-join', form_revision: schema.revision, fields: fields };
  }
  function missingFields() {
    $('missing-fields').replaceChildren();
    let invalid = false;
    controls.forEach((entry, name) => {
      if (!entry.input.checkValidity()) {
        invalid = true;
        $('missing-fields').appendChild(text('li', (labels[name] || name) + '：' + entry.input.validationMessage));
      }
    });
    return invalid;
  }
  function renderSchema(data) {
    if (!Array.isArray(data.fields) || !data.revision) throw new Error('表单 schema 不可用，无法安全创建申请。');
    schema = data;
    const fieldset = $('draft-fields');
    fieldset.replaceChildren(text('legend', '申请字段'));
    controls.clear();
    data.fields.forEach((definition) => {
      const name = definition.name;
      if (typeof name !== 'string' || !/^[a-z][a-z0-9_]*$/.test(name)) throw new Error('不支持的表单字段。');
      const label = text('label', (labels[name] || name) + (definition.required ? ' *' : '（可选）'));
      const input = document.createElement(name === 'project_id' ? 'select' : definition.type === 'integer' ? 'input' : 'textarea');
      input.id = 'field-' + name;
      input.name = name;
      input.required = Boolean(definition.required);
      if (definition.type === 'integer') { input.type = 'number'; input.step = '1'; }
      if (definition.min_length != null) input.minLength = definition.min_length;
      if (definition.max_length != null) input.maxLength = definition.max_length;
      if (definition.minimum != null) input.min = definition.minimum;
      if (definition.maximum != null) input.max = definition.maximum;
      label.htmlFor = input.id;
      fieldset.append(label, input);
      controls.set(name, { input: input, definition: definition });
      input.addEventListener('input', () => {
        invalidateDraft();
        status('draft-status', '内容已修改；当前预览失效，请重新保存草稿。');
        $('draft-preview').textContent = '预览失效，请重新保存并校验。';
      });
    });
    $('schema-meta').textContent = '表单版本：' + data.revision + ' · 来源：' + (data.source_ref || '未提供') + ' · 更新：' + (data.updated_at || '未提供');
    fieldset.disabled = false;
    $('save-draft').disabled = false;
    updateProjectSelects();
  }
  wireForm('draft-form', 'draft-status', async () => {
    invalidateDraft();
    if (!schema || missingFields()) throw new Error('请先补全列出的字段；草稿尚未保存。');
    const version = editVersion;
    const payload = fieldsPayload();
    const validation = await api('/applications/validate', { method: 'POST', body: payload });
    if (version !== editVersion) throw new Error('校验时内容已更改，请重新保存。');
    if (validation.valid !== true) {
      (validation.missing_fields || validation.errors || []).forEach((item) => $('missing-fields').appendChild(text('li', typeof item === 'string' ? item : JSON.stringify(item))));
      throw new Error('校验未通过，尚未保存草稿。');
    }
    const saved = await api('/applications/drafts', { method: 'POST', body: Object.assign({}, payload, draft ? { draft_id: draft.id } : {}) });
    draft = saved;
    if (version !== editVersion) throw new Error('保存时字段已改变；服务端草稿已保存，但请重新校验当前内容。');
    if (!saved.id || !saved.preview) throw new Error('草稿响应缺少标识或规范化预览，禁止提交。');
    $('draft-preview').textContent = JSON.stringify(saved.preview, null, 2);
    validated = true;
    $('confirm-check').disabled = false;
    status('draft-status', '草稿 ' + saved.id + ' 已保存并校验，尚未正式提交。');
    await loadApplications(true);
  });
  $('confirm-check').addEventListener('change', syncSubmit);
  wireForm('submit-form', 'draft-status', async () => {
    if (!validated || !draft || !$('confirm-check').checked) throw new Error('请先校验草稿并明确勾选确认。');
    const version = editVersion;
    $('draft-fields').disabled = true;
    $('save-draft').disabled = true;
    try {
      if (!pendingSubmission) {
        const proof = await api('/applications/' + encodeURIComponent(draft.id) + '/confirmations', { method: 'POST', body: {} });
        if (version !== editVersion || !$('confirm-check').checked) throw new Error('确认已改变，请重新核对。');
        if (JSON.stringify(proof.preview) !== JSON.stringify(draft.preview)) {
          invalidateDraft();
          throw new Error('服务端草稿内容已变化，必须重新保存并核对预览。');
        }
        if (typeof proof.confirmation !== 'string') throw new Error('服务器未签发确认凭据。');
        pendingSubmission = { body: { draft_id: draft.id, confirmation: proof.confirmation }, key: newKey() };
      }
      const result = await api('/applications', { method: 'POST', body: pendingSubmission.body, headers: { 'Idempotency-Key': pendingSubmission.key } });
      invalidateDraft();
      draft = null;
      status('draft-status', '申请 ' + result.id + ' 已正式提交。当前状态：' + (statuses[result.status] || result.status) + '。');
      $('draft-preview').textContent = '此草稿已正式提交。再次申请请重新填写并保存新草稿。';
      await loadApplications(true);
    } finally { $('draft-fields').disabled = false; $('save-draft').disabled = false; }
  });
  function newKey() {
    if (!globalThis.crypto || !crypto.getRandomValues) throw new Error('浏览器缺少安全随机数能力，无法创建幂等键。');
    return Array.from(crypto.getRandomValues(new Uint8Array(24)), (b) => b.toString(16).padStart(2, '0')).join('');
  }
  async function loadDraft(requestId) {
    const result = await api('/applications/' + encodeURIComponent(requestId));
    if (!['draft', 'needs_information'].includes(result.status)) throw new Error('该申请当前不可编辑。');
    if (!schema || !result.preview || result.form_revision !== schema.revision) throw new Error('表单版本不一致，请重新创建申请。');
    const projectId = result.preview.fields.project_id;
    const projectControl = controls.get('project_id');
    if (projectControl && !Array.from(projectControl.input.options || []).some((option) => option.value === projectId)) {
      const project = await api('/projects/' + encodeURIComponent(projectId));
      const option = text('option', project.title || project.id);
      option.value = project.id;
      projectControl.input.appendChild(option);
      if (!projects.some((item) => item.id === project.id)) projects.push(project);
    }
    invalidateDraft();
    draft = result;
    controls.forEach((entry, name) => { entry.input.value = result.preview.fields[name] == null ? '' : result.preview.fields[name]; });
    $('draft-preview').textContent = JSON.stringify(result.preview, null, 2);
    status('draft-status', '已载入 ' + result.id + '，请核对并重新保存、校验后确认。');
    $('join').scrollIntoView({ behavior: 'smooth' });
  }
  function renderApplication(item, parent, review) {
    const card = text('article', '', 'item');
    card.append(text('h3', item.id), text('p', statuses[item.status] || item.status), text('p', '项目：' + item.project_id), text('p', '更新时间：' + (item.updated_at || '未提供'), 'muted'));
    action(card, '查看内容和审批记录', async () => {
      const result = await api('/applications/' + encodeURIComponent(item.id));
      let details = card.querySelector('pre');
      if (!details) { details = text('pre', ''); card.appendChild(details); }
      details.textContent = JSON.stringify({ preview: result.preview, reviews: result.reviews || [] }, null, 2);
    });
    if (!review && ['draft', 'needs_information'].includes(item.status)) action(card, '载入草稿继续填写', () => loadDraft(item.id));
    if (review) {
      const reason = document.createElement('textarea');
      const reasonLabel = text('label', '审批说明');
      reason.id = 'review-' + item.id;
      reasonLabel.htmlFor = reason.id;
      reason.maxLength = 2000;
      card.append(reasonLabel, reason);
      const consent = document.createElement('input');
      consent.type = 'checkbox';
      const consentLabel = text('label', '', 'check');
      consentLabel.append(consent, text('span', '我已核对申请内容及审批说明，确认执行所选审批决定。'));
      card.appendChild(consentLabel);
      [['approved', '批准'], ['rejected', '拒绝'], ['needs_information', '要求补充信息']].forEach(([decision, label]) => action(card, label, async () => {
        if (!consent.checked || !reason.value.trim()) throw new Error('审批前请填写说明并明确勾选确认。');
        await api('/applications/' + encodeURIComponent(item.id) + '/review', { method: 'POST', body: { decision: decision, reason: reason.value.trim() } });
        status('admin-status', '审批决定已保存。');
        await loadReviews(true);
      }));
    }
    parent.appendChild(card);
  }
  function loadApplications(reset) { return page('applications', '/applications?mine=true', 'application-list', 'applications-more', (item, parent) => renderApplication(item, parent, false), reset); }
  function loadReviews(reset) {
    return page('reviews', '/applications?assigned=true', 'review-list', 'reviews-more',
      (item, parent) => renderApplication(item, parent, true), reset);
  }
  function updateConfig() {
    const item = projects.find((project) => project.id === $('config-project').value);
    $('config-recruiting').checked = Boolean(item && item.recruiting);
  }
  $('config-project').addEventListener('change', updateConfig);
  wireForm('project-config', 'admin-status', async () => {
    if (!identity || identity.role !== 'admin') throw new Error('需要当前网页管理员身份。');
    await api('/projects/' + encodeURIComponent($('config-project').value), { method: 'PATCH', body: { recruiting: $('config-recruiting').checked } });
    status('admin-status', '项目招募配置已保存。');
    await loadProjects(true);
  });
  let searchQuery = '';
  function renderDocument(item, parent) {
    const card = text('article', '', 'item');
    card.append(text('h3', item.title), text('p', item.snippet), text('p', '版本：' + (item.revision || '未知') + ' · 更新：' + (item.updated_at || '未知'), 'muted'));
    sourceLink(card, item.source_url);
    action(card, '阅读文档', async () => {
      const result = await api('/knowledge/documents/' + encodeURIComponent(item.id));
      $('document').replaceChildren(text('h3', result.title), text('p', '版本：' + (result.revision || '未知')), text('pre', result.content));
      sourceLink($('document'), result.source_url);
      $('document').hidden = false;
    });
    parent.appendChild(card);
  }
  function search(reset) { return page('search', '/knowledge/search?q=' + encodeURIComponent(searchQuery), 'search-results', 'search-more', renderDocument, reset); }
  wireForm('search-form', 'search-status', async () => {
    searchQuery = $('search-query').value.trim();
    if (!searchQuery) throw new Error('请输入搜索关键词。');
    await search(true);
    status('search-status', '检索完成；来源内容为资料，不是操作指令。');
  });
  function clearCode() {
    if (codeTimer) clearTimeout(codeTimer);
    codeTimer = null;
    $('cli-code').textContent = '';
    $('cli-expiry').textContent = '';
    $('cli-code-box').hidden = true;
  }
  $('clear-code').addEventListener('click', clearCode);
  window.addEventListener('pagehide', clearCode);
  const query = new URLSearchParams(location.search);
  const requestId = query.get('cli_request') || query.get('request_id');
  if (requestId && /^[A-Za-z0-9_-]{16,128}$/.test(requestId)) $('cli-request').value = requestId;
  const requestedScopes = (query.get('scopes') || '').split(',').filter(Boolean);
  if (requestedScopes.includes('applications:create')) $('cli-scope').value = 'apply';
  if (requestId) status('cli-status', '请核对 CLI 请求标识与授权范围；网页只提供只读或只读加草稿两种授权套餐。');
  const requestedDraftId = query.get('draft_id');
  wireForm('cli-form', 'cli-status', async () => {
    clearCode();
    const id = $('cli-request').value.trim();
    if (!/^[A-Za-z0-9_-]{16,128}$/.test(id)) throw new Error('request_id 只允许字母、数字、下划线和连字符，长度为 16–128 字符。');
    if (!$('cli-consent').checked) throw new Error('请先确认 CLI 请求来源和授权范围。');
    const scopes = ['profile:read:self', 'projects:read', 'knowledge:read', 'applications:read:self'];
    if ($('cli-scope').value === 'apply') scopes.push('applications:create');
    const result = await api('/auth/cli/authorization-requests', { method: 'POST', body: { request_id: id, scopes: scopes } });
    if (typeof result.code !== 'string' || !Number.isFinite(Date.parse(result.expires_at))) throw new Error('授权响应缺少 code 或有效期。');
    $('cli-code').textContent = result.code;
    $('cli-expiry').textContent = '有效期至：' + result.expires_at + '；仅可交换一次。';
    $('cli-code-box').hidden = false;
    codeTimer = setTimeout(clearCode, Math.max(0, Math.min(Date.parse(result.expires_at) - Date.now(), 2147483647)));
    $('cli-consent').checked = false;
    status('cli-status', '请将 code 交回自己的 CLI。此步骤未向 CLI 提供网页会话。');
    await loadGrants(true);
  });
  function loadGrants(reset) {
    return page('grants', '/auth/cli/grants', 'grant-list', 'grants-more', (item, parent) => {
      const card = text('article', '', 'item');
      card.append(text('h3', item.id), text('p', (item.scopes || []).join('、')), text('p', '到期：' + item.expires_at), text('p', item.revoked_at ? '已撤销' : '未撤销（是否有效以服务器为准）'));
      if (!item.revoked_at) action(card, '撤销此授权', async () => {
        await api('/auth/cli/grants/' + encodeURIComponent(item.id), { method: 'DELETE' });
        status('cli-status', '授权已撤销。');
        await loadGrants(true);
      });
      parent.appendChild(card);
    }, reset);
  }
  [['projects-more', () => loadProjects(false)], ['applications-more', () => loadApplications(false)], ['applications-refresh', () => loadApplications(true)], ['reviews-more', () => loadReviews(false)], ['reviews-refresh', () => loadReviews(true)], ['search-more', () => search(false)], ['grants-more', () => loadGrants(false)], ['grants-refresh', () => loadGrants(true)]].forEach(([id, handler]) => $(id).addEventListener('click', () => run('global-status', handler)));
  async function initialize() {
    clearCode();
    invalidateDraft();
    $('admin').hidden = true;
    $('draft-fields').disabled = true;
    $('save-draft').disabled = true;
    $('draft-preview').textContent = '尚无已校验草稿';
    ['identity', 'capabilities', 'project-list', 'application-list', 'review-list', 'grant-list', 'search-results', 'document'].forEach((id) => $(id).replaceChildren());
    pages.clear();
    schema = null;
    draft = null;
    identity = null;
    try {
      const me = await api('/me');
      if (!['member', 'admin'].includes(me.role)) throw new Error('仅当前 member / admin 账号可访问成员事务。');
      identity = me;
      $('identity').replaceChildren(text('p', '身份：' + (me.display_name || me.nickname || me.principal_id || '成员') + ' · ' + me.role), text('p', '主体标识：' + (me.principal_id || '未提供'), 'muted'));
      $('admin').hidden = me.role !== 'admin';
      status('global-status', '已验证当前成员身份。各区块独立加载，失败可重试。');
    } catch (e) { status('global-status', e.message, true); return; }
    await Promise.all([
      run('global-status', async () => {
        const data = await api('/capabilities');
        $('capabilities').replaceChildren();
        const entries = data.scopes || data.capabilities || data.items || [];
        (Array.isArray(entries) ? entries : Object.keys(entries).filter((key) => entries[key])).forEach((item) => $('capabilities').appendChild(text('li', typeof item === 'string' ? item : item.name || item.id || JSON.stringify(item))));
      }),
      run('global-status', () => loadProjects(true)),
      run('draft-status', async () => renderSchema(await api('/forms/project-join/schema'))),
      run('global-status', () => loadApplications(true)),
      run('cli-status', () => loadGrants(true)),
      identity.role === 'admin' ? run('admin-status', () => loadReviews(true)) : Promise.resolve()
    ]);
    if (requestedDraftId) await run('draft-status', () => loadDraft(requestedDraftId));
  }
  $('refresh').addEventListener('click', () => run('global-status', initialize));
  initialize();
})();
