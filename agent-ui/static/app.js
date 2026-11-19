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
let reviewDiffKey = null;
let activeUpgradeTask = null;

const labels = {
  queued: '排队中', running: '执行中', review: '待人工验收', applying: '验收合入中', succeeded: '验收通过', failed: '失败', cancelled: '已停止', paused: '已暂停', waiting_owner: '等待你决定', budget_exhausted: '预算已用完', no_change: '本轮无改动', discarded: '本轮已放弃'
};
const reasoningLabels = {default: '默认', low: '低', medium: '中', high: '高'};
const kinds = {status: '状态', assistant: 'AI 助手', tool: '工具调用', tool_result: '执行结果', artifact: '文件改动', review: '人工验收', research: '联网调研', error: '错误'};

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

function parseLogJson(text) {
  try { return JSON.parse(text); } catch { return null; }
}

function testResultText(result) {
  if (result?.timed_out || !Number.isInteger(result?.exit_code)) return '';
  const stdout = result.stdout || '';
  const stderr = result.stderr || '';
  if (/^TAP version \d+\r?$/m.test(stdout)) {
    const count = label => Number(stdout.match(new RegExp(`^# ${label} (\\d+)\\r?$`, 'm'))?.[1]);
    const [total, passed, failed, skipped] = ['tests', 'pass', 'fail', 'skipped'].map(count);
    if (Number.isFinite(total) && Number.isFinite(passed) && Number.isFinite(failed)) {
      const parts = [`${passed} 项通过`, `${failed} 项失败`];
      if (skipped > 0) parts.push(`${skipped} 项跳过`);
      if (total === 0) return '没有发现可运行的测试，不能据此验收。';
      const warning = result.exit_code === 0 ? '' : ` 命令未成功结束（退出码 ${result.exit_code}）。`;
      return `本次测试：${parts.join('、')}（共报告 ${total} 项）。${warning}`;
    }
  }
  const ran = stderr.match(/^Ran (\d+) tests? in [\d.]+s\r?$/m);
  if (ran && /^[-]{20,}\r?\nRan /m.test(stderr)) {
    const summary = stderr.match(/^(OK|FAILED)(?: \(([^\r\n]*)\))?\r?$/m);
    if (summary && Number(ran[1]) > 0) {
      if (summary[1] === 'OK' && !summary[2] && result.exit_code === 0) {
        return `本次测试：${ran[1]} 项通过、0 项失败。`;
      }
      return `运行了 ${ran[1]} 项测试。${summary[1] === 'FAILED' || result.exit_code !== 0 ? '测试命令未通过。' : '有未计为通过的项目，请展开查看详情。'}`;
    }
  }
  return '';
}

function explainEvent(item, result, testText) {
  if (item.kind === 'tool') {
    const action = item.content.match(/^工具 (\w+)：/);
    if (!action) return 'Agent 发起了一次工具操作，还要看后面的执行结果。';
    const args = parseLogJson(item.content.slice(action[0].length));
    const path = typeof args?.path === 'string' && args.path ? `：${args.path}` : '';
    switch (action[1]) {
      case 'list_files': return `Agent 准备查看工作区有哪些文件${path}。`;
      case 'read_file': return `Agent 准备读取文件${path}，不会修改文件。`;
      case 'write_file': return `Agent 准备写入文件${path}，是否写入成功要看后面的记录。`;
      case 'run_command': {
        const command = typeof args?.command === 'string' ? args.command.split('\n')[0] : '';
        return `Agent 准备在隔离环境执行命令${command ? `：${command}` : ''}。`;
      }
      default: return 'Agent 发起了一次工具操作，还要看后面的执行结果。';
    }
  }
  if (item.kind === 'error' && /ReadTimeout/.test(item.content)) {
    return '等模型回复超时了。这条错误不代表你的指令填错，也不是测试失败。';
  }
  if (item.kind !== 'tool_result') return '';
  if (!result) return '这条结果无法自动解读，请展开查看原始记录。';
  if (result.timed_out) return '命令没有在规定时间内跑完，不能当作通过。';
  if (testText) return testText;
  if (Number.isInteger(result.exit_code)) {
    return result.exit_code === 0 ? '命令正常结束。这条记录没有可识别的测试统计。' : `命令未成功结束（退出码 ${result.exit_code}），请展开查看原因。`;
  }
  if (result.ok === false) return '工具没有完成操作，请展开查看原因。';
  if (typeof result.written === 'string' && result.ok) return `已将改动保存到工作区：${result.written}。`;
  if (Array.isArray(result.files) && result.ok) return `找到了 ${result.files.length} 个文件，这一步没有修改文件。`;
  if (typeof result.content === 'string' && result.ok) return '文件已读取，没有修改；内容可在原始记录中查看。';
  return '已收到工具结果，请展开查看详情。';
}

