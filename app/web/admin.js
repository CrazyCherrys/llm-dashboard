'use strict';
const ui = window.llmDashboardUI;
const $ = (id) => document.getElementById(id);
const escapeHTML = (value) =>
  String(value ?? '').replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c],
  );
const time = (value) =>
  value
    ? new Intl.DateTimeFormat('zh-CN', {
        timeZone: 'Asia/Shanghai',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      }).format(new Date(value * 1000))
    : '等待调度';
let authenticated = false,
  cards = [],
  models = [],
  editingCard = null,
  modelCardId = null,
  categories = [],
  templates = [],
  editingTemplate = null,
  editingCategory = null,
  editingModel = null,
  copyingModel = null,
  busy = false,
  refreshPromise = null,
  setup = false,
  authStatus = null,
  sessionEpoch = 0,
  statusSequence = 0,
  sessionExpires = null,
  loginPending = false,
  loggingOut = false,
  settingsLoaded = false,
  settingsLoading = false,
  settingsLoadSequence = 0,
  passwordPending = false;
const formSnapshots = new Map();
const settingInputs = {
  worker_concurrency: 'settings-concurrency',
  run_retention_days: 'settings-run-retention',
  artifact_retention_days: 'settings-artifact-retention',
  output_retention_days: 'settings-output-retention',
};
let toastTimer;
const stages = {
  queued: '排队中',
  requesting: '请求中',
  retrying: '重试中',
  rendering: '生成缩略图',
  reviewing: '视觉审核',
  finished: '已完成',
};
function message(text, error = false) {
  $('admin-message').textContent = text;
  $('admin-message').classList.toggle('error', error);
  const toast = $('admin-toast');
  toast.textContent = text;
  toast.classList.toggle('error', error);
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(
    () => {
      toast.hidden = true;
    },
    error ? 6000 : 3500,
  );
}
function formValue(form) {
  return JSON.stringify(
    [...form.querySelectorAll('input, select, textarea')].map((input) => [
      input.id,
      input.type === 'checkbox' ? input.checked : input.value,
    ]),
  );
}
function formChanged(form) {
  return formSnapshots.has(form.id) && formSnapshots.get(form.id) !== formValue(form);
}
function openForm(kind, initialFocus) {
  const form = $(kind + '-form');
  formSnapshots.set(form.id, formValue(form));
  form.querySelector('.dialog-scroll').scrollTop = 0;
  ui.openDialog($(kind + '-dialog'), initialFocus);
}
function saving(kind, active) {
  const dialog = $(kind + '-dialog');
  dialog.classList.toggle('saving', active);
  dialog.querySelectorAll('[data-close]').forEach((button) => {
    button.disabled = active;
  });
  const button = $(kind + '-save');
  button.disabled = active;
  button.textContent = active ? '保存中…' : {
    card: '保存卡片', category: '保存分类', template: '保存模板', model: '保存模型与计划',
    settings: '保存设置', password: '修改密码并重新登录',
  }[kind];
  if (kind === 'settings') button.disabled = active || !settingsLoaded;
  dialog.querySelector('.dialog-scroll').inert = active;
}
for (const kind of ['card', 'category', 'model', 'template']) {
  const form = $(kind + '-form');
  ui.bindDialog($(kind + '-dialog'), {
    fallbackFocus: $('add-model'),
    beforeClose: async () => !busy && (!formChanged(form) || (await ui.confirmDiscard())),
    onClose: () => {
      formSnapshots.delete(form.id);
      if (kind === 'card') {
        $('card-key').value = '';
        $('card-url').value = '';
      }
    },
  });
}
for (const kind of ['settings', 'password']) {
  const form = $(kind + '-form');
  ui.bindDialog($(kind + '-dialog'), {
    fallbackFocus: kind === 'settings' ? $('manage-settings') : $('change-password'),
    beforeClose: async () => {
      const epoch = sessionEpoch;
      if (busy) return false;
      const discard = !formChanged(form) || await ui.confirmDiscard();
      return discard && epoch === sessionEpoch && authenticated && !busy;
    },
    onClose: () => {
      formSnapshots.delete(form.id);
      form.reset();
      $(kind + '-error').textContent = '';
      if (kind === 'settings') clearSettingsView();
    },
  });
}
window.addEventListener('beforeunload', (event) => {
  if ([...document.querySelectorAll('.form-dialog[open] form')].some(formChanged)) {
    event.preventDefault();
    event.returnValue = '';
  }
});
// The browser keeps the HttpOnly session cookie; JavaScript never stores a credential.
const sessionEvents =
  typeof BroadcastChannel === 'function' ? new BroadcastChannel('llm-dashboard-admin-session') : null;
