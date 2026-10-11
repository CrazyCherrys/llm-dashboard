'use strict';
const ui = window.llmDashboardUI;
const $ = (id) => document.getElementById(id);
const escapeHTML = (value) =>
  String(value ?? '').replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c],
  );
const percent = (value) => (value == null ? '—' : `${Number(value).toFixed(2)}%`);
const seconds = (value) => (value == null ? '—' : `${Number(value).toFixed(2)} s`);
const speedClass = (value) => value == null ? 'muted' : value < 10 ? 'green' : value < 30 ? 'yellow' : value < 50 ? 'orange' : 'red';
const time = (value, full = false) =>
  value == null
    ? '—'
    : new Intl.DateTimeFormat('zh-CN', {
        timeZone: 'Asia/Shanghai',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        ...(full ? { second: '2-digit' } : {}),
        hour12: false,
      }).format(new Date(value * 1000));
const clock = (value) =>
  value == null
    ? '—'
    : new Intl.DateTimeFormat('zh-CN', {
        timeZone: 'Asia/Shanghai',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      }).format(new Date(value * 1000));
const labels = {
  passed: '通过',
  invalid: '未通过',
  error: '检测失败',
  interrupted: '已中断',
  cancelled: '已取消',
  running: '检测中',
  queued: '排队中',
  legacy: '历史记录',
  ungraded: '未评分',
};
const stageLabels = {
  queued: '排队中',
  requesting: '请求中',
  retrying: '重试中',
  rendering: '生成预览',
  reviewing: '视觉审核',
  finished: '已完成',
};
function resultLabel(run) {
  if (run.kind === 'pelican') {
    if (run.review?.status === 'invalid') return '图形已生成 · 审核未通过';
    if (run.review?.status === 'uncertain') return '图形已生成 · 审核证据不足';
    if (run.artifact?.render_status === 'error') return '图形已生成 · 预览处理失败';
    if (run.review?.status === 'error') return '图形已生成 · 审核失败';
    if (run.status === 'error' && !run.artifact) return '图形生成失败';
  } else if (run.status === 'error') return '请求失败';
  return labels[run.status] || run.status;
}
let data,
  busy = false,
  timer,
  failures = 0,
  hashes = new Map(),
  selectedCategory = 'all',
  modalSequence = 0,
  timeOffset = 0,
  modalView = '',
  historyState = null,
  runTimer,
  currentRunId = null,
  currentRunHash = '',
  logicWindow = null,
  previewRuns = [];
async function api(path) {
  const response = await fetch(path, { signal: AbortSignal.timeout(15000), cache: 'no-store' });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}