function reviewNotes(output) {
  const block = output?.match(/^已验证的规则[：:]\s*\n((?:[ \t]*- [^\r\n]+\r?\n?)+)/m)?.[1];
  return (block || '').trim().split(/\r?\n/).filter(Boolean)
    .map(line => line.trim().replace(/^- /, '').replace(/\*\*/g, ''));
}

function renderEvents(events, output) {
  const previousScrollTop = eventsNode.scrollTop;
  const wasAtBottom = eventsNode.scrollHeight - eventsNode.clientHeight - previousScrollTop <= 48;
  const expanded = new Set([...eventsNode.querySelectorAll('details[open]')].map(node => node.dataset.eventId));
  const records = events.map(item => {
    const result = item.kind === 'tool_result' ? parseLogJson(item.content) : null;
    return {item, result, testText: testResultText(result)};
  });
  const lastTest = records.findLast(record => record.testText);
  const notes = reviewNotes(output);
  eventsNode.replaceChildren();
  if (!events.length) {
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    empty.textContent = '等待 Agent 开始…';
    eventsNode.append(empty);
    eventsNode.scrollTop = wasAtBottom ? eventsNode.scrollHeight : previousScrollTop;
    return;
  }
  for (const record of records) {
    const {item, result, testText} = record;
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
    card.append(top);
    const meaning = explainEvent(item, result, testText);
    if (meaning) {
      const summary = document.createElement('p');
      summary.className = 'event-meaning';
      summary.textContent = `实际意思：${meaning}`;
      card.append(summary);
      if (record === lastTest && notes.length) {
        const note = document.createElement('div');
        note.className = 'event-review-notes';
        note.textContent = 'Agent 对验收项的说明（自述，供对照）：';
        const list = document.createElement('ul');
        for (const text of notes) {
          const bullet = document.createElement('li');
          bullet.textContent = text;
          list.append(bullet);
        }
        note.append(list);
        card.append(note);
      }
      const raw = document.createElement('details');
      raw.className = 'event-raw';
      raw.dataset.eventId = item.id;
      raw.open = expanded.has(String(item.id));
      const toggle = document.createElement('summary');
      toggle.textContent = '查看原始记录';
      raw.append(toggle, content);
      card.append(raw);
    } else {
      card.append(content);
    }
    eventsNode.append(card);
  }
  eventsNode.scrollTop = wasAtBottom ? eventsNode.scrollHeight : previousScrollTop;
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

async function loadCandidateChanges(task) {
  const key = `${task.id}:${task.updated_at}`;
  if (reviewDiffKey === key) return;
  reviewDiffKey = key;
  const container = document.querySelector('#candidate-changes');
  const approveButton = document.querySelector('#approve-task');
  const fullDiffAckRow = document.querySelector('#full-diff-ack-row');
  const fullDiffAck = document.querySelector('#full-diff-ack');
  approveButton.disabled = true;
  fullDiffAck.checked = false;
  fullDiffAckRow.hidden = true;
  container.replaceChildren();
  const loading = document.createElement('p');
  loading.className = 'diff-empty';
  loading.textContent = '正在读取候选文件差异……';
  container.append(loading);
  try {
    const data = await api(`/api/tasks/${task.id}/changes`);
    if (reviewDiffKey !== key) return;
    container.replaceChildren();
    if (!data.changes.length) {
      const empty = document.createElement('p');
      empty.className = 'diff-empty';
      empty.textContent = '没有文件改动；本次只有文字总结。';
      container.append(empty);
    }
    const hasTruncatedDiff = data.changes.some(change => change.truncated);
    fullDiffAckRow.hidden = !hasTruncatedDiff;
    approveButton.disabled = hasTruncatedDiff;
    for (const change of data.changes) {
      const section = document.createElement('details');
      section.className = 'diff-file';
      const summary = document.createElement('summary');
      const title = document.createElement('span');
      title.textContent = change.path;
      const kind = document.createElement('span');
      kind.className = `diff-kind diff-${change.kind}`;
      kind.textContent = ({added: '新增', modified: '修改', deleted: '删除'})[change.kind] || change.kind;
      summary.append(title, kind);
      const content = [];
      if (change.truncated) {
        const notice = document.createElement('p');
        notice.className = 'diff-truncated';
        notice.textContent = '当前仅显示差异预览，内容已截断；下载完整差异检查后再验收。';
        const download = document.createElement('a');
        download.className = 'diff-download';
        download.href = `/api/tasks/${task.id}/changes/full?path=${encodeURIComponent(change.path)}`;
        download.textContent = '下载完整差异';
        download.setAttribute('download', 'candidate.diff');
        content.push(notice, download);
      }
      const pre = document.createElement('pre');
      pre.textContent = change.diff;
      content.push(pre);
      section.append(summary, ...content);
      container.append(section);
    }
  } catch (error) {
    if (reviewDiffKey !== key) return;
    container.replaceChildren();
    const failed = document.createElement('p');
    failed.className = 'diff-error';
    failed.textContent = `差异读取失败：${error.message}`;
    container.append(failed);
  }
}

async function loadTaskDetail(id) {
  const task = await api(`/api/tasks/${id}`);
  document.querySelector('#detail-empty').hidden = true;
  const detail = document.querySelector('#detail');
  detail.hidden = false;
  document.querySelector('#detail-title').textContent = task.title;
  document.querySelector('#detail-status').textContent = `${labels[task.status] || task.status} · 推理强度：${reasoningLabels[task.reasoning_effort] || reasoningLabels.default} · 创建于 ${fmtTime(task.created_at)}`;
  document.querySelector('#task-goal').textContent = task.prompt || '';
  document.querySelector('#task-criteria').textContent = task.acceptance_criteria || '按任务说明完成并验证。';
  const contextSection = document.querySelector('#task-context-section');
  contextSection.hidden = !task.context;
  document.querySelector('#task-context').textContent = task.context || '';
  const output = document.querySelector('#task-output');
  output.hidden = !task.output;
  output.textContent = task.output || '';
  const outputTitle = document.querySelector('#task-output-title');
  outputTitle.hidden = !task.output;
  outputTitle.textContent = task.status === 'review' ? 'Agent 候选结果（未验收）' : 'Agent 执行总结';
  const cancel = document.querySelector('#cancel-task');
  cancel.hidden = task.task_type === 'upgrade' || !['queued', 'running'].includes(task.status);
  const upgradePanel = document.querySelector('#upgrade-detail');
  upgradePanel.hidden = task.task_type !== 'upgrade';
  activeUpgradeTask = task.task_type === 'upgrade' ? task : null;
  const reviewPanel = document.querySelector('#review-panel');
  const canInspectUpgrade = task.task_type === 'upgrade' && ['paused', 'failed', 'budget_exhausted'].includes(task.status);
  const canInspect = task.status === 'review' || canInspectUpgrade;
  reviewPanel.hidden = !canInspect;
  reviewPanel.querySelector('h3').textContent = task.status === 'review' ? '人工验收' : '候选差异（只读）';
  reviewPanel.querySelector('p').textContent = task.status === 'review'
    ? '候选改动尚未进入正式工作区。先检查差异和验证记录，再通过或退回。'
    : '本轮未完成验收，仅供检查部分候选差异；不会合入正式工作区。';
  document.querySelector('#review-note').hidden = task.status !== 'review';
  document.querySelector('#request-changes').hidden = task.status !== 'review';
  document.querySelector('#approve-task').hidden = task.status !== 'review';
  if (canInspect) loadCandidateChanges(task);
  else { reviewDiffKey = null; document.querySelector('#candidate-changes').replaceChildren(); }
  if (task.task_type === 'upgrade') renderUpgradeDetail(task);
  renderEvents(task.events || [], task.output);
}

function renderUpgradeDetail(task) {
  const upgrade = task.upgrade || {};
  const phaseLabels = {queued: '排队中', inspection: '检查项目', research: '联网调研', decision: '选择方向', implementation: '隔离实现', testing: '运行测试', review: '待你验收', paused: '已暂停', waiting_owner: '等待你决定', unknown_model_call: '模型请求结果未知', budget_exhausted: '预算触顶', no_change: '本轮无改动', base_conflict: '工作区基线冲突'};
  const usageLabels = {actual: '服务商返回值', estimated: '估算值', mixed: '实际值与估算值混合', unknown: '暂不可知'};
  document.querySelector('#upgrade-summary').textContent = [
    '阶段：' + (phaseLabels[task.phase] || task.phase || labels[task.status] || task.status),
    '模型调用：' + (upgrade.model_calls || 0) + ' / ' + (upgrade.budgets?.max_model_calls || '—'),
    '联网搜索：' + (upgrade.search_calls || 0) + ' / ' + (upgrade.budgets?.max_search_calls || '—'),
    'Token：' + (upgrade.budget_tokens_used || 0) + ' / ' + (upgrade.budget_limit_tokens || 0) + '（' + (usageLabels[upgrade.usage_quality] || '暂不可知') + '）',
    '授权：只改隔离候选；不合并、不 Push、不部署。'
  ].join(' · ');
  const owner = upgrade.owner_request;
  const ownerNode = document.querySelector('#owner-request');
  ownerNode.hidden = !owner;
  ownerNode.textContent = owner ? 'Agent 请求批准：' + owner.action + '\n原因：' + owner.reason + '\n批准只对本轮候选区内该动作有效，不增加工具能力。' : '';
  const sourcesNode = document.querySelector('#upgrade-sources');
  sourcesNode.replaceChildren();
  if (!task.sources?.length) {
    const empty = document.createElement('p');
    empty.className = 'muted small';
    empty.textContent = '本轮还没有已保存来源。';
    sourcesNode.append(empty);
  } else {
    for (const source of task.sources) {
      const card = document.createElement('article');
      card.className = 'upgrade-source';
      const link = document.createElement('a');
      link.textContent = source.title;
      try {
        const parsed = new URL(source.url);
        if (parsed.protocol === 'https:') { link.href = parsed.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; }
        else link.textContent += '（无效链接）';
      } catch { link.textContent += '（无效链接）'; }
      const snippet = document.createElement('p');
      snippet.textContent = source.snippet;
      card.append(link, snippet);
      sourcesNode.append(card);
    }
  }
  document.querySelector('#pause-upgrade').hidden = !['queued', 'running'].includes(task.status);
  document.querySelector('#resume-upgrade').hidden = !['paused', 'failed', 'waiting_owner'].includes(task.status) || Boolean(owner);
  document.querySelector('#authorize-upgrade').hidden = !owner || task.status !== 'waiting_owner';
  document.querySelector('#discard-upgrade').hidden = ['succeeded', 'applying', 'discarded'].includes(task.status);
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
    document.querySelector('#upgrade-search-warning').hidden = status.search_configured;
    document.querySelector('#start-upgrade').disabled = !status.api_configured || !status.search_configured;
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

const goalField = document.querySelector('#goal');
goalField.addEventListener('input', () => {
  document.querySelector('#char-count').textContent = `${goalField.value.length} / 8000`;
});

document.querySelector('#task-form').addEventListener('submit', async event => {
  event.preventDefault();
  taskError.textContent = '';
  const button = document.querySelector('#submit-task');
  button.disabled = true;
  try {
    const result = await api('/api/tasks', {
      method: 'POST',
      body: JSON.stringify({
        goal: goalField.value,
        acceptance_criteria: document.querySelector('#acceptance-criteria').value,
        context: document.querySelector('#task-context-input').value,
        reasoning_effort: document.querySelector('#reasoning-effort').value,
      }),
    });
    goalField.value = '';
    document.querySelector('#acceptance-criteria').value = '';
    document.querySelector('#task-context-input').value = '';
    document.querySelector('#char-count').textContent = '0 / 8000';
    activeTaskId = result.id;
    await loadTasks();
    await selectTask(result.id);
  } catch (error) { taskError.textContent = error.message; }
  finally { button.disabled = false; }
});

document.querySelector('#upgrade-form').addEventListener('submit', async event => {
  event.preventDefault();
  const errorNode = document.querySelector('#upgrade-error');
  errorNode.textContent = '';
  const button = document.querySelector('#start-upgrade');
  button.disabled = true;
  try {
    const result = await api('/api/upgrades', {
      method: 'POST',
      body: JSON.stringify({
        goal: document.querySelector('#upgrade-goal').value,
        budget_tokens: Number(document.querySelector('#upgrade-budget').value),
        max_model_calls: Number(document.querySelector('#upgrade-model-calls').value),
        max_search_calls: Number(document.querySelector('#upgrade-search-calls').value),
        reasoning_effort: document.querySelector('#upgrade-reasoning').value,
      }),
    });
    activeTaskId = result.id;
    await loadTasks();
    await selectTask(result.id);
  } catch (error) { errorNode.textContent = error.message; }
  finally { button.disabled = false; }
});

async function runUpgradeControl(path, body = {}) {
  if (!activeUpgradeTask) return;
  try {
    await api('/api/tasks/' + activeUpgradeTask.id + '/' + path, {method: 'POST', body: JSON.stringify(body)});
    await loadTasks();
  } catch (error) { taskError.textContent = error.message; }
}

document.querySelector('#pause-upgrade').addEventListener('click', () => runUpgradeControl('pause'));
document.querySelector('#resume-upgrade').addEventListener('click', () => {
  if (!activeUpgradeTask) return;
  const unknown = Boolean(activeUpgradeTask.unknown_model_call);
  if (unknown && !confirm('上一次模型请求没有可确认的响应，服务商可能已计费。确认你接受可能重复计费并重试？')) return;
  runUpgradeControl('resume', {confirm_unknown_model_call: unknown});
});
document.querySelector('#authorize-upgrade').addEventListener('click', () => {
  const action = activeUpgradeTask?.upgrade?.owner_request?.action;
  if (!action || !confirm('只批准本轮隔离候选区内执行此动作：\n\n' + action + '\n\n不会授权读密钥、合并、Push 或部署。继续？')) return;
  runUpgradeControl('authorize', {action});
});
document.querySelector('#discard-upgrade').addEventListener('click', () => {
  if (!activeUpgradeTask || !confirm('放弃并清理这一次升级的候选文件与检查点？正式工作区不会受影响。')) return;
  runUpgradeControl('discard');
});

document.querySelector('#refresh').addEventListener('click', loadTasks);
document.querySelector('#cancel-task').addEventListener('click', async () => {
  if (!activeTaskId) return;
  try { await api(`/api/tasks/${activeTaskId}/cancel`, {method: 'POST'}); await loadTasks(); }
  catch (error) { taskError.textContent = error.message; }
});

document.querySelector('#full-diff-ack').addEventListener('change', event => {
  document.querySelector('#approve-task').disabled = !event.currentTarget.checked;
});

document.querySelector('#approve-task').addEventListener('click', async () => {
  if (!activeTaskId) return;
  const button = document.querySelector('#approve-task');
  button.disabled = true;
  try {
    await api(`/api/tasks/${activeTaskId}/approve`, {
      method: 'POST',
      body: JSON.stringify({full_diff_reviewed: document.querySelector('#full-diff-ack').checked}),
    });
    await loadTasks();
  } catch (error) { taskError.textContent = error.message; }
  finally { button.disabled = false; }
});

document.querySelector('#request-changes').addEventListener('click', async () => {
  if (!activeTaskId) return;
  const note = document.querySelector('#review-note').value.trim();
  if (!note) {
    taskError.textContent = '请先写明需要修改的地方。';
    return;
  }
  const button = document.querySelector('#request-changes');
  button.disabled = true;
  try {
    await api(`/api/tasks/${activeTaskId}/request-changes`, {method: 'POST', body: JSON.stringify({note})});
    document.querySelector('#review-note').value = '';
    await loadTasks();
  } catch (error) { taskError.textContent = error.message; }
  finally { button.disabled = false; }
});

(async function boot() {
  try {
    const response = await fetch('/api/session', {credentials: 'same-origin'});
    const session = await response.json();
    if (session.authenticated) await enterWorkspace();
    else showLogin();
  } catch (_) { showLogin(); }
})();