class SessionChangedError extends Error {}
function clearPrivateView() {
  cards = [];
  models = [];
  editingCard = modelCardId = null;
  categories = [];
  templates = [];
  editingTemplate = null;
  busy = false;
  passwordPending = false;
  editingCategory = editingModel = copyingModel = null;
  formSnapshots.clear();
  document.querySelectorAll('dialog[open]').forEach((dialog) => dialog.close());
  clearSettingsView();
  for (const kind of ['card', 'category', 'model', 'template', 'settings', 'password']) {
    $(kind + '-form').reset();
    saving(kind, false);
    $(kind + '-error').textContent = '';
  }
  $('admin-token').value = '';
  $('confirm-password').value = '';
  $('card-key').value = '';
  $('card-url').value = '';
  $('saved-key').textContent = '';
  $('saved-url').textContent = '';
  $('model-list').replaceChildren();
  $('category-list').replaceChildren();
  $('template-list').replaceChildren();
  $('admin-category-filter').innerHTML = '<option value="all">全部分类</option>';
  $('model-count').textContent = '';
  $('task-list').replaceChildren();
  $('tasks-panel').hidden = true;
  $('admin-message').textContent = '';
  $('admin-toast').textContent = '';
  $('admin-toast').hidden = true;
  clearTimeout(toastTimer);
}
function configureLogin(status) {
  setup = !status.configured;
  $('auth-title').textContent = setup ? '首次设置管理密码' : '登录管理员后台';
  $('auth-description').textContent = setup
    ? '设置 12–256 字符的管理密码。'
    : '登录后 2 小时内，刷新页面或返回后台会自动恢复登录。';
  $('confirm-row').hidden = !setup;
  $('confirm-password').required = setup;
  $('admin-token').minLength = setup ? 12 : 1;
  $('admin-token').autocomplete = setup ? 'new-password' : 'current-password';
  $('auth-submit').disabled = loginPending || loggingOut || (setup && !status.setup_allowed);
  if (setup && !status.setup_allowed)
    $('auth-error').textContent = '暂时无法设置管理密码，请刷新页面后重试。';
}
function showLogin(error = '') {
  sessionEpoch++;
  statusSequence++;
  authenticated = false;
  sessionExpires = null;
  clearPrivateView();
  $('auth-loading').hidden = true;
  $('admin-content').hidden = true;
  $('login-panel').hidden = false;
  $('logout').hidden = true;
  $('manage-settings').hidden = true;
  $('add-model').hidden = true;
  $('auth-error').textContent = error;
  configureLogin(authStatus || { configured: true });
}
function showSessionLoading(error = '') {
  $('auth-loading').hidden = false;
  $('login-panel').hidden = true;
  $('session-status').textContent = error || '正在检查浏览器会话，无需重复输入密码。';
  $('retry-session').hidden = !error;
}
function showAdmin(status) {
  if (!authenticated || sessionExpires !== status.expires_at) sessionEpoch++;
  authenticated = true;
  sessionExpires = status.expires_at;
  $('auth-loading').hidden = true;
  $('login-panel').hidden = true;
  $('admin-content').hidden = false;
  $('logout').hidden = false;
  $('manage-settings').hidden = false;
  $('add-model').hidden = false;
  $('auth-error').textContent = '';
  $('admin-token').value = '';
  $('confirm-password').value = '';
}
async function api(path, body, method = 'POST') {
  const epoch = sessionEpoch;
  const protectedRequest = !path.startsWith('/api/auth/');
  if (protectedRequest && (!authenticated || loggingOut)) throw new SessionChangedError();
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : method,
    credentials: 'same-origin',
    cache: 'no-store',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    signal: AbortSignal.timeout(15000),
  });
  const result = await response.json();
  if (protectedRequest && epoch !== sessionEpoch) throw new SessionChangedError();
  if (response.status === 401 && protectedRequest) {
    showLogin('登录已过期，请重新登录。');
    throw new SessionChangedError();
  }
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}
async function checkSession() {
  if (loginPending || loggingOut || passwordPending) return;
  const sequence = ++statusSequence;
  const epoch = sessionEpoch;
  try {
    const status = await api('/api/auth/status');
    if (sequence !== statusSequence || epoch !== sessionEpoch || loginPending || loggingOut || passwordPending) return;
    authStatus = status;
    configureLogin(status);
    if (status.authenticated) {
      const restored = !authenticated;
      showAdmin(status);
      if (restored) message('登录已恢复。配置保存后自动生效。');
      if (restored || (!busy && !document.querySelector('dialog[open]'))) await refresh(true);
    } else {
      showLogin(authenticated ? '登录已过期，请重新登录。' : $('auth-error').textContent);
    }
  } catch (error) {
    if (sequence !== statusSequence || epoch !== sessionEpoch) return;
    if (authenticated) message(`会话检查失败：${error.message}，稍后重试。`, true);
    else showSessionLoading(`暂时无法恢复登录状态：${error.message}。请重新检查。`);
  }
}
async function logout() {
  if (loggingOut || !authenticated) return;
  loggingOut = true;
  const epoch = sessionEpoch;
  $('logout').disabled = true;
  $('logout').textContent = '正在退出…';
  $('add-model').disabled = true;
  $('manage-settings').disabled = true;
  $('admin-content').inert = true;
  try {
    await api('/api/auth/logout', {});
    showLogin('已退出登录。');
    sessionEvents?.postMessage({ type: 'logout' });
  } catch (error) {
    if (authenticated && epoch === sessionEpoch)
      message(`退出失败：${error.message}。当前仍保持登录，请重试。`, true);
  } finally {
    loggingOut = false;
    $('logout').disabled = false;
    $('logout').textContent = '退出登录';
    $('add-model').disabled = false;
    $('manage-settings').disabled = false;
    $('admin-content').inert = false;
    configureLogin(authStatus || { configured: true });
  }
}
function clearSettingsView() {
  settingsLoadSequence++;
  settingsLoaded = settingsLoading = false;
  Object.values(settingInputs).forEach(id => { $(id).value = ''; });
  $('settings-fields').disabled = true;
  $('settings-save').disabled = true;
  $('settings-status').textContent = '';
  $('settings-retry').hidden = true;
  $('settings-retry').disabled = false;
  $('settings-error').textContent = '';
}
async function loadSettings() {
  if (!authenticated || loggingOut || busy || settingsLoading || !$('settings-dialog').open) return;
  const epoch = sessionEpoch;
  const sequence = ++settingsLoadSequence;
  settingsLoading = true;
  settingsLoaded = false;
  $('settings-fields').disabled = true;
  $('settings-save').disabled = true;
  $('settings-retry').hidden = true;
  $('settings-status').textContent = '正在加载系统设置…';
  $('settings-error').textContent = '';
  try {
    const values = await api('/api/admin/settings');
    if (epoch !== sessionEpoch || sequence !== settingsLoadSequence || !authenticated || !$('settings-dialog').open) return;
    Object.entries(settingInputs).forEach(([key, id]) => { $(id).value = values[key]; });
    settingsLoaded = true;
    $('settings-status').textContent = '';
    formSnapshots.set('settings-form', formValue($('settings-form')));
  } catch (error) {
    if (epoch === sessionEpoch && sequence === settingsLoadSequence && authenticated && $('settings-dialog').open && !(error instanceof SessionChangedError)) {
      $('settings-status').textContent = '';
      $('settings-error').textContent = `无法加载系统设置：${error.message}`;
      $('settings-retry').hidden = false;
    }
  } finally {
    if (epoch === sessionEpoch && sequence === settingsLoadSequence) {
      settingsLoading = false;
      $('settings-fields').disabled = !settingsLoaded;
      $('settings-save').disabled = !settingsLoaded;
    }
  }
}
$('manage-settings').addEventListener('click', () => {
  if (busy || !authenticated || loggingOut || $('settings-dialog').open) return;
  clearSettingsView();
  openForm('settings', $('settings-dialog').querySelector('[data-close]'));
  loadSettings();
});
$('settings-retry').addEventListener('click', loadSettings);
$('settings-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (busy || !authenticated || loggingOut || !settingsLoaded || !$('settings-form').reportValidity()) return;
  const epoch = sessionEpoch;
  busy = true;
  saving('settings', true);
  render();
  $('settings-error').textContent = '';
  $('settings-status').textContent = '';
  try {
    const values = await api('/api/admin/settings', Object.fromEntries(
      Object.entries(settingInputs).map(([key, id]) => [key, Number($(id).value)]),
    ), 'PATCH');
    if (epoch !== sessionEpoch || !authenticated) return;
    Object.entries(settingInputs).forEach(([key, id]) => { $(id).value = values[key]; });
    formSnapshots.set('settings-form', formValue($('settings-form')));
    $('settings-status').textContent = '设置已保存。新的保留天数在下一次例行清理时生效。';
    message('系统设置已保存。');
  } catch (error) {
    if (authenticated && epoch === sessionEpoch && !(error instanceof SessionChangedError)) $('settings-error').textContent = error.message;
  } finally {
    if (epoch === sessionEpoch) {
      busy = false;
      saving('settings', false);
      render();
    }
  }
});
$('change-password').addEventListener('click', () => {
  if (busy || !authenticated || loggingOut) return;
  $('password-form').reset();
  $('password-error').textContent = '';
  openForm('password', $('current-password'));
});
$('password-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (busy || !authenticated || loggingOut || !$('password-form').reportValidity()) return;
  if ($('new-password').value !== $('new-password-confirm').value) {
    $('password-error').textContent = '两次新密码不一致';
    return;
  }
  const epoch = sessionEpoch;
  busy = passwordPending = true;
  statusSequence++;
  saving('password', true);
  render();
  $('password-error').textContent = '';
  try {
    await api('/api/admin/password', {
      current_password: $('current-password').value,
      new_password: $('new-password').value,
    });
    if (epoch !== sessionEpoch || !authenticated) return;
    authStatus = { configured: true, setup_allowed: false, authenticated: false, expires_at: null };
    showLogin('管理密码已修改，请使用新密码重新登录。');
    sessionEvents?.postMessage({ type: 'logout', reason: 'password-changed' });
  } catch (error) {
    if (authenticated && epoch === sessionEpoch && !(error instanceof SessionChangedError)) $('password-error').textContent = error.message;
  } finally {
    if (epoch === sessionEpoch) {
      passwordPending = false;
      busy = false;
      saving('password', false);
      render();
    }
  }
});
function categoryName(id) {
  return categories.find((c) => c.id === id)?.name || '未分类';
}
function runButton(model, kind) {
  const task = model.tasks.find((item) => item.kind === kind);
  const name = kind === 'candy' ? '逻辑' : '图形';
  const reason = !model.has_key ? '请先填写 API Key' : task ? `${name}检测${task.status === 'queued' ? '已排队' : '进行中'}，完成后可再次检测` : `立即执行一次${name}检测`;
  const text = task ? `${name}${task.status === 'queued' ? '已排队' : '检测中'}` : `测${name}`;
  return `<button class="button secondary compact" data-model-run="${model.id}" data-kind="${kind}" title="${escapeHTML(reason)}" aria-label="${escapeHTML(reason)}" ${!model.has_key || task ? 'disabled' : ''}>${text}</button>`;
}
function render() {
  if (!authenticated) return;
  const focus = ui.captureFocus($('model-list'));
  const filter = $('admin-category-filter');
  const selected = filter.value;
  const options = document.createElement('select');
  options.innerHTML = `<option value="all">全部分类</option><option value="">未分类</option>${categories.map(c => `<option value="${c.id}">${escapeHTML(c.name)}</option>`).join('')}`;
  ui.patchChildren(filter, options);
  filter.value = selected === 'all' || selected === '' || categories.some(c => String(c.id) === selected) ? selected : 'all';
  const visible = cards.filter(c => filter.value === 'all' || String(c.category_id ?? '') === filter.value);
  $('model-count').textContent = `共 ${cards.length} 张卡片 / ${models.length} 个模型 · 当前 ${visible.length} 张卡片`;
  const fresh = document.createElement('div');
  fresh.innerHTML = visible.length ? visible.map(c => `<section class="admin-panel" data-card="${c.id}">
    <div class="admin-channel-heading"><div><h2>${escapeHTML(c.title)} <span class="badge ${c.enabled ? 'passed' : ''}">${c.enabled ? '总开关已开启' : '整卡已暂停'}</span></h2>
    <div class="admin-channel-meta"><span>${escapeHTML(categoryName(c.category_id))}</span><span>${c.models.length} 个模型 · ${c.public ? '公开展示' : '仅管理员可见'} · 排序 ${c.sort_order}</span></div></div>
    <div class="action-buttons"><button class="button primary compact" data-card-add-model="${c.id}">＋ 添加模型</button><button class="button secondary compact" data-card-edit="${c.id}">卡片设置</button><button class="button secondary compact" data-card-toggle="${c.id}">${c.enabled ? '暂停整卡' : '恢复整卡'}</button><button class="button danger compact" data-card-archive="${c.id}">归档卡片</button></div></div>
    <div class="admin-channel-meta card-connection"><span>共享 API ${escapeHTML(c.base_url_masked)}</span><span>密钥 ${escapeHTML(c.api_key_masked)}</span></div>
    ${c.models.length ? c.models.map(m => `<section class="admin-model child-model" data-model="${m.id}"><div class="admin-channel-heading"><div><h3>${escapeHTML(m.display_name)} <span class="badge ${m.effective_enabled ? 'passed' : ''}">${!m.enabled ? '模型已暂停' : !c.enabled ? '等待卡片恢复' : '自动检测已启用'}</span></h3><p>${escapeHTML(m.model)} · ${m.protocol === 'chat' ? 'Chat Completions' : 'Responses'} / ${escapeHTML(m.effort)} · 排序 ${m.position}</p></div><div class="action-buttons"><button class="button secondary compact" data-model-edit="${m.id}">编辑模型</button><button class="button secondary compact" data-model-copy="${m.id}">复制模型</button><button class="button secondary compact" data-model-toggle="${m.id}">${m.enabled ? '暂停' : '启用'}</button>${['candy','pelican'].map(kind=>runButton(m,kind)).join('')}<button class="button danger compact" data-model-archive="${m.id}">归档模型</button></div></div>
    <div class="schedule-chips">${m.schedules.map(sc => `<span>${sc.kind === 'candy' ? '逻辑' : '图形'}：${escapeHTML(templateById(sc.template_id)?.name || '未选择模板')} · ${sc.enabled ? `${sc.interval_minutes} 分钟 / 次` : '已关闭'}${sc.enabled && m.effective_enabled ? ` · 下次 ${escapeHTML(time(sc.next_run))}` : ''}</span>`).join('')}${m.tasks.map(t => `<span>${t.kind === 'candy' ? '逻辑' : '图形'} ${escapeHTML(stages[t.stage] || t.status)}</span>`).join('')}</div></section>`).join('') : '<p class="muted-box">尚无模型。点击“添加模型”后，这张卡片才会出现在大盘。</p>'}
    </section>`).join('') : `<section class="admin-panel admin-empty">${cards.length ? '这个分类尚无卡片，可在卡片设置中调整分类。' : '尚无卡片。点击“新增卡片”填写共用接口，再添加模型。'}</section>`;
  ui.patchChildren($('model-list'), fresh);
  if (busy) $('model-list').querySelectorAll('button').forEach(b => b.disabled = true);
  $('add-model').disabled = busy || loggingOut;
  $('manage-categories').disabled = busy || loggingOut;
  $('manage-templates').disabled = busy || loggingOut;
  $('manage-settings').disabled = busy || loggingOut;
  ui.restoreFocus(focus);
}
async function refresh(force = false) {
  if (!authenticated || loggingOut) return;
  if (refreshPromise) {
    await refreshPromise;
    if (force && authenticated && !loggingOut) return refresh();
    return;
  }
  const epoch = sessionEpoch;
  const pending = (async () => {
    try {
      const [next, tasks] = await Promise.all([
        api('/api/admin/cards'),
        api('/api/admin/tasks'),
      ]);
      if (!authenticated || loggingOut || epoch !== sessionEpoch) return;
      cards = next.cards;
      models = cards.flatMap(c => c.models);
      categories = next.categories;
      templates = next.templates || [];
      render();
      $('tasks-panel').hidden = !tasks.length;
      $('task-list').innerHTML = tasks
        .map(
          (task) =>
            `<span>#${task.id} ${escapeHTML(task.title)} / ${escapeHTML(task.display_name)} · ${task.kind === 'candy' ? '逻辑' : '图形'} · ${escapeHTML(stages[task.stage])}</span>`,
        )
        .join('');
    } catch (error) {
      if (authenticated && epoch === sessionEpoch && !(error instanceof SessionChangedError))
        message(error.message, true);
    } finally {
      if (refreshPromise === pending) refreshPromise = null;
    }
  })();
  refreshPromise = pending;
  return pending;
}
async function action(fn) {
  if (busy || !authenticated || loggingOut) return;
  const epoch = sessionEpoch;
  busy = true;
  render();
  try {
    await fn();
    await refresh(true);
  } catch (error) {
    if (authenticated && epoch === sessionEpoch && !(error instanceof SessionChangedError))
      message(error.message, true);
  } finally {
    if (epoch === sessionEpoch) {
      busy = false;
      render();
    }
  }
}
function modelById(id) {
  return models.find(m => m.id === id);
}
function editModel(id = null, copy = false, cardId = null) {
  const m = modelById(id);
  modelCardId = m?.card_id ?? cardId;
  const card = cards.find(c => c.id === modelCardId);
  if (!card) return;
  $('model-card-context').textContent = `所属卡片：${card.title} · 共用卡片接口与密钥${card.enabled ? '' : ' · 整卡已暂停，开启总开关后才会自动检测'}`;
  editingModel = copy ? null : id;
  copyingModel = copy ? id : null;
  $('model-title').textContent = copy ? '复制模型配置' : m ? '编辑模型与检测计划' : '新增模型';
  for (const [field, value] of Object.entries({
    'model-name': m?.model || '',
    'model-display': m ? (copy ? `${m.display_name.slice(0, 115)} · 副本` : m.display_name) : '',
    'model-protocol': m?.protocol || 'responses',
    'model-effort': m?.effort || 'medium',
    'model-retries': m?.retry_count ?? 2,
    'model-timeout': m?.timeout_seconds ?? 300,
    'model-first-timeout': m?.first_token_timeout ?? 120,
    'model-idle-timeout': m?.idle_timeout ?? 60,
    'model-max-tokens': m?.max_output_tokens ?? 16000,
    'model-order': m?.position ?? 0,
  })) $(field).value = value;
  $('model-enabled').checked = copy ? false : (m?.enabled ?? false);
  for (const kind of ['candy', 'pelican']) {
    const schedule = m?.schedules.find(s => s.kind === kind);
    $(kind + '-enabled').checked = schedule?.enabled ?? kind === 'candy';
    $(kind + '-interval').value = schedule?.interval_minutes ?? (kind === 'candy' ? 10 : 45);
  }
  populateTemplateSelects(m);
  $('model-error').textContent = '';
  $('model-form').querySelector('.advanced-settings').open = false;
  updateScheduleInputs();
  openForm('model', $('model-name'));
}
function editCard(id = null) {
  const c = cards.find(c => c.id === id);
  editingCard = id;
  $('card-title').textContent = c ? '编辑卡片与共享连接' : '新增卡片';
  $('card-name').value = c?.title || '';
  $('card-order').value = c?.sort_order ?? 0;
  $('card-enabled').checked = c?.enabled ?? true;
  $('card-public').checked = c?.public ?? true;
  $('card-category').innerHTML = `<option value="">未分类</option>${categories.map(c => `<option value="${c.id}">${escapeHTML(c.name)}</option>`).join('')}<option value="new">＋ 新建分类…</option>`;
  $('card-category').value = c ? c.category_id ?? '' : ($('admin-category-filter').value === 'all' ? '' : $('admin-category-filter').value);
  $('card-category-name').value = '';
  updateCategoryInput();
  $('card-url').value = $('card-key').value = '';
  $('card-url').required = !c;
  $('card-key').required = !c || !c.has_key;
  $('card-url').placeholder = c ? '留空保留当前地址' : 'https://example.com/v1';
  $('card-key').placeholder = c ? '留空保留已保存密钥' : '填写完整 API Key';
  $('saved-url').textContent = c ? `已保存：${c.base_url_masked}。更换地址需重新填写 Key，作用于卡片内所有模型。` : '';
  $('saved-key').textContent = c ? `已保存：${c.api_key_masked}。卡片内模型共用。` : '';
  $('card-error').textContent = '';
  openForm('card', $('card-name'));
}
$('card-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (busy || !authenticated || loggingOut || !$('card-form').reportValidity()) return;
  const epoch = sessionEpoch;
  const creating = editingCard === null;
  busy = true; saving('card', true);
  try {
    const result = await api(creating ? '/api/admin/cards' : `/api/admin/cards/${editingCard}`, {
      title: $('card-name').value, base_url: $('card-url').value, api_key: $('card-key').value,
      sort_order: Number($('card-order').value), enabled: $('card-enabled').checked, public: $('card-public').checked,
      ...($('card-category').value === 'new' ? {category_name: $('card-category-name').value} : {category_id: $('card-category').value ? Number($('card-category').value) : null}),
    }, creating ? 'POST' : 'PATCH');
    $('card-dialog').close();
    await refresh(true);
    if (epoch === sessionEpoch && authenticated) {
      message(creating ? '卡片已创建，请添加第一个模型。' : '卡片已保存，共享设置对内部所有模型生效。');
      if (creating) editModel(null, false, result.id);
    }
  } catch (error) {
    if (authenticated && epoch === sessionEpoch && !(error instanceof SessionChangedError)) $('card-error').textContent = error.message;
  } finally {
    if (epoch === sessionEpoch) {busy = false; saving('card', false); render();}
  }
});
function updateCategoryInput() {
  const isNew = $('card-category').value === 'new';
  $('new-category-row').hidden = !isNew;
  $('card-category-name').required = isNew;
  $('card-category-name').disabled = !isNew;
}
$('card-category').addEventListener('change', () => {
  updateCategoryInput();
  if ($('card-category').value === 'new') $('card-category-name').focus();
});
function renderCategories() {
  $('category-list').innerHTML = categories.length ? categories.map(c => `<div class="category-row"><span><strong>${escapeHTML(c.name)}</strong><small>${c.card_count} 张卡片 · 排序 ${c.sort_order}</small></span><div class="action-buttons"><button type="button" class="button secondary compact" data-category-edit="${c.id}">编辑</button><button type="button" class="button danger compact" data-category-delete="${c.id}">删除</button></div></div>`).join('') : '<p class="muted-box">尚无分类，在下方填写名称即可创建。</p>';
}
async function editCategory(id = null) {
  const epoch = sessionEpoch;
  if ($('category-dialog').open && formChanged($('category-form')) && !(await ui.confirmDiscard())) return;
  if (epoch !== sessionEpoch || !authenticated || busy || loggingOut) return;
  editingCategory = id;
  const c = categories.find(c => c.id === id);
  $('category-name').value = c?.name || '';
  $('category-order').value = c?.sort_order ?? 0;
  $('category-editor-title').textContent = c ? '编辑分类' : '新增分类';
  $('category-error').textContent = '';
  renderCategories();
  openForm('category', $('category-name'));
}
$('manage-categories').addEventListener('click', () => editCategory());
$('category-new').addEventListener('click', () => editCategory());
$('admin-category-filter').addEventListener('change', render);
const templateRuleLabels = { exact: '完整答案匹配', number: '独立数字匹配', contains: '包含指定文本', none: '仅记录，不评分', visual: '视觉审核' };
const templateRuleHints = {
  exact: '比较完整回答，忽略首尾空白、连续空白和英文字母大小写。建议在题目中要求只输出答案。',
  number: '回答中出现这个独立数字即通过，不判断完整推理；例如 21 不匹配 121 或 21.5。',
  contains: '回答包含指定文本即通过，区分大小写；不判断完整语义。',
  none: '只保存模型回答，不给出对错，也不计入通过率。',
};
function templateById(id) { return templates.find(t => t.id === Number(id)); }
function updateTemplatePreview(kind) {
  const t = templateById($(kind + '-template').value);
  $(kind + '-template-rule').textContent = t ? `${templateRuleLabels[t.rule_type]}${t.expected_answer ? ' · 预期答案：' + t.expected_answer : ''}${t.review_criteria ? ' · 审核标准：' + t.review_criteria : ''}` : '先在“提示词模板”中创建对应的模板。';
  $(kind + '-template-preview').textContent = t?.body || '';
}
function populateTemplateSelects(model) {
  for (const kind of ['candy', 'pelican']) {
    const choices = templates.filter(t => t.kind === kind);
    const selected = model?.schedules.find(s => s.kind === kind)?.template_id;
    $(kind + '-template').innerHTML = `<option value="">请选择模板</option>${choices.map(t => `<option value="${t.id}">${escapeHTML(t.name)} · v${t.revision}</option>`).join('')}`;
    $(kind + '-template').value = model ? selected ?? '' : choices[0]?.id ?? '';
    $(kind + '-template').closest('fieldset').querySelector('.template-preview').open = false;
    updateTemplatePreview(kind);
  }
}
for (const kind of ['candy', 'pelican']) $(kind + '-template').addEventListener('change', () => updateTemplatePreview(kind));
function updateTemplateFields() {
  const graphic = $('template-kind').value === 'pelican';
  $('template-logic-fields').hidden = graphic;
  $('template-graphic-fields').hidden = !graphic;
  $('template-criteria').disabled = !graphic;
  $('template-criteria').required = graphic;
  $('template-rule').disabled = graphic;
  $('template-answer').disabled = graphic || $('template-rule').value === 'none';
  $('template-answer').required = !graphic && $('template-rule').value !== 'none';
  $('template-answer-row').hidden = $('template-rule').value === 'none';
  $('template-rule-hint').textContent = templateRuleHints[$('template-rule').value];
}
$('template-kind').addEventListener('change', updateTemplateFields);
$('template-rule').addEventListener('change', updateTemplateFields);
function renderTemplates() {
  $('template-list').innerHTML = templates.map(t => `<div class="category-row"><span><strong>${escapeHTML(t.name)}</strong><small>${t.kind === 'candy' ? '逻辑题' : '图形测试'} · v${t.revision} · ${t.model_count} 个模型使用</small></span><div class="action-buttons"><button type="button" class="button secondary compact" data-template-edit="${t.id}">编辑</button><button type="button" class="button secondary compact" data-template-copy="${t.id}">复制</button><button type="button" class="button danger compact" data-template-archive="${t.id}" ${t.model_count ? 'disabled title="先在模型设置中更换此模板，才能归档"' : ''}>归档</button></div></div>`).join('') || '<p class="muted-box">尚无模板，在下方创建后即可为模型选择。</p>';
}
async function editTemplate(id = null, copy = false) {
  const epoch = sessionEpoch;
  if ($('template-dialog').open && formChanged($('template-form')) && !(await ui.confirmDiscard())) return;
  if (epoch !== sessionEpoch || !authenticated || busy || loggingOut) return;
  const t = templateById(id);
  editingTemplate = copy ? null : id;
  $('template-editor-title').textContent = copy ? '复制模板' : t ? '编辑模板' : '新增模板';
  $('template-name').value = t ? (copy ? `${t.name.slice(0, 95)} · 副本` : t.name) : '';
  $('template-kind').value = t?.kind || 'candy';
  $('template-kind').disabled = !!editingTemplate;
  $('template-body').value = t?.body || '';
  $('template-rule').value = t && t.kind === 'candy' ? t.rule_type : 'exact';
  $('template-answer').value = t?.expected_answer || '';
  $('template-criteria').value = t?.review_criteria || '';
  $('template-animation').checked = t?.require_animation ?? true;
  $('template-error').textContent = '';
  updateTemplateFields();
  renderTemplates();
  openForm('template', $('template-name'));
}
$('manage-templates').addEventListener('click', () => editTemplate());
$('template-new').addEventListener('click', () => editTemplate());
$('template-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (busy || !authenticated || loggingOut || !$('template-form').reportValidity()) return;
  const epoch = sessionEpoch;
  busy = true;
  saving('template', true);
  try {
    const graphic = $('template-kind').value === 'pelican';
    await api(editingTemplate ? `/api/admin/templates/${editingTemplate}` : '/api/admin/templates', {
      name: $('template-name').value, kind: $('template-kind').value, body: $('template-body').value,
      rule_type: graphic ? 'visual' : $('template-rule').value,
      expected_answer: graphic ? '' : $('template-answer').value,
      review_criteria: graphic ? $('template-criteria').value : '',
      require_animation: graphic && $('template-animation').checked,
    }, editingTemplate ? 'PATCH' : 'POST');
    $('template-dialog').close();
    await refresh(true);
    if (epoch === sessionEpoch) message('模板已保存，后续检测使用新版本。');
  } catch (error) {
    if (authenticated && epoch === sessionEpoch && !(error instanceof SessionChangedError)) $('template-error').textContent = error.message;
  } finally {
    if (epoch === sessionEpoch) { busy = false; saving('template', false); render(); }
  }
});

