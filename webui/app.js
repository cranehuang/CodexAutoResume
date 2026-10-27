'use strict';

const $ = (id) => document.getElementById(id);
const REFRESH_MS = 10000;
const QUOTA_MS = 30000;

const WAITING = {
  idle: ['无候选', 'ok'],
  quota_blocked: ['额度耗尽，等待恢复', 'warn'],
  quota_unknown: ['额度状态未知，等待', 'warn'],
  dry_run: ['dry-run：只报告不发送', 'info'],
  dispatching: ['发送中', 'info'],
  sent: ['本轮已处理', 'ok'],
  scan_failed: ['扫描失败', 'bad'],
  transport_error: ['传输异常，等待下一轮', 'bad'],
};

const ATTEMPT_STATE = {
  submitted: ['submitted', 'ok'],
  unknown: ['unknown（未确认，不重发）', 'bad'],
  submit_unknown: ['未知（未确认）', 'warn'],
};

let overview = null;
let liveQuota = null;
let liveQuotaAt = 0;
let toastTimer = null;

/* ---------- helpers ---------- */

function make(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (cls) node.className = cls;
  return node;
}

function cell(text, cls, title) {
  const td = make('td', text, cls);
  if (title) td.title = title;
  if (cls && cls.includes('mono')) td.classList.add('cell-clip');
  return td;
}

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleString('zh-CN', { hour12: false });
}

function fmtReset(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const now = new Date();
  const time = d.toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit' });
  if (d.toDateString() === now.toDateString()) return time;
  return `${d.getMonth() + 1}月${d.getDate()}日 ${time}`;
}

