const loginPanel = document.querySelector('#login-panel');
const workspacePanel = document.querySelector('#workspace-panel');
const topActions = document.querySelector('#top-actions');
const loginError = document.querySelector('#login-error');
const taskError = document.querySelector('#task-error');
const tasksNode = document.querySelector('#tasks');
const eventsNode = document.querySelector('#events');
let activeTaskId = null;
let pollTimer = null;
let knownTasks = [];

const labels = {
  queued: '排队中', running: '执行中', succeeded: '已完成', failed: '失败', cancelled: '已停止'
};
const kinds = {status: '状态', assistant: 'AI 助手', tool: '工具调用', tool_result: '执行结果', error: '错误'};

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json', ...(options.headers || {})},
    ...options,
  });
  if (response.status === 401 && path !== '/api/login') {
    showLogin();
    throw new Error('登录已过期，请重新登录');
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `请求失败（${response.status}）`);
  return data;
}

function showLogin() {
  loginPanel.hidden = false;
  workspacePanel.hidden = true;
  topActions.hidden = true;
}

function showWorkspace() {
  loginPanel.hidden = true;
  workspacePanel.hidden = false;
  topActions.hidden = false;
}

function escapeText(value) {
  return String(value ?? '');
}

function fmtTime(seconds) {
  if (!seconds) return '';
  return new Date(seconds * 1000).toLocaleString('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'});
}

function renderTasks(tasks) {
  knownTasks = tasks;
  tasksNode.replaceChildren();
  if (!tasks.length) {
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    empty.textContent = '还没有任务，先提交一个试试。';
    tasksNode.append(empty);
    return;
  }
  for (const task of tasks) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `task-item${task.id === activeTaskId ? ' selected' : ''}`;
    const title = document.createElement('div');
    title.className = 'task-item-title';
    title.textContent = escapeText(task.title);
    const meta = document.createElement('div');
    meta.className = 'task-item-meta';
    const status = document.createElement('span');
    status.className = `status-pill status-${task.status}`;
    status.textContent = labels[task.status] || task.status;
    const date = document.createElement('time');
    date.textContent = fmtTime(task.created_at);
    meta.append(status, date);
    button.append(title, meta);
    button.addEventListener('click', () => selectTask(task.id));
    tasksNode.append(button);
  }
}

function renderEvents(events) {
  eventsNode.replaceChildren();
  if (!events.length) {
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    empty.textContent = '等待 Agent 开始…';
    eventsNode.append(empty);
    return;
  }
  for (const item of events) {
    const card = document.createElement('article');
    card.className = `event${item.kind === 'error' ? ' event-error' : ''}${item.kind === 'tool' ? ' event-tool' : ''}`;
    const top = document.createElement('div');
    top.className = 'event-top';
    const kind = document.createElement('span');
    kind.className = 'event-kind';
    kind.textContent = kinds[item.kind] || item.kind;
    const date = document.createElement('time');
    date.textContent = fmtTime(item.created_at);
    const content = document.createElement('div');
    content.className = 'event-content';
    content.textContent = escapeText(item.content);
    top.append(kind, date);
    card.append(top, content);
    eventsNode.append(card);
  }
  eventsNode.scrollTop = eventsNode.scrollHeight;
}

async function loadTasks() {
  try {
    const tasks = await api('/api/tasks');
    renderTasks(tasks);
    if (activeTaskId && tasks.some(task => task.id === activeTaskId)) await loadTaskDetail(activeTaskId);
  } catch (error) {
    if (!loginPanel.hidden) return;
    taskError.textContent = error.message;
  }
}

async function loadTaskDetail(id) {
  const task = await api(`/api/tasks/${id}`);
  document.querySelector('#detail-empty').hidden = true;
  const detail = document.querySelector('#detail');
  detail.hidden = false;
  document.querySelector('#detail-title').textContent = task.title;
  document.querySelector('#detail-status').textContent = `${labels[task.status] || task.status} · 创建于 ${fmtTime(task.created_at)}`;
  const output = document.querySelector('#task-output');
  output.hidden = !task.output;
  output.textContent = task.output || '';
  const cancel = document.querySelector('#cancel-task');
  cancel.hidden = !['queued', 'running'].includes(task.status);
  renderEvents(task.events || []);
}

async function selectTask(id) {
  activeTaskId = id;
  renderTasks(knownTasks);
  try { await loadTaskDetail(id); } catch (error) { taskError.textContent = error.message; }
}

async function enterWorkspace() {
  showWorkspace();
  try {
    const status = await api('/api/status');
    const badge = document.querySelector('#model-badge');
    badge.textContent = status.api_configured ? `模型：${status.model}` : '模型 API 未配置';
    badge.classList.toggle('badge-warn', !status.api_configured);
    document.querySelector('#config-warning').hidden = status.api_configured;
    await loadTasks();
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(loadTasks, 2500);
  } catch (error) {
    showLogin();
    loginError.textContent = error.message;
  }
}

document.querySelector('#login-form').addEventListener('submit', async event => {
  event.preventDefault();
  loginError.textContent = '';
  const button = event.currentTarget.querySelector('button');
  button.disabled = true;
  try {
    await api('/api/login', {method: 'POST', body: JSON.stringify({password: document.querySelector('#password').value})});
    document.querySelector('#password').value = '';
    await enterWorkspace();
  } catch (error) {
    loginError.textContent = error.message;
  } finally { button.disabled = false; }
});

document.querySelector('#logout').addEventListener('click', async () => {
  try { await api('/api/logout', {method: 'POST'}); } catch (_) {}
  if (pollTimer) clearInterval(pollTimer);
  showLogin();
});

const promptField = document.querySelector('#prompt');
promptField.addEventListener('input', () => {
  document.querySelector('#char-count').textContent = `${promptField.value.length} / 8000`;
});

document.querySelector('#task-form').addEventListener('submit', async event => {
  event.preventDefault();
  taskError.textContent = '';
  const button = document.querySelector('#submit-task');
  button.disabled = true;
  try {
    const result = await api('/api/tasks', {method: 'POST', body: JSON.stringify({prompt: promptField.value})});
    promptField.value = '';
    document.querySelector('#char-count').textContent = '0 / 8000';
    activeTaskId = result.id;
    await loadTasks();
    await selectTask(result.id);
  } catch (error) { taskError.textContent = error.message; }
  finally { button.disabled = false; }
});

document.querySelector('#refresh').addEventListener('click', loadTasks);
document.querySelector('#cancel-task').addEventListener('click', async () => {
  if (!activeTaskId) return;
  try { await api(`/api/tasks/${activeTaskId}/cancel`, {method: 'POST'}); await loadTasks(); }
  catch (error) { taskError.textContent = error.message; }
});

(async function boot() {
  try {
    const response = await fetch('/api/session', {credentials: 'same-origin'});
    const session = await response.json();
    if (session.authenticated) await enterWorkspace();
    else showLogin();
  } catch (_) { showLogin(); }
})();