function scheduleText(model, kind) {
  const schedule = model.schedules.find((s) => s.kind === kind);
  const running = model.runs.find(
    (r) => r.kind === kind && ['running', 'queued'].includes(r.status),
  );
  if (running)
    return `<span class="countdown testing">◷ ${escapeHTML(stageLabels[running.stage] || labels[running.status])}</span>`;
  if (!schedule?.effective_enabled) return '<span class="countdown muted">已暂停</span>';
  return `<span class="countdown" data-due="${schedule.next_run || 0}">◷ 等待调度</span>`;
}
function modelHTML(model) {
  const candy = model.runs.filter((r) => r.kind === 'candy');
  const schedule = model.schedules.find((s) => s.kind === 'candy');
  const cell = (r) =>
    `<button class="candy-cell ${escapeHTML(r.status)}" data-run="${r.id}" aria-label="${escapeHTML(time(r.created))} ${escapeHTML(labels[r.status])}" title="${escapeHTML(time(r.created))} · ${escapeHTML(labels[r.status])}"></button>`;
  const cells =
    Array(Math.max(0, 50 - candy.length))
      .fill('<span class="candy-cell empty" title="尚无检测记录"></span>')
      .join('') + candy.map(cell).join('');
  return `<section class="model-block" data-model="${model.id}"><div class="model-row"><div class="model-identity"><button class="link-button model-name" data-model-details="${model.id}" aria-label="查看 ${escapeHTML(model.name)} 的模型详情" title="${escapeHTML(model.name)}">${escapeHTML(model.name)}</button><span class="model-template" title="${escapeHTML(schedule?.template_name || (schedule?.enabled ? '逻辑测试' : '未启用逻辑检测'))}">${escapeHTML(schedule?.template_name || (schedule?.enabled ? '逻辑测试' : '未启用逻辑检测'))}</span></div><div class="model-facts"><span class="model-correctness"><span class="model-rate-label">正确率</span><span class="model-rate ${model.candy_rate == null ? 'muted' : model.candy_rate === 100 ? 'green' : ''}" aria-label="正确率 ${percent(model.candy_rate)}">${percent(model.candy_rate)}</span></span>${scheduleText(model, 'candy')}</div><button class="link-button model-latest" ${candy.length ? `data-run="${candy.at(-1).id}" aria-label="查看 ${escapeHTML(model.name)} 的最近逻辑结果"` : 'disabled title="暂无逻辑检测结果"'}>最近结果 ↗</button></div><div class="candy-strip" role="group" aria-label="${escapeHTML(model.name)} 最近 50 轮逻辑结果">${cells}</div></section>`;
}
function galleryStatus(card) {
  const active = card.models.flatMap(m => m.runs).filter(r => r.kind === 'pelican' && ['queued','running'].includes(r.status));
  if (active.length) return `<span class="countdown">${active.length} 个模型图形检测中</span>`;
  const next = card.models.flatMap(m => m.schedules.filter(s => s.kind === 'pelican' && s.effective_enabled).map(s => ({...s,name:m.name}))).sort((a,b)=>(a.next_run||0)-(b.next_run||0))[0];
  return next ? `<span class="countdown" data-due="${next.next_run || 0}" data-prefix="${escapeHTML('下次')}">◷ 等待调度</span>` : '<span class="countdown muted">已暂停</span>';
}
function galleryHTML(model) {
  const images = model.images;
  const enabled = model.models.some(m => m.schedules.some(s => s.kind === 'pelican' && s.enabled));
  const status = galleryStatus(model);
  const strip = images.length ? images.map(i =>
    `<button class="thumbnail" data-run="${i.run_id}" aria-controls="hover-preview" aria-label="${escapeHTML(i.display_name)} · ${escapeHTML(time(i.generated, true))} · ${escapeHTML(labels[i.status])}">${i.has_thumbnail ? `<img src="/api/artifacts/${i.id}/thumbnail" loading="lazy" alt="${escapeHTML(i.display_name)} 的图形结果">` : '<span class="thumb-fallback">查看图形 ↗</span>'}<span class="thumb-name">${escapeHTML(i.display_name)}</span><span class="thumb-time ${escapeHTML(i.status)}"><i class="dot"></i>${escapeHTML(clock(i.generated))}</span></button>`).join('')
    : `<div class="gallery-empty">${enabled ? '等待首张图形结果，后台按计划自动生成' : '未启用图形检测'}</div>`;
  return `<div class="section-row"><h3>图形测试</h3><div class="gallery-toolbar">${status}</div></div><div class="image-strip" data-strip="${model.id}" tabindex="0" role="group" aria-label="${escapeHTML(model.title)} 图形结果，使用左右方向键浏览">${strip}</div>${images.length > 1 ? `<input class="gallery-scroll" data-gallery-scroll="${model.id}" type="range" min="0" max="1000" value="0" aria-label="拖动浏览 ${escapeHTML(model.title)} 的图形结果">` : ''}`;
}
function categoryName(id) {
  return data.categories.find(c => c.id === id)?.name || '未分类';
}
function renderCategoryFilters() {
  const entries = [{ id: 'all', name: '全部', card_count: data.cards.length }, ...data.categories];
  const unclassified = data.cards.filter(m => m.category_id == null).length;
  if (unclassified) entries.push({ id: '', name: '未分类', card_count: unclassified });
  if (!entries.some(c => String(c.id) === selectedCategory)) selectedCategory = 'all';
  const focus = ui.captureFocus($('category-filters'));
  const fresh = document.createElement('div');
  fresh.innerHTML = entries.map(c => `<button class="category-filter ${String(c.id) === selectedCategory ? 'selected' : ''}" data-category-filter="${c.id}" aria-pressed="${String(c.id) === selectedCategory}">${escapeHTML(c.name)} <span>${c.card_count ?? c.model_count}</span></button>`).join('');
  ui.patchChildren($('category-filters'), fresh);
  $('category-filters').hidden = !data.cards.length;
  ui.restoreFocus(focus);
}
function cardHTML(channel, hours) {
  const metrics = channel.metrics,
    tl = channel.timeline;
  let state = !channel.enabled
    ? 'paused'
    : !channel.active_models
      ? 'paused'
      : !metrics.attempts
      ? 'paused'
      : metrics.success_rate === 100
        ? 'normal'
        : metrics.success_rate === 0
          ? 'failed'
          : 'mixed';
  const statusLabel = !channel.enabled
    ? '整张卡片已暂停'
    : !channel.active_models
      ? '全部模型已暂停'
    : !metrics.attempts
      ? '等待检测数据'
      : state === 'normal'
        ? '请求正常'
        : state === 'failed'
          ? '请求异常'
          : '存在请求失败';
  const buckets = tl.buckets
    .map(
      (b, i) =>
        `<button class="segment ${b.availability} ${b.running ? 'active' : ''}" data-bucket="${channel.id}:${i}" aria-label="${escapeHTML(time(b.start))} 成功率 ${percent(b.success_rate)}" title="${escapeHTML(time(b.start))} · ${b.attempts} 次请求 · 成功率 ${percent(b.success_rate)}"></button>`,
    )
    .join('');
  const latency = tl.buckets
    .map(
      (b, i) =>
        `<button class="segment ${b.speed}" data-bucket="${channel.id}:${i}" aria-label="${escapeHTML(time(b.start))} 首次返回 ${seconds(b.first_token_seconds)}" title="${escapeHTML(time(b.start))} · 首次返回 ${seconds(b.first_token_seconds)}"></button>`,
    )
    .join('');
  return `<article class="channel-card" data-channel="${channel.id}"><header class="card-title"><h2>${escapeHTML(channel.title)}</h2><span class="badge">${escapeHTML(categoryName(channel.category_id))} · ${channel.models.length} 个模型 · ${!channel.enabled ? '整卡已暂停' : channel.active_models ? '自动观测' : '模型已暂停'}</span></header><div class="metric-grid"><div class="metric"><div class="metric-label">缓存率</div><strong class="metric-value blue">${percent(metrics.cache_rate)}</strong></div><div class="metric"><div class="metric-label">请求成功率</div><strong class="metric-value ${metrics.success_rate == null ? 'muted' : metrics.success_rate < 90 ? 'red' : 'green'}">${percent(metrics.success_rate)}</strong></div><div class="metric"><div class="metric-label">首次返回</div><strong class="metric-value ${speedClass(metrics.first_token_seconds)}">${metrics.first_token_seconds == null ? '—' : metrics.first_token_seconds.toFixed(2)}${metrics.first_token_seconds == null ? '' : '<small>s</small>'}</strong></div></div><div class="card-stats-link"><button class="link-button" data-card-details="${channel.id}">近 ${hours} 小时 · 统计详情</button></div><section class="channel-section"><div class="section-row"><span class="status-text ${state}"><i class="dot"></i>${statusLabel}</span><button class="link-button" data-definition="true" aria-label="查看指标定义">ⓘ</button></div><div class="segments" aria-label="最近六小时请求可用性">${buckets}</div><div class="latency-heading"><span>首次返回速度</span><div class="legend"><span><i class="fast"></i>&lt;10s</span><span><i class="warm"></i>10–30s</span><span><i class="slow"></i>30–50s</span><span><i class="late"></i>≥50s</span></div></div><div class="segments latency-segments" aria-label="最近六小时首次返回速度">${latency}</div><div class="timeline-label"><span>${escapeHTML(time(tl.start))}</span><span>近 6 小时 · 10 分钟 / 格</span><span data-timeline-end>${escapeHTML(clock(Math.min(tl.end, data.server_time)))}</span></div></section><section class="models-section"><div class="section-row"><h3>逻辑题表现</h3><span class="subtle">最近 50 轮 · 正确率</span></div>${channel.models.map(modelHTML).join('')}</section><section class="gallery-section">${galleryHTML(channel)}</section></article>`;
}
function renderOverview() {
  const stats = data.overview_24h;
  const count = key => stats?.[key] == null ? '—' : Number(stats[key]).toLocaleString('zh-CN');
  const fresh = document.createElement('div');
  fresh.innerHTML = [
    ['participants', '参与监测模型', 'participating_models', '个', `当前监控中 <b>${count('monitoring_models')}</b> 个`],
    ['completed', '累计检测', 'completed', '次', '已完成的逻辑检测'],
    ['anomalies', '异常记录', 'anomalies', '次', `校验未通过 <b>${count('wrong')}</b> · 请求失败 <b>${count('failed')}</b>`],
  ].map(([id, label, key, unit, note]) => `<article class="overview-stat" data-overview="${id}"><h2>${label}<span class="overview-period">24H</span></h2><div class="overview-value${key === 'anomalies' && stats?.anomalies > 0 ? ' has-anomalies' : ''}"><strong>${count(key)}</strong><span>${unit}</span></div><p>${note}</p></article>`).join('');
  ui.patchChildren($('overview-models'), fresh);
  $('overview-breakdown').textContent = stats
    ? `通过 ${count('passed')} · 校验未通过 ${count('wrong')} · 请求失败 ${count('failed')} · 未评分 ${count('ungraded')} · 中断 ${count('interrupted')} · 取消 ${count('cancelled')}${stats.other ? ` · 其他 ${count('other')}` : ''}`
    : '全站统计暂不可用，等待数据同步。';
  $('overview-range').textContent = `${time(data.overview_start ?? data.server_time - 86400)} 至 ${time(data.overview_end ?? data.server_time)} · 北京时间 UTC+8`;
}
function render(next) {
  const focus = ui.captureFocus($('dashboard'));
  data = next;
  // Accept older cached responses during rolling updates.
  data.cards ||= next.models.map(m => ({...m,models:[m],active_models:m.enabled?1:0}));
  timeOffset = next.server_time * 1000 - Date.now();
  renderCategoryFilters();
  const visibleModels = next.cards.filter(m => selectedCategory === 'all' || String(m.category_id ?? '') === selectedCategory);
  renderOverview();
  const root = $('dashboard');
  if (!visibleModels.length) {
    root.innerHTML =
      '<div class="empty-panel"><div class="eyebrow">THE FIRST OBSERVATION AWAITS</div><h2>等待第一张模型卡片</h2><p>管理员创建卡片并添加模型后，将在这里显示；启用检测后持续记录结果。</p><a class="button secondary" href="/admin">进入管理后台 ↗</a></div>';
    hashes.clear();
  } else {
    root.querySelector('.empty-panel')?.remove();
    const visible = new Set(visibleModels.map((c) => c.id));
    root.querySelectorAll('[data-channel]').forEach((el) => {
      if (!visible.has(Number(el.dataset.channel))) {
        hashes.delete(Number(el.dataset.channel));
        el.remove();
      }
    });
    visibleModels.forEach((channel, index) => {
      const hash = JSON.stringify([channel, next.window_hours, categoryName(channel.category_id)]);
      let el = root.querySelector(`[data-channel="${channel.id}"]`);
      if (hashes.get(channel.id) !== hash) {
        const strip = el?.querySelector('.image-strip');
        const previous = strip
          ? {
              left: strip.scrollLeft,
              end: strip.scrollWidth - strip.clientWidth - strip.scrollLeft < 8,
            }
          : null;
        const wrapper = document.createElement('div');
        wrapper.innerHTML = cardHTML(channel, next.window_hours);
        const fresh = wrapper.firstElementChild;
        if (el) ui.patchElement(el, fresh);
        else { root.append(fresh); el = fresh; }
        const newStrip = el.querySelector('.image-strip');
        newStrip.scrollLeft = !previous || previous.end ? newStrip.scrollWidth : previous.left;
        hashes.set(channel.id, hash);
      }
      if (root.children[index] !== el) {
        const strip = el.querySelector('.image-strip'),
          left = strip.scrollLeft;
        root.insertBefore(el, root.children[index] || null);
        strip.scrollLeft = left;
      }
      const endpoint = el.querySelector('[data-timeline-end]');
      if (endpoint) endpoint.textContent = clock(Math.min(channel.timeline.end, next.server_time));
    });
  }
  $('summary').textContent =
    `${visibleModels.length} 张卡片 · ${visibleModels.reduce((sum,c)=>sum+c.models.length,0)} 个模型分别检测`;
  $('updated').textContent = `页面同步 ${time(next.server_time, true)}`;
  updateCountdowns();
  ui.restoreFocus(focus);
  updateGalleryControls();
  positionHoverPreview();
  if (hoverAnchor && !hoverAnchor.isConnected) {
    const replacement = root.querySelector(`.thumbnail[data-run="${hoverAnchor.dataset.run}"]`);
    if (replacement) {
      hoverAnchor = replacement;
      positionHoverPreview();
    } else hideHoverPreview();
  }
}
function updateCountdowns() {
  const now = (Date.now() + timeOffset) / 1000;
  document.querySelectorAll('[data-due]').forEach((el) => {
    const due = Number(el.dataset.due),
      prefix = el.dataset.prefix || '下一轮';
    if (!due || due <= now) {
      el.textContent = `◷ ${el.dataset.prefix ? prefix + ' · ' : ''}等待调度`;
      return;
    }
    const left = Math.ceil(due - now);
    const hours = Math.floor(left / 3600),
      minutes = Math.floor((left % 3600) / 60),
      secs = left % 60;
    el.textContent = `◷ ${prefix} ${hours ? String(hours).padStart(2, '0') + ':' : ''}${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
  });
}
async function refresh() {
  if (busy) return;
  const requestedHours = $('window').value;
  busy = true;
  $('refresh').disabled = true;
  clearTimeout(timer);
  try {
    const next = await api(`/api/dashboard?hours=${requestedHours}`);
    render(next);
    failures = 0;
    $('connection').classList.remove('offline');
    $('connection').innerHTML = '<i class="dot"></i>监控已同步';
    $('notice').hidden = true;
  } catch (error) {
    failures++;
    $('connection').classList.add('offline');
    $('connection').innerHTML = '<i class="dot"></i>同步中断';
    $('notice').hidden = false;
    $('notice').textContent = `同步失败：${error.message}。页面保留上次结果，稍后自动重试。`;
  } finally {
    busy = false;
    $('refresh').disabled = false;
    timer = setTimeout(
      refresh,
      requestedHours !== $('window').value
        ? 0
        : document.hidden
          ? 60000
          : Math.min(60000, 15000 * 2 ** Math.min(failures, 2)),
    );
  }
}
function openModal(title, body, view = '') {
  clearTimeout(runTimer);
  if (hoverPreview.contains(document.activeElement)) hoverAnchor?.focus({ preventScroll: true });
  hideHoverPreview();
  modalView = view;
  currentRunId = null;
  currentRunHash = '';
  previewRuns = [];
  logicWindow = null;
  updateLogicNavigation();
  if (!['run', 'history'].includes(view)) historyState = null;
  modalSequence++;
  $('detail-title').textContent = title;
  $('detail-body').innerHTML = body;
  $('detail-body').scrollTop = 0;
  $('detail-back').hidden = !(view === 'run' && historyState);
  ui.openDialog($('detail-dialog'), $('close-detail'));
  return modalSequence;
}
const detailField = (label, value) =>
  `<div><small>${escapeHTML(label)}</small><strong>${escapeHTML(value)}</strong></div>`;
function logicWindowFor(id, modelId) {
  if (modelId == null && modalView === 'run' && logicWindow?.ids.includes(id)) return logicWindow;
  const model = data?.models.find(m => (modelId == null || m.id === modelId) &&
    m.runs.some(r => r.kind === 'candy' && r.id === id));
  const runs = modalView === 'history' ? historyState?.items : model?.runs;
  const selected = runs?.find(r => r.id === id && r.kind === 'candy');
  if (!selected) return null;
  return { modelId: selected.model_id, ids: runs.filter(r => r.kind === 'candy' &&
    r.model_id === selected.model_id).map(r => r.id).sort((a, b) => a - b) };
}
function updateLogicNavigation() {
  const index = logicWindow?.ids.indexOf(currentRunId) ?? -1;
  $('detail-dialog').classList.toggle('logic-detail', index >= 0);
  $('logic-navigation').hidden = index < 0;
  $('logic-position').textContent = index < 0 ? '' : `窗口内 ${index + 1} / ${logicWindow.ids.length} 轮`;
  const focused = document.activeElement;
  $('logic-prev').disabled = index <= 0;
  $('logic-next').disabled = index < 0 || index >= logicWindow.ids.length - 1;
  if (focused?.matches('#logic-prev, #logic-next') && focused.disabled)
    (focused.id === 'logic-prev' ? $('logic-next') : $('logic-prev')).focus({ preventScroll: true });
}
async function showRun(id, polling = false, modelId = null) {
  if (polling && (modalView !== 'run' || currentRunId !== id || !$('detail-dialog').open)) return;
  if (polling && document.hidden) {
    runTimer = setTimeout(() => showRun(id, true), 15000);
    return;
  }
  let sequence = modalSequence;
  if (!polling) {
    const nextWindow = logicWindowFor(id, modelId);
    if (modalView === 'history') rememberHistory();
    if (modalView === 'run' && $('detail-dialog').open) {
      clearTimeout(runTimer);
      sequence = ++modalSequence;
      currentRunHash = '';
      previewRuns = [];
      $('detail-body').innerHTML = '<div class="muted-box" role="status">正在读取记录…</div>';
      $('detail-body').scrollTop = 0;
    } else sequence = openModal('检测详情', '<div class="muted-box" role="status">正在读取记录…</div>', 'run');
    logicWindow = nextWindow;
  }
  currentRunId = id;
  updateLogicNavigation();
  try {
    const run = await api(`/api/runs/${id}`);
    if (sequence !== modalSequence || !$('detail-dialog').open || currentRunId !== id) return;
    if (run.kind === 'candy' && !logicWindow) logicWindow = { modelId: run.model_id, ids: [id] };
    updateLogicNavigation();
    const pending = ['running', 'queued'].includes(run.status);
    const hash = JSON.stringify(run);
    if (polling && hash === currentRunHash) {
      $('detail-body').querySelector('.detail-refresh-status').textContent = pending ? `${stageLabels[run.stage] || labels[run.status]} · 结果自动更新` : '';
      if (pending) runTimer = setTimeout(() => showRun(id, true), 3000);
      return;
    }
    const scroll = $('detail-body').scrollTop;
    const focus = polling ? ui.captureFocus($('detail-body')) : null;
    const expanded = $('detail-body').querySelector('details')?.open;
    currentRunHash = hash;
    const meta = run.config;
    const primary = run.attempts.filter((a) => a.role === 'primary');
    const last = primary[primary.length - 1];
    const imageChannel = data?.cards.find((c) => c.images.some((i) => i.run_id === run.id));
    const historyImages = historyState?.scope === 'card' && historyState?.kind === 'pelican' ? historyState.items?.filter(r => r.has_graphic).map(r => ({run_id:r.id})) : null;
    const images = historyImages?.some(i => i.run_id === run.id) ? historyImages : imageChannel?.images || [];
    const imageIndex = images.findIndex((i) => i.run_id === run.id);
    previewRuns = images.map((i) => i.run_id);
    const navigation =
      imageIndex >= 0
        ? `<div class="preview-navigation"><button class="button secondary compact" ${imageIndex > 0 ? `data-run="${images[imageIndex - 1].run_id}"` : 'disabled'}>← 上一张</button><span>${imageIndex + 1} / ${images.length}</span><button class="button secondary compact" ${imageIndex < images.length - 1 ? `data-run="${images[imageIndex + 1].run_id}"` : 'disabled'}>下一张 →</button></div>`
        : '';
    $('detail-title').textContent =
      `${meta.title || '历史卡片'} / ${meta.display_name || meta.model || '历史模型'} · ${run.kind === 'candy' ? '逻辑检测' : '图形测试'}`;
    const checks = Object.entries(run.checks)
      .map(
        ([key, value]) =>
          `<span class="badge ${value ? 'passed' : 'invalid'}">${escapeHTML({ answer_21: '答案包含独立数字 21', expected_answer: '模板答案匹配', svg: '安全 SVG', animation: '动画声明', nonce: '校验码' }[key] || key)} ${value ? '✓' : '×'}</span>`,
      )
      .join('');
    const preview =
      run.kind === 'pelican' && (run.artifact || run.source === 'legacy')
        ? `<img class="preview-image" src="/api/runs/${run.id}/svg" alt="${escapeHTML(meta.model)} 生成的 SVG 图形">`
        : '';
    const attempts = run.attempts.length
      ? `<h3>请求尝试</h3><div class="table-scroll"><table><thead><tr><th>请求</th><th>结果</th><th>首次返回</th><th>完整耗时</th><th>输入 / 缓存 Token</th></tr></thead><tbody>${run.attempts.map((a) => `<tr><td>${a.role === 'review' ? '视觉审核' : '主检测'} #${a.attempt}</td><td>${escapeHTML({ success: '成功', failed: '失败', timeout: '超时', running: '请求中', interrupted: '中断' }[a.status] || a.status)}${a.http_status ? ` · HTTP ${a.http_status}` : ''}</td><td>${seconds(a.first_token_seconds)}</td><td>${seconds(a.duration_seconds)}</td><td>${a.input_tokens ?? '—'} / ${a.cached_tokens ?? '—'}</td></tr>`).join('')}</tbody></table></div>`
      : '<p class="subtle">此历史记录未采集请求计时与缓存指标。</p>';
    $('detail-body').innerHTML =
      `${preview}${navigation}<p class="detail-refresh-status" role="status">${pending ? `${escapeHTML(stageLabels[run.stage] || labels[run.status])} · 结果自动更新` : ''}</p><div class="detail-grid">${detailField('所属卡片', meta.title || '—')}${detailField('模型名称', meta.display_name || meta.model || '—')}${detailField('模型 ID', meta.model || '—')}${run.template ? detailField('提示词模板', `${run.template.name} · v${run.template.revision}`) : ''}${detailField('检测结果', resultLabel(run))}${detailField(run.kind === 'pelican' ? '图形生成时间' : '检测完成时间', time(run.artifact?.generated || run.finished, true))}${detailField('协议 / 思考强度', `${meta.protocol || '—'} / ${meta.effort || '—'}`)}${detailField('首次有效返回', seconds(last?.first_token_seconds))}${detailField('检测总耗时', run.finished && run.started ? seconds(run.finished - run.started) : '—')}</div><div class="check-tags">${checks}</div>${run.error ? `<p class="notice">${escapeHTML(run.error)}</p>` : ''}${run.review?.status ? `<h3>视觉审核</h3><p class="subtle">${escapeHTML({ passed: '审核通过', invalid: '审核未通过', uncertain: '证据不足', error: '审核失败' }[run.review.status] || run.review.status)} · ${escapeHTML(run.review.reason || '')}</p>` : ''}${attempts}<details ${run.kind === 'candy' ? 'open' : ''}><summary>测试提示词与模型回答</summary><h3>测试提示词</h3><pre>${escapeHTML(run.prompt)}</pre><h3>模型回答</h3><pre>${escapeHTML(run.output || '尚无回答，或已超过内容保留时间')}</pre></details><p class="archive-note">${run.kind === 'candy' ? `规则版本：${escapeHTML(run.rule_version)}。此结果按当次模板判定，表示本题表现，不代表模型整体能力。` : '动画生成与视觉审核分别记录，审核可能存在误判。'}</p>`;
    if (polling) {
      if (expanded !== undefined) $('detail-body').querySelector('details').open = expanded;
      $('detail-body').scrollTop = scroll;
      ui.restoreFocus(focus);
    }
    if (pending) runTimer = setTimeout(() => showRun(id, true), 3000);
  } catch (error) {
    if (sequence !== modalSequence || !$('detail-dialog').open) return;
    if (polling) {
      $('detail-body').querySelector('.detail-refresh-status').textContent = '进度同步暂时中断，稍后自动重试。';
      runTimer = setTimeout(() => showRun(id, true), 5000);
    } else $('detail-body').innerHTML = `<p class="notice">${escapeHTML(error.message)}</p><button class="button secondary" data-run="${id}">重新读取</button>`;
  }
}
function rememberHistory() {
  if (!historyState || modalView !== 'history') return;
  historyState.body = $('detail-body').innerHTML;
  historyState.scroll = $('detail-body').scrollTop;
  historyState.focus = ui.captureFocus($('detail-body'));
}
function restoreHistory(state = historyState) {
  if (!state) return;
  historyState = state;
  openModal(state.title, state.body, 'history');
  $('detail-body').scrollTop = state.scroll || 0;
  ui.restoreFocus(state.focus);
}
async function showHistory(modelId, before = '', kind = '', previous = null, scope = 'model') {
  const model = (scope === 'card' ? data.cards : data.models).find(m => m.id === modelId);
  const title = `${model?.title || '历史配置'} · ${kind === 'candy' ? '逻辑检测记录' : kind === 'pelican' ? '全部模型图形记录' : '全部模型检测记录'}`;
  const sequence = openModal(
    title,
    '<div class="muted-box">正在读取历史…</div>',
    'history',
  );
  historyState = { modelId, kind, before, previous, title, scope, body: '', scroll: 0 };
  try {
    const history = await api(
      `/api/${scope === 'card' ? 'cards' : 'models'}/${modelId}/runs?${kind ? `kind=${kind}&` : ''}limit=50${before ? '&before=' + before : ''}`,
    );
    if (sequence !== modalSequence) return;
    historyState.items = history.items;
    $('detail-body').innerHTML = history.items.length
      ? `<div class="history-list">${history.items.map((r) => `<button class="history-record" data-run="${r.id}"><span><strong>${escapeHTML(r.display_name || model?.name || '历史模型')}</strong><small>${escapeHTML(time(r.created, true))} · ${r.kind === 'candy' ? '逻辑题' : '图形'}</small></span><span class="badge ${escapeHTML(r.status)}">${escapeHTML(labels[r.status] || r.status)}</span><span class="history-arrow" aria-hidden="true">→</span></button>`).join('')}</div>`
      : '<div class="muted-box">尚无检测记录</div>';
    $('detail-body').insertAdjacentHTML('beforeend', `<div class="history-pagination"><button class="button secondary" data-history-prev ${previous ? '' : 'disabled'}>← 较新记录</button><button class="button secondary" data-history-more="${history.next_before || ''}" ${history.next_before ? '' : 'disabled'}>更早记录 →</button></div>`);
    rememberHistory();
  } catch (error) {
    if (sequence === modalSequence)
      $('detail-body').innerHTML = `<p class="notice">${escapeHTML(error.message)}</p>`;
  }
}
document.addEventListener('click', (event) => {
  const target = event.target.closest('button');
  if (!target) return;
  if (target.hasAttribute('data-category-filter')) { selectedCategory = target.dataset.categoryFilter; render(data); return; }
  if (target.dataset.modelDetails) {
    const model = data.models.find(m => m.id === Number(target.dataset.modelDetails));
    if (!model) return;
    const schedule = model.schedules.find(s => s.kind === 'candy');
    const runs = model.runs.filter(r => r.kind === 'candy');
    return openModal(`${model.card_title || model.title} / ${model.name}`, `<div class="detail-grid">${detailField('模型 ID', model.model)}${detailField('协议 / 思考强度', `${model.protocol === 'chat' ? 'Chat' : 'Responses'} / ${model.effort}`)}${detailField('提示词模板', schedule?.template_name || '—')}${detailField('逻辑检测频率', schedule?.enabled ? `每 ${schedule.interval_minutes} 分钟` : '已关闭')}${detailField('缓存率', percent(model.metrics.cache_rate))}${detailField('最近 50 轮正确率', percent(model.candy_rate))}${detailField('通过 / 答错', `${model.passed} / ${model.wrong}`)}${detailField('无有效结果 / 未评分 / 待完成', `${model.unavailable} / ${model.ungraded || 0} / ${model.pending}`)}</div><div class="detail-actions">${runs.length ? `<button class="button secondary" data-run="${runs.at(-1).id}" data-run-model="${model.id}">最近结果</button>` : ''}<button class="button secondary" data-model-history="${model.id}">查看逻辑记录</button></div>`);
  }
  if (target.dataset.cardDetails) {
    const card = data.cards.find(c => c.id === Number(target.dataset.cardDetails));
    if (!card) return;
    const stats = card.metrics;
    return openModal(`${card.title} · 近 ${data.window_hours} 小时统计`, `<div class="detail-grid">${detailField('主请求次数（含重试）', stats.attempts)}${detailField('成功计时样本', stats.timing_samples)}${detailField('缓存有效样本', stats.cache_samples)}${detailField('缓存样本覆盖', percent(stats.cache_coverage))}</div><p class="subtle">统计汇总本卡片全部模型的主请求，不含视觉审核。首次返回表示首个有效内容的等待时间。</p><div class="detail-actions"><button class="button secondary" data-history="${card.id}">查看检测记录</button><button class="button secondary" data-definition="true">指标定义</button></div>`);
  }
  if (target.dataset.cardGraphicsHistory) return showHistory(Number(target.dataset.cardGraphicsHistory), '', 'pelican', null, 'card');
  if (target.dataset.modelHistory) return showHistory(Number(target.dataset.modelHistory), '', 'candy');
  if (target.hasAttribute('data-history-prev')) return restoreHistory(historyState?.previous);
  if (target.dataset.historyMore) {
    rememberHistory();
    return showHistory(historyState.modelId, target.dataset.historyMore, historyState.kind, historyState, historyState.scope);
  }
  if (target.dataset.run && !target.closest('#detail-dialog')) historyState = null;
  if (target.dataset.run) return showRun(Number(target.dataset.run), false,
    target.dataset.runModel ? Number(target.dataset.runModel) : target.closest('[data-model]') ? Number(target.closest('[data-model]').dataset.model) : null);
  if (target.dataset.history)
    return showHistory(Number(target.dataset.history), target.dataset.before, '', null, 'card');
  if (target.dataset.definition)
    return openModal(
      '指标如何计算',
      `<div class="detail-grid">${Object.entries(data.definitions)
        .map(([key, value]) =>
          detailField(
            {
              cache: '缓存率',
              success: '请求成功率',
              first_token: '平均首次返回',
              candy: '模型逻辑正确率',
            }[key],
            value,
          ),
        )
        .join(
          '',
        )}</div><p class="subtle">灰色表示未提供、未检测或无有效结果。上方请求时间条每格 10 分钟，下方模型检测条每格一轮。图形视觉审核请求单独记录。</p>`,
    );
  if (target.dataset.bucket) {
    const [cid, index] = target.dataset.bucket.split(':').map(Number),
      channel = data.cards.find((c) => c.id === cid),
      bucket = channel.timeline.buckets[index];
    openModal(
      `${channel.title} · ${time(bucket.start)}`,
      `<div class="detail-grid">${detailField('主检测请求', bucket.attempts + ' 次')}${detailField('成功请求', bucket.successful + ' 次')}${detailField('请求成功率', percent(bucket.success_rate))}${detailField('平均首次返回', seconds(bucket.first_token_seconds))}${detailField('计时样本', bucket.timing_samples + ' 个')}${detailField('缓存率', percent(bucket.cache_rate))}</div><p class="subtle">该格覆盖 10 分钟。没有有效内容返回的失败请求不计算首次返回平均值。</p>`,
    );
  }
});
$('refresh').addEventListener('click', refresh);
$('window').addEventListener('change', () => {
  hashes.clear();
  refresh();
});
$('close-detail').addEventListener('click', () => ui.requestClose($('detail-dialog')));
$('detail-back').addEventListener('click', () => restoreHistory());
$('logic-prev').addEventListener('click', () => navigatePreview(-1));
$('logic-next').addEventListener('click', () => navigatePreview(1));
ui.bindDialog($('detail-dialog'), {
  fallbackFocus: $('refresh'),
  onClose: () => {
    modalSequence++;
    clearTimeout(runTimer);
    currentRunId = null;
    logicWindow = null;
    previewRuns = [];
    updateLogicNavigation();
    historyState = null;
    modalView = '';
    $('detail-body').replaceChildren();
  },
});
function updateGalleryControls() {
  document.querySelectorAll('[data-strip]').forEach(strip => {
    const slider = strip.parentElement.querySelector('[data-gallery-scroll]');
    if (!slider) return;
    const distance = Math.max(0, strip.scrollWidth - strip.clientWidth);
    slider.hidden = distance <= 1;
    slider.disabled = distance <= 1;
    slider.value = distance ? Math.round(strip.scrollLeft / distance * 1000) : 0;
  });
}
document.addEventListener('input', event => {
  const slider = event.target.closest('[data-gallery-scroll]');
  if (!slider) return;
  const strip = slider.parentElement.querySelector('[data-strip]');
  hideHoverPreview();
  strip.scrollLeft = Number(slider.value) / 1000 * Math.max(0, strip.scrollWidth - strip.clientWidth);
});
const hoverPreview = $('hover-preview');
const hoverCapable = matchMedia('(hover: hover) and (pointer: fine)');
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
let hoverAnchor = null,
  hoverOpenTimer,
  hoverCloseTimer;
function hideHoverPreview() {
  clearTimeout(hoverOpenTimer);
  clearTimeout(hoverCloseTimer);
  hoverAnchor?.removeAttribute('aria-expanded');
  hoverAnchor = null;
  hoverPreview.hidden = true;
  hoverPreview.replaceChildren();
}
function positionHoverPreview() {
  if (!hoverAnchor || hoverPreview.hidden) return;
  const rect = hoverAnchor.getBoundingClientRect();
  if (!hoverAnchor.isConnected || rect.bottom < 0 || rect.top > innerHeight) return hideHoverPreview();
  const strip = hoverAnchor.closest('.image-strip')?.getBoundingClientRect();
  if (strip && (rect.right <= strip.left || rect.left >= strip.right)) return hideHoverPreview();
  const width = Math.min(360, innerWidth - 24);
  hoverPreview.style.width = `${width}px`;
  const height = hoverPreview.offsetHeight;
  const above = rect.top - height - 10;
  hoverPreview.style.left = `${Math.max(12, Math.min(innerWidth - width - 12, rect.left + rect.width / 2 - width / 2))}px`;
  hoverPreview.style.top = `${Math.max(12, Math.min(innerHeight - height - 12, above >= 12 ? above : rect.bottom + 10))}px`;
}
function queueHoverPreview(anchor) {
  if (!hoverCapable.matches || $('detail-dialog').open) return;
  clearTimeout(hoverCloseTimer);
  if (hoverAnchor === anchor && !hoverPreview.hidden) return;
  hideHoverPreview();
  hoverAnchor = anchor;
  hoverOpenTimer = setTimeout(() => {
    if (!anchor.isConnected || $('detail-dialog').open) return hideHoverPreview();
    const image = data?.cards.flatMap((c) => c.images).find((i) => i.run_id === Number(anchor.dataset.run));
    if (!image) return hideHoverPreview();
    const animate = !reducedMotion.matches;
    const thumbnail = image.has_thumbnail ? `/api/artifacts/${image.id}/thumbnail` : '';
    const source = animate ? `/api/runs/${image.run_id}/svg` : thumbnail;
    hoverPreview.innerHTML = `${source ? `<img src="${source}" ${animate ? `data-hover-animation data-static-src="${thumbnail}"` : ''} alt="${escapeHTML(image.display_name)} 的${animate ? '动画' : '静态'}预览">` : '<div class="muted-box">静态预览暂不可用，可打开详情查看图形。</div>'}<div class="hover-meta"><strong>${escapeHTML(image.display_name)}</strong><span class="badge ${escapeHTML(image.status)}">${escapeHTML(labels[image.status] || image.status)}</span><small>${escapeHTML(time(image.generated, true))}</small></div><p class="hover-playback-status" role="status">${animate ? '正在加载动画…' : '已按减少动态效果偏好显示静态预览'}</p><button class="button secondary" data-run="${image.run_id}">查看动画与详情 →</button>`;
    hoverPreview.hidden = false;
    anchor.setAttribute('aria-expanded', 'true');
    positionHoverPreview();
    hoverPreview.querySelector('img')?.addEventListener('load', (event) => {
      if (!event.target.isConnected) return;
      if (event.target.hasAttribute('data-hover-animation'))
        hoverPreview.querySelector('.hover-playback-status').textContent = '原始图形预览 · 移开鼠标后停止';
      positionHoverPreview();
    });
  }, 250);
}
function queueHoverClose() {
  clearTimeout(hoverOpenTimer);
  clearTimeout(hoverCloseTimer);
  hoverCloseTimer = setTimeout(hideHoverPreview, 150);
}
document.addEventListener('pointerover', (event) => {
  if (event.pointerType !== 'mouse') return;
  if (hoverPreview.contains(event.target)) {
    clearTimeout(hoverCloseTimer);
    return;
  }
  const anchor = event.target.closest('.thumbnail');
  if (anchor && !anchor.contains(event.relatedTarget)) queueHoverPreview(anchor);
});
document.addEventListener('pointerout', (event) => {
  const anchor = event.target.closest('.thumbnail');
  if ((anchor && !anchor.contains(event.relatedTarget) && !hoverPreview.contains(event.relatedTarget)) ||
      (hoverPreview.contains(event.target) && !hoverPreview.contains(event.relatedTarget) && !hoverAnchor?.contains(event.relatedTarget))) queueHoverClose();
});
document.addEventListener('focusin', (event) => {
  const anchor = event.target.closest('.thumbnail');
  if (anchor?.matches(':focus-visible')) queueHoverPreview(anchor);
  else if (!hoverPreview.contains(event.target)) queueHoverClose();
});
document.addEventListener('focusout', (event) => {
  if (!hoverPreview.contains(event.relatedTarget) && !hoverAnchor?.contains(event.relatedTarget)) queueHoverClose();
});
document.addEventListener('dialogopening', hideHoverPreview);
document.addEventListener('scroll', (event) => {
  if (event.target instanceof Element && event.target.matches('.image-strip')) updateGalleryControls();
  positionHoverPreview();
}, true);
window.addEventListener('resize', () => { positionHoverPreview(); updateGalleryControls(); });
hoverCapable.addEventListener('change', hideHoverPreview);
reducedMotion.addEventListener('change', hideHoverPreview);
function navigatePreview(direction) {
  const runs = logicWindow?.ids || previewRuns;
  const index = runs.indexOf(currentRunId);
  const id = index >= 0 ? runs[index + direction] : null;
  if (id) showRun(id);
}
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !$('detail-dialog').open) hideHoverPreview();
  const strip = event.target.closest('[data-strip]');
  if (strip && !$('detail-dialog').open && !event.altKey && !event.ctrlKey && !event.metaKey &&
      (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) {
    event.preventDefault();
    hideHoverPreview();
    const direction = event.key === 'ArrowLeft' ? -1 : 1;
    const images = [...strip.querySelectorAll('.thumbnail')];
    const index = images.indexOf(event.target.closest('.thumbnail'));
    if (index >= 0) {
      const next = images[Math.max(0, Math.min(images.length - 1, index + direction))];
      next.focus({preventScroll: true});
      const box = next.getBoundingClientRect(), viewport = strip.getBoundingClientRect();
      if (box.left < viewport.left) strip.scrollLeft += box.left - viewport.left;
      else if (box.right > viewport.right) strip.scrollLeft += box.right - viewport.right;
    } else strip.scrollLeft += direction * 120;
    return;
  }
  if (!$('detail-dialog').open || modalView !== 'run' || event.altKey || event.ctrlKey || event.metaKey ||
      event.target.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"])') || !getSelection().isCollapsed) return;
  if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
    event.preventDefault();
    navigatePreview(event.key === 'ArrowLeft' ? -1 : 1);
  }
});
let swipeStart;
$('detail-body').addEventListener('pointerdown', (event) => {
  swipeStart = event.pointerType === 'touch' && event.target.matches('.preview-image')
    ? { x: event.clientX, y: event.clientY, id: event.pointerId }
    : null;
});
$('detail-body').addEventListener('pointerup', (event) => {
  if (swipeStart?.id === event.pointerId && Math.abs(event.clientX - swipeStart.x) >= 60 && Math.abs(event.clientY - swipeStart.y) < 45)
    navigatePreview(event.clientX < swipeStart.x ? 1 : -1);
  swipeStart = null;
});
$('detail-body').addEventListener('pointercancel', () => { swipeStart = null; });
document.addEventListener(
  'error',
  (event) => {
    const img = event.target;
    if (!(img instanceof HTMLImageElement)) return;
    if (img.closest('.thumbnail')) {
      const fallback = document.createElement('span');
      fallback.className = 'thumb-fallback';
      fallback.textContent = '预览暂不可用 ↗';
      img.replaceWith(fallback);
    } else if (img.closest('.hover-preview')) {
      const status = hoverPreview.querySelector('.hover-playback-status');
      if (img.hasAttribute('data-hover-animation') && img.dataset.staticSrc) {
        img.removeAttribute('data-hover-animation');
        img.alt = '动画暂不可用，显示静态图形预览';
        if (status) status.textContent = '动画暂不可用，已恢复静态预览';
        img.src = img.dataset.staticSrc;
        return;
      }
      const fallback = document.createElement('div');
      fallback.className = 'muted-box';
      fallback.textContent = '预览暂不可用，可打开详情查看图形。';
      if (status) status.textContent = '';
      img.replaceWith(fallback);
      positionHoverPreview();
    } else if (img.classList.contains('preview-image')) {
      const fallback = document.createElement('p');
      fallback.className = 'notice';
      fallback.textContent = '图形暂时无法读取，请稍后重试。';
      img.replaceWith(fallback);
    }
  },
  true,
);
document.addEventListener('visibilitychange', () => {
  hideHoverPreview();
  clearTimeout(timer);
  if (!document.hidden) refresh();
  else timer = setTimeout(refresh, 60000);
});
setInterval(updateCountdowns, 1000);
refresh();