function updateScheduleInputs() {
  for (const kind of ['candy', 'pelican']) {
    $(kind + '-interval').disabled = !$(kind + '-enabled').checked;
    $(kind + '-template').required = $(kind + '-enabled').checked;
  }
}
for (const kind of ['candy', 'pelican'])
  $(kind + '-enabled').addEventListener('change', updateScheduleInputs);
$('model-form').addEventListener(
  'invalid',
  (event) => {
    const details = event.target.closest('details');
    if (details) details.open = true;
  },
  true,
);
$('auth-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (loginPending || loggingOut) return;
  const password = $('admin-token').value;
  if (setup && password !== $('confirm-password').value) {
    $('auth-error').textContent = '两次密码不一致';
    return;
  }
  const epoch = sessionEpoch;
  loginPending = true;
  statusSequence++;
  $('auth-submit').disabled = true;
  try {
    if (setup) {
      await api('/api/auth/setup', { password });
      setup = false;
    }
    // The response token remains available for API clients; the page ignores it.
    await api('/api/auth/login', { password });
    if (epoch !== sessionEpoch) {
      // A logout in another tab must win over a login response arriving late.
      await api('/api/auth/logout', {});
      sessionEvents?.postMessage({ type: 'logout' });
      return;
    }
    $('auth-error').textContent = '';
    loginPending = false;
    await checkSession();
    if (authenticated) {
      message('已登录。配置保存后自动生效。');
      sessionEvents?.postMessage({ type: 'login' });
    }
  } catch (error) {
    if (epoch === sessionEpoch) $('auth-error').textContent = error.message;
  } finally {
    loginPending = false;
    $('admin-token').value = '';
    $('confirm-password').value = '';
    configureLogin(authStatus || { configured: !setup, setup_allowed: true });
  }
});
$('logout').addEventListener('click', logout);
$('retry-session').addEventListener('click', () => {
  showSessionLoading();
  checkSession();
});
$('add-model').addEventListener('click', () => { if (!busy && authenticated && !loggingOut) editCard(); });
document.addEventListener('click', (event) => {
  const b = event.target.closest('button');
  if (!b) return;
  if (b.dataset.close) return ui.requestClose($(b.dataset.close));
  if (busy || !authenticated || loggingOut) return;
  if (b.dataset.cardEdit) return editCard(Number(b.dataset.cardEdit));
  if (b.dataset.cardAddModel) return editModel(null, false, Number(b.dataset.cardAddModel));
  if (b.dataset.cardToggle) {
    const c = cards.find(c => c.id === Number(b.dataset.cardToggle));
    return action(async () => {
      await api(`/api/admin/cards/${c.id}`, {enabled: !c.enabled}, 'PATCH');
      message(c.enabled ? '整张卡片已暂停，各模型自身的启停选择保留。' : '卡片已恢复，已启用的模型继续自动检测。');
    });
  }
  if (b.dataset.cardArchive && confirm('归档整张卡片？其全部模型将停止新任务并从大盘隐藏，历史记录保留。')) return action(async () => {
    await api(`/api/admin/cards/${b.dataset.cardArchive}`, {archived: true}, 'PATCH');
    message('卡片已归档，历史记录保留。');
  });
  if (b.dataset.templateEdit) return editTemplate(Number(b.dataset.templateEdit));
  if (b.dataset.templateCopy) return editTemplate(Number(b.dataset.templateCopy), true);
  if (b.dataset.templateArchive && confirm('归档这个模板？已有检测记录会保留。')) return action(async () => {
    await api(`/api/admin/templates/${b.dataset.templateArchive}`, {}, 'DELETE');
    await refresh(true);
    if (editingTemplate === Number(b.dataset.templateArchive)) {
      editingTemplate = null;
      $('template-form').reset();
      $('template-kind').disabled = false;
      $('template-editor-title').textContent = '新增模板';
      updateTemplateFields();
      formSnapshots.set('template-form', formValue($('template-form')));
    }
    renderTemplates();
    message('模板已归档，历史记录保留。');
  });
  if (b.dataset.modelEdit) return editModel(Number(b.dataset.modelEdit));
  if (b.dataset.modelCopy) return editModel(Number(b.dataset.modelCopy), true);
  if (b.dataset.categoryEdit) return editCategory(Number(b.dataset.categoryEdit));
  if (b.dataset.categoryDelete && confirm('删除这个分类？其中的卡片将移到“未分类”，配置和历史保留。'))
    return action(async () => {
      await api(`/api/admin/categories/${b.dataset.categoryDelete}`, {}, 'DELETE');
      await refresh(true);
      if (editingCategory === Number(b.dataset.categoryDelete)) {
        editingCategory = null;
        $('category-form').reset();
        $('category-editor-title').textContent = '新增分类';
        formSnapshots.set('category-form', formValue($('category-form')));
      }
      renderCategories();
      message('分类已删除，卡片配置和历史保留。');
    });
  if (b.dataset.modelToggle) {
    const m = modelById(Number(b.dataset.modelToggle));
    return action(async () => {
      await api(`/api/admin/models/${m.id}`, { enabled: !m.enabled }, 'PATCH');
      message(m.enabled ? '模型已暂停。' : '模型已启用。');
    });
  }
  if (b.dataset.modelRun)
    return action(async () => {
      const result = await api(`/api/admin/models/${b.dataset.modelRun}/run`, {
        kinds: [b.dataset.kind],
      });
      message(`检测已排队：#${result.ids.join('、#')}。`);
    });
  if (b.dataset.modelArchive && confirm('归档这个模型？将停止新任务并隐藏，历史数据保留。'))
    return action(async () => {
      await api(
        `/api/admin/models/${b.dataset.modelArchive}`,
        { archived: true, enabled: false },
        'PATCH',
      );
      message('模型已归档，历史保留。');
    });
});
$('category-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy || !authenticated || loggingOut || !$('category-form').reportValidity()) return;
  const epoch = sessionEpoch;
  busy = true;
  saving('category', true);
  try {
    await api(editingCategory ? `/api/admin/categories/${editingCategory}` : '/api/admin/categories',
      { name: $('category-name').value, sort_order: Number($('category-order').value) }, editingCategory ? 'PATCH' : 'POST');
    $('category-dialog').close();
    await refresh(true);
    if (epoch === sessionEpoch) message('分类已保存。');
  } catch (error) {
    if (authenticated && epoch === sessionEpoch && !(error instanceof SessionChangedError)) $('category-error').textContent = error.message;
  } finally {
    if (epoch === sessionEpoch) { busy = false; saving('category', false); render(); }
  }
});
$('model-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy || !authenticated || loggingOut || !$('model-form').reportValidity()) return;
  const epoch = sessionEpoch;
  busy = true;
  saving('model', true);
  try {
    const values = {
      position: Number($('model-order').value),
      ...(copyingModel ? { copy_from: copyingModel } : {}),
      model: $('model-name').value,
      display_name: $('model-display').value,
      protocol: $('model-protocol').value,
      effort: $('model-effort').value,
      retry_count: Number($('model-retries').value),
      timeout_seconds: Number($('model-timeout').value),
      first_token_timeout: Number($('model-first-timeout').value),
      idle_timeout: Number($('model-idle-timeout').value),
      max_output_tokens: Number($('model-max-tokens').value),
      enabled: $('model-enabled').checked,
      schedules: Object.fromEntries(
        ['candy', 'pelican'].map((kind) => [
          kind,
          {
            enabled: $(kind + '-enabled').checked,
            interval_minutes: Number($(kind + '-interval').value),
            template_id: $(kind + '-template').value ? Number($(kind + '-template').value) : null,
          },
        ]),
      ),
    };
    await api(
      editingModel
        ? `/api/admin/models/${editingModel}`
        : `/api/admin/cards/${modelCardId}/models`,
      values,
      editingModel ? 'PATCH' : 'POST',
    );
    $('model-dialog').close();
    await refresh(true);
    if (epoch === sessionEpoch) message('模型与独立检测计划已保存。');
  } catch (error) {
    if (authenticated && epoch === sessionEpoch && !(error instanceof SessionChangedError))
      $('model-error').textContent = error.message;
  } finally {
    if (epoch === sessionEpoch) {
      busy = false;
      saving('model', false);
      render();
    }
  }
});
setInterval(() => {
  if (!document.hidden && !busy && !document.querySelector('dialog[open]')) refresh();
}, 5000);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) checkSession();
});
window.addEventListener('pageshow', (event) => {
  if (!event.persisted) return;
  sessionEpoch++;
  authenticated = false;
  clearPrivateView();
  $('admin-content').hidden = true;
  $('logout').hidden = true;
  $('manage-settings').hidden = true;
  $('add-model').hidden = true;
  showSessionLoading();
  checkSession();
});
if (sessionEvents)
  sessionEvents.onmessage = (event) => {
    if (event.data?.type === 'logout') showLogin(event.data.reason === 'password-changed'
      ? '管理密码已修改，请使用新密码重新登录。'
      : '已在另一个标签页退出登录。');
    else if (event.data?.type === 'login') checkSession();
  };
setInterval(() => {
  if (!document.hidden && authenticated) checkSession();
}, 30000);
checkSession();