function fmtAge(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟`;
  return `${Math.floor(seconds / 3600)} 小时`;
}

function shortId(id) {
  return id ? String(id).slice(0, 10) : '—';
}

function baseName(path) {
  if (!path) return '';
  const trimmed = String(path).replace(/[\\/]+$/, '');
  const cut = Math.max(trimmed.lastIndexOf('/'), trimmed.lastIndexOf('\\'));
  return cut >= 0 ? trimmed.slice(cut + 1) : trimmed;
}

function pct(value) {
  return typeof value === 'number' ? Math.max(0, Math.min(100, value)) : null;
}

function toast(message, isError = false) {
  const node = $('toast');
  node.textContent = message;
  node.className = 'toast' + (isError ? ' error' : '');
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, 4000);
}

async function api(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), options.timeout || 15000);
  try {
    const response = await fetch(path, { ...options, signal: controller.signal });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `${response.status} ${response.statusText}`);
    return data;
  } finally {
    clearTimeout(timeout);
  }
}

function renderMetrics(container, items) {
  container.replaceChildren(...items.map(([key, value, hint]) => {
    const box = make('div', null, 'metric');
    box.append(make('div', key, 'k'), make('div', value, 'v'));
    if (hint) box.append(make('div', hint, 'muted small'));
    return box;
  }));
}

/* ---------- render ---------- */

function renderHeader() {
  const status = (overview && overview.status) || {};
  const service = (overview && overview.service) || {};
  const running = service.running;

  $('daemon-dot').className = 'dot ' + (running ? 'ok' : 'bad');
  $('daemon-badge').textContent = running ? '守护进程运行中' : '守护进程未运行';
  $('daemon-badge').className = 'badge ' + (running ? 'ok' : 'bad');

  const mode = status.mode;
  $('mode-badge').textContent = mode === 'send' ? 'SEND · 真实续跑' : mode === 'dry-run' ? 'DRY-RUN · 不发送' : '模式未知';
  $('mode-badge').className = 'badge ' + (mode === 'send' ? 'warn' : 'info');

  $('updated').textContent = overview
    ? `快照 ${fmtTime(status.updated_at)}${overview.status_age_sec == null ? '' : ` · ${fmtAge(overview.status_age_sec)}前`}`
    : '尚未连接';
}

function renderScan() {
  const status = (overview && overview.status) || {};
  const scan = status.scan;
  if (!scan) {
    renderMetrics($('scan-metrics'), [['状态', '暂无扫描数据']]);
    return;
  }
  renderMetrics($('scan-metrics'), [
    ['Threads', scan.threads ?? '—'],
    ['候选', scan.candidates ?? '—'],
    ['未监控', scan.unmonitored ?? '—'],
    ['本轮耗时', scan.duration_ms == null ? '—' : `${(scan.duration_ms / 1000).toFixed(1)}s`],
    ['已提交去重', scan.duplicates ?? '—'],
    ['重连', scan.reconnects ?? '—'],
    ['错误', scan.errors ?? '—'],
  ]);
}

function renderWaiting() {
  const status = (overview && overview.status) || {};
  const box = $('waiting-body');
  box.replaceChildren();

  if (!status.waiting) {
    box.append(make('p', '暂无扫描记录。', 'muted'));
    return;
  }
  const [label, kind] = WAITING[status.waiting] || [status.waiting, 'info'];
  const line = make('div', null, 'quota-line');
  line.append(make('span', label, 'badge ' + kind),
              make('span', `${(status.scan && status.scan.candidates) || 0} 个候选`, 'muted'));
  box.append(line);

  if (status.submitted && status.submitted.length) {
    const ids = status.submitted.map((s) => shortId(s.thread_id)).join(', ');
    box.append(make('div', `本轮已提交：${ids}`, 'muted small'));
  }
  if (status.last_error) box.append(make('div', status.last_error, 'error'));

  const interval = status.interval;
  if (interval) {
    const next = overview && overview.status_age_sec != null
      ? Math.max(0, interval - overview.status_age_sec) : null;
    box.append(make('div', next == null ? `扫描间隔 ${interval}s` : `约 ${next}s 后下一轮`, 'muted small'));
  }
}

function renderAttemptStats() {
  const attempts = (overview && overview.attempts) || { counts: {}, total: 0, last_24h: 0 };
  const counts = attempts.counts || {};
  const unknown = (counts.unknown || 0) + (counts.submit_unknown || 0);
  renderMetrics($('attempt-metrics'), [
    ['总记录', attempts.total ?? 0],
    ['24 小时', attempts.last_24h ?? 0],
    ['已提交', counts.submitted || 0],
    ['未确认', unknown],
  ]);
}

function windowLabel(name, window_) {
  const mins = window_ && window_.windowDurationMins;
  if (typeof mins === 'number' && mins > 0) {
    if (mins >= 525600) return '每年额度';
    if (mins >= 43200) return '每月额度';
    if (mins >= 10080) return '每周额度';
    if (mins >= 1440) return '每天额度';
    if (mins >= 300) return '5 小时额度';
    if (mins % 60 === 0) return `${mins / 60} 小时额度`;
    return `${mins} 分钟额度`;
  }
  return name === 'primary' ? '短周期额度' : '长周期额度';
}

function currentQuota() {
  const snapshot = (overview && overview.status && overview.status.quota) || null;
  if (liveQuota && (liveQuota.primary || liveQuota.secondary)) {
    return { data: liveQuota, live: true, error: null };
  }
  return { data: snapshot, live: false, error: liveQuota ? liveQuota.error : null };
}

function renderQuota() {
  const { data: quota, live, error } = currentQuota();
  const box = $('quota-body');
  box.replaceChildren();

  if (!quota) {
    box.append(make('p', '尚未获取额度信息。', 'muted'));
    if (error) box.append(make('div', error, 'error'));
    return;
  }

  const line = make('div', null, 'quota-line');
  line.append(make('span', '额度状态', 'muted'),
              make('span', quota.ordinaryUsageAllowed ? '可用' : '受限',
                   'badge ' + (quota.ordinaryUsageAllowed ? 'ok' : 'bad')));
  box.append(line);

  for (const name of ['primary', 'secondary']) {
    const window_ = quota[name];
    if (!window_) continue;
    const used = pct(window_.usedPercent);
    const remaining = used == null ? null : Math.max(0, Math.min(100, Math.round(100 - used)));
    const head = make('div', null, 'quota-head');
    head.append(make('span', windowLabel(name, window_)),
                make('span', remaining == null ? '—' : `剩余 ${remaining}%`));
    const bar = make('div', null, 'bar');
    const fill = make('i');
    if (remaining != null) {
      fill.style.width = `${remaining}%`;
      fill.className = remaining <= 15 ? 'bad' : remaining <= 50 ? 'warn' : '';
    }
    bar.append(fill);
    const reset = window_.resetsAt ? `重置 ${fmtReset(window_.resetsAt)}` : '';
    const block = make('div', null, 'quota-window');
    block.append(head, bar);
    if (reset) block.append(make('div', reset, 'quota-reset'));
    box.append(block);
  }

  const source = live
    ? `实时探测 · ${fmtAge(Math.max(0, Math.round((Date.now() - liveQuotaAt) / 1000)))}前`
    : '取自上次扫描快照';
  box.append(make('div', source, 'quota-reset'));
  if (error) box.append(make('div', error, 'error'));
}

function renderCandidates() {
  const status = (overview && overview.status) || {};
  const rows = status.eligible || [];
  const body = $('candidates-body');
  body.replaceChildren(...rows.map((item) => {
    const tr = document.createElement('tr');
    const who = make('div', item.title || shortId(item.thread_id));
    who.append(make('div', shortId(item.thread_id), 'mono muted'));
    const whoCell = document.createElement('td');
    whoCell.append(who);
    tr.append(
      whoCell,
      cell(item.project_label || baseName(item.project) || '全部', 'small', item.project || ''),
      cell(item.cwd || '—', 'mono small', item.cwd || ''),
      cell(item.status || '—', 'small'),
      cell(shortId(item.failed_turn_id), 'mono small', item.failed_turn_id || ''),
    );
    return tr;
  }));
  $('candidates-count').textContent = rows.length ? `${rows.length} 个` : '';
  $('candidates-empty').hidden = rows.length > 0;
}

function renderUnmonitored() {
  const status = (overview && overview.status) || {};
  const rows = status.unmonitored || [];
  const body = $('unmonitored-body');
  body.replaceChildren(...rows.map((item) => {
    const tr = document.createElement('tr');
    tr.append(
      cell(item.title || shortId(item.thread_id), 'small', item.thread_id || ''),
      cell(item.cwd || '—', 'mono small', item.cwd || ''),
      cell(item.status || '—', 'small'),
      cell(shortId(item.failed_turn_id), 'mono small', item.failed_turn_id || ''),
    );
    return tr;
  }));
  $('unmonitored-count').textContent = rows.length ? `${rows.length} 个` : '（0）';
}

function renderProjects() {
  const config = (overview && overview.config) || { projects: [] };
  const projects = config.projects || [];
  const box = $('projects');
  box.replaceChildren();

  const active = projects.filter((p) => p.enabled !== false).length;
  const count = $('config-count');
  count.textContent = projects.length ? `${active}/${projects.length}` : '';
  count.hidden = !projects.length;

  if (!projects.length) {
    box.append(make('p', '当前未限制项目，监控全部 thread。', 'muted'));
    return;
  }

  projects.forEach((project, index) => {
    const row = make('div', null, 'project-row' + (project.enabled === false ? ' disabled' : ''));

    const toggle = document.createElement('input');
    toggle.type = 'checkbox';
    toggle.checked = project.enabled !== false;
    toggle.title = '启用 / 停用';
    toggle.addEventListener('change', () => {
      saveProjects(projects.map((item, i) => i === index ? { ...item, enabled: toggle.checked } : { ...item }));
    });

    const info = make('div');
    info.append(make('div', project.path, 'path mono'));
    if (project.label) info.append(make('div', project.label, 'label'));

    const state = make('span', project.enabled === false ? '已停用' : '监控中',
                       'badge ' + (project.enabled === false ? '' : 'ok'));

    const remove = make('button', '删除', 'btn danger');
    remove.type = 'button';
    remove.addEventListener('click', () => {
      if (confirm(`删除监控项目 ${project.path}？`)) {
        saveProjects(projects.filter((_, i) => i !== index));
      }
    });

    row.append(toggle, info, state, remove);
    box.append(row);
  });
}

function renderAttempts() {
  const attempts = (overview && overview.attempts) || { recent: [], total: 0 };
  const rows = attempts.recent || [];
  const monitorAll = !!(overview && overview.status && overview.status.monitor_all);
  const body = $('attempts-body');
  body.replaceChildren(...rows.map((item) => {
    const tr = document.createElement('tr');
    const [label, kind] = ATTEMPT_STATE[item.state] || [item.state || '—', ''];
    const stateCell = document.createElement('td');
    stateCell.append(make('span', label, 'badge ' + kind));
    const who = make('div', item.title || shortId(item.thread_id));
    who.append(make('div', shortId(item.thread_id), 'mono muted'));
    const whoCell = document.createElement('td');
    whoCell.append(who);
    tr.append(
      cell(fmtTime(item.claimed_at), 'small'),
      whoCell,
      cell(item.project_label || baseName(item.project) || (monitorAll ? '全部' : '—'), 'small',
           item.project || ''),
      cell(item.cwd || '—', 'mono small', item.cwd || ''),
      stateCell,
      cell(shortId(item.created_turn_id), 'mono small', item.created_turn_id || ''),
      cell(item.details || '—', 'small', item.details || ''),
    );
    return tr;
  }));
  $('attempts-count').textContent = rows.length ? `最近 ${rows.length} 条 / 共 ${attempts.total || 0} 条` : '';
}

function render() {
  renderHeader();
  renderScan();
  renderWaiting();
  renderAttemptStats();
  renderQuota();
  renderCandidates();
  renderUnmonitored();
  renderProjects();
  renderAttempts();
}

/* ---------- data ---------- */

async function loadOverview() {
  overview = await api('/api/overview');
  render();
}

async function loadQuota(force) {
  try {
    liveQuota = await api(`/api/quota${force ? '?force=1' : ''}`, { timeout: 20000 });
    liveQuotaAt = Date.now();
  } catch (error) {
    liveQuota = { error: error.message, ordinaryUsageAllowed: null };
    liveQuotaAt = Date.now();
  }
  renderQuota();
}

async function loadAll({ quota = false } = {}) {
  try {
    await loadOverview();
  } catch (error) {
    toast(`刷新失败：${error.message}`, true);
  }
  if (quota) await loadQuota(false);
}

async function saveProjects(projects) {
  $('config-error').textContent = '';
  try {
    await api('/api/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ projects }),
    });
    toast('配置已保存，下一轮扫描生效');
    await loadOverview();
    return true;
  } catch (error) {
    $('config-error').textContent = error.message;
    toast(error.message, true);
    return false;
  }
}

/* ---------- events ---------- */

function setConfigOpen(open) {
  const panel = $('config-panel');
  const toggle = $('config-toggle');
  panel.hidden = !open;
  toggle.classList.toggle('active', open);
  toggle.setAttribute('aria-expanded', String(open));
  if (open) {
    const first = $('projects').querySelector('.project-row .path') || $('new-path');
    if (first) first.focus({ preventScroll: true });
  }
}

$('config-toggle').addEventListener('click', (event) => {
  event.stopPropagation();
  setConfigOpen($('config-panel').hidden);
});

document.addEventListener('click', (event) => {
  if ($('config-panel').hidden) return;
  if (event.target.closest('#config-panel')) return;
  setConfigOpen(false);
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !$('config-panel').hidden) setConfigOpen(false);
});

$('refresh').addEventListener('click', () => loadAll({ quota: true }));
$('quota-refresh').addEventListener('click', () => loadQuota(true));

$('add-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const path = $('new-path').value.trim();
  const label = $('new-label').value.trim();
  if (!path) {
    $('config-error').textContent = '请填写项目绝对路径';
    return;
  }
  const projects = ((overview && overview.config && overview.config.projects) || [])
    .map((item) => ({ path: item.path, label: item.label, enabled: item.enabled }));
  projects.push({ path, label, enabled: true });
  saveProjects(projects).then((saved) => {
    if (!saved) return;
    $('new-path').value = '';
    $('new-label').value = '';
  });
});

setInterval(() => {
  if (!document.hidden && $('auto').checked) loadAll();
}, REFRESH_MS);

setInterval(() => {
  if (!document.hidden && $('auto').checked) loadQuota(false);
}, QUOTA_MS);

loadAll({ quota: true });
