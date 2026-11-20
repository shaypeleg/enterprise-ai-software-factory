'use strict';

const session = location.hash.slice(1);
const el = (id) => document.getElementById(id);
const drawer = el('run-details');
const lanes = [
  ['intake', 'Intake', ['intake']],
  ['spec', 'Spec', ['spec']],
  ['build', 'Build', ['build', 'gate']],
  ['review', 'Review', ['review']],
  ['approval', 'Approval', ['handoff', 'ready']],
  ['human', 'Needs human', ['needs-info', 'needs-human']],
  ['done', 'Done', ['published', 'rejected']],
];
const stages = {intake: 'Intake', spec: 'Specification', build: 'Build', gate: 'Checks', review: 'Review',
  handoff: 'Handoff', ready: 'Ready to publish', published: 'Draft published', rejected: 'Rejected',
  'needs-human': 'Needs human review', 'needs-info': 'Needs information'};
const roles = {intake: 'Intake', spec: 'Specification', build: 'Build', gate: 'Checks', code: 'Code review',
  security: 'Security review', ux: 'UX review', ui: 'UI review', handoff: 'Handoff'};
const cardRoles = {intake: 'Intake', spec: 'Spec', build: 'Build', gate: 'Gate',
  code: 'Code', security: 'Security', ux: 'UX', ui: 'UI', handoff: 'Handoff'};
const events = {created: 'Run created', transition: 'Stage changed', 'base-pinned': 'Base commit pinned',
  'gate-pass': 'Checks passed', 'revision-required': 'Revision requested', published: 'Draft PR/MR published',
  resumed: 'Run resumed', 'expanded-review': 'Full pipeline requested',
  'commit-prepared': 'Candidate commit prepared', interrupted: 'Run interrupted',
  'unexpected-candidate-mutation': 'Candidate changed unexpectedly',
  'check-candidate-mutation': 'A check changed candidate files'};
const dateTime = new Intl.DateTimeFormat(undefined, {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit'});
const displayName = (name) => name === '.' ? 'Parent' : name;
const runKey = ({name, id, number}) => JSON.stringify([name, id ?? number]);
let repositories = [];
let selection = null;
let boardStamp = '';
let detailStamp = '';

function node(tag, text = '', className = '') {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

async function read(path) {
  const response = await fetch(path, {headers: {'X-Factory-Session': session}, signal: AbortSignal.timeout(8000)});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error);
  return payload;
}

function updateRepositories({project, repositories: list}) {
  repositories = list;
  el('project').textContent = project;
  const filter = el('repository');
  const selected = filter.value;
  const options = list.map(({name, status}) => [name, status === 'configured'
    ? displayName(name) : `${displayName(name)} · ${status.replaceAll('-', ' ')}`]);
  const before = Array.from(filter.options).slice(1).map((entry) => [entry.value, entry.textContent]);
  if (JSON.stringify(before) !== JSON.stringify(options)) {
    filter.replaceChildren(new Option('All repositories', ''), ...options.map(([key, label]) => new Option(label, key)));
    filter.value = options.some(([name]) => name === selected) ? selected : '';
  }
  el('repository-filter').hidden = options.length < 2;
}

function matchingRuns() {
  const filter = el('repository').value;
  return repositories.filter(({name}) => !filter || name === filter)
    .flatMap(({name, runs}) => runs.map((job) => ({name, ...job})))
    .sort((a, b) => String(b.updated || '').localeCompare(String(a.updated || '')) || b.number - a.number);
}

function stageLabel(job) {
  if (job.active_role) return `${roles[job.active_role]} running`;
  if (job.last_event === 'interrupted' || job.last_event?.startsWith('started-')) return 'Interrupted';
  return stages[job.stage] || job.stage;
}

function link(label, value) {
  if (!value) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.username || url.password) return null;
    const anchor = node('a', label);
    anchor.href = url.href;
    anchor.target = '_blank';
    anchor.rel = 'noopener noreferrer';
    return anchor;
  } catch { return null; }
}

function linksFor(job, full = false) {
  const links = node('p', '', 'card-links');
  const issue = link(full ? 'View issue' : 'Issue', job.issue_url);
  const request = link(full ? 'View draft PR/MR' : 'Draft PR/MR', job.request_url);
  if (issue) links.append(issue);
  if (request) links.append(request);
  return links.childElementCount ? links : null;
}

function chip(role, verdict, short = false) {
  const label = {running: 'In progress', 'not-run': 'Not run', 'NEEDS-INFO': 'Needs info', QUICK: 'Quick path'}[verdict] || verdict;
  const kind = ['PASS', 'READY', 'ACCEPT', 'QUICK'].includes(verdict) ? 'pass'
    : ['FAIL', 'CHANGES', 'BLOCKED', 'REJECT'].includes(verdict) ? 'fail'
    : {running: 'progress', 'not-run': 'not-run', 'NEEDS-INFO': 'needs-info'}[verdict] || '';
  const name = roles[role];
  const badge = node('span', short ? cardRoles[role] : `${name} · ${label}`, `chip ${kind}`.trim());
  if (short) {
    badge.title = `${name} · ${label}`;
    badge.append(node('span', ` · ${label}`, 'visually-hidden'));
  }
  return badge;
}

function roundTags(job) {
  const rows = node('div', '', 'card-rounds');
  for (let round = 1; round <= job.round; round++) {
    const recorded = job.rounds?.[round] || {};
    const active = round === job.round ? job.active_role : null;
    const row = node('div', '', 'card-round');
    row.setAttribute('role', 'group');
    row.setAttribute('aria-label', `Round ${round}`);
    const tags = node('div', '', 'verdicts');
    for (const role of Object.keys(cardRoles)) {
      const verdict = active === role ? 'running' : recorded[role]?.verdict;
      if (verdict || role === 'gate' || job.review_roles.includes(role)) {
        tags.append(chip(role, verdict || 'not-run', true));
      }
    }
    row.append(node('span', `R${round}`, 'round-label'), tags);
    rows.append(row);
  }
  return rows;
}

function elapsed(job) {
  const seconds = Math.floor(((job.active_role ? Date.now() : Date.parse(job.updated)) - Date.parse(job.started)) / 1000);
  if (!Number.isFinite(seconds)) return '';
  const total = Math.max(0, seconds);
  return total < 60 ? `${total}s` : total < 3600 ? `${Math.floor(total / 60)}m`
    : `${Math.floor(total / 3600)}h ${Math.floor(total / 60) % 60}m`;
}

function renderCard(job) {
  const item = node('li', '', 'run-card');
  item.dataset.active = String(Boolean(job.active_role));
  item.dataset.waiting = String(['needs-human', 'needs-info'].includes(job.stage));
  const button = node('button', '', 'run-open');
  button.type = 'button';
  button.dataset.run = runKey(job);
  button.setAttribute('aria-haspopup', 'dialog');
  const issueLabel = job.workspace_task ? `Parent #${job.number}` : `#${job.number}`;
  const identity = repositories.length > 1 ? `${displayName(job.name)} · ${issueLabel}` : issueLabel;
  button.append(node('span', identity, 'identity'), node('strong', job.title));
  button.addEventListener('click', () => {
    selection = job;
    detailStamp = '';
    el('run-identity').textContent = identity;
    el('run-title').textContent = job.title;
    el('detail').replaceChildren(node('p', 'Loading run details…', 'run-meta'));
    drawer.showModal();
    showSelected();
  });
  const meta = node('p', '', 'card-meta');
  meta.append(node('span', stageLabel(job), 'card-state'));
  if (job.risk) meta.append(node('span', `${job.risk} risk`));
  if (job.started) meta.append(node('span', elapsed(job), 'elapsed'));
  const links = linksFor(job);
  const rounds = roundTags(job);
  item.append(button, meta);
  if (links) item.append(links);
  item.append(rounds);
  return item;
}

function renderBoard() {
  const jobs = matchingRuns();
  if (selection) {
    selection = jobs.find((job) => runKey(job) === runKey(selection)) || null;
    if (!selection && drawer.open) drawer.close();
  }
  const stamp = JSON.stringify(jobs);
  if (stamp !== boardStamp) {
    boardStamp = stamp;
    const focusedKey = document.activeElement?.dataset.run;
    const columns = lanes.map(([key, label, states]) => {
      const lane = node('section', '', 'lane');
      lane.dataset.lane = key;
      const title = node('h2', label);
      title.id = `lane-${key}`;
      lane.setAttribute('aria-labelledby', title.id);
      const items = jobs.filter((job) => states.includes(job.stage));
      const heading = node('header', '', 'lane-heading');
      heading.append(title, node('span', String(items.length), 'lane-count'));
      const list = node('ul', '', 'cards');
      list.append(...items.map(renderCard));
      lane.append(heading, list);
      return lane;
    });
    el('board').replaceChildren(...columns);
    if (focusedKey) focusCard(focusedKey);
  } else {
    const active = new Map(jobs.filter((job) => job.active_role).map((job) => [runKey(job), job]));
    for (const card of el('board').querySelectorAll('[data-active="true"]')) {
      const job = active.get(card.querySelector('button').dataset.run);
      const time = card.querySelector('.elapsed');
      if (job && time) time.textContent = elapsed(job);
    }
  }
  el('count').textContent = `${jobs.length} ${jobs.length === 1 ? 'run' : 'runs'} · ${jobs.filter((job) => job.active_role).length} active`;
  el('empty').hidden = jobs.length > 0;
  const filter = el('repository').value;
  const visible = repositories.filter(({name}) => !filter || name === filter);
  el('empty').textContent = visible.some(({status}) => status === 'configured')
    ? 'No runs yet. Start a labeled issue with $factory run.'
    : 'No run data is available. Check your local settings or use $factory setup.';
}

function focusCard(key) {
  Array.from(el('board').querySelectorAll('button')).find((button) => button.dataset.run === key)?.focus();
}

function renderDetail({job, rounds, events: entries, agents = {}, planner_id: plannerId}) {
  const meta = node('p', [stageLabel(job), `Round ${job.round}`, job.risk && `${job.risk} risk`,
    job.profile === 'quick' && 'Quick path'].filter(Boolean).join(' · '), 'run-meta');
  const links = linksFor(job, true);
  const detail = el('detail');
  const openRounds = new Map(Array.from(detail.querySelectorAll('details')).map((entry) => [entry.dataset.round, entry.open]));
  const focusedRound = document.activeElement?.parentElement?.dataset.round;
  const scroll = detail.scrollTop;
  const evidence = node('section', '', 'evidence');
  evidence.append(node('h3', 'Checks and reviews by round'));
  const roundNumbers = [...new Set([...Object.keys(rounds), ...Object.keys(agents)])].sort((a, b) => b - a);
  for (const number of roundNumbers) {
    const reports = rounds[number] ?? {};
    const details = node('details');
    details.dataset.round = number;
    details.open = openRounds.get(number) ?? (number === String(job.round));
    details.append(node('summary', `Round ${number}`));
    const tags = node('div', '', 'verdicts');
    for (const role of Object.keys(roles)) {
      if (reports[role]) tags.append(chip(role, reports[role].verdict));
    }
    details.append(tags);
    for (const role of Object.keys(roles)) {
      const ids = agents[number]?.[role] ?? [];
      if (reports[role]?.report || ids.length) details.append(node('h4', roles[role]));
      if (ids.length) details.append(agentTrace('Agent IDs', ids));
      if (reports[role]?.report) details.append(node('pre', reports[role].report));
    }
    evidence.append(details);
  }
  if (!roundNumbers.length) evidence.append(node('p', 'Results appear here as each stage finishes.', 'run-meta'));
  const activity = node('section', '', 'evidence');
  activity.append(node('h3', 'Recent activity'));
  const history = node('ol', '', 'history');
  for (const {event, at, stage} of entries.slice(-12).reverse()) {
    let label = events[event] || event;
    if (event.startsWith('stage-')) label = `${roles[event.slice(6)] || stage} recorded`;
    if (event.startsWith('started-')) label = `${roles[event.slice(8)] || stage} started`;
    if (event.startsWith('agent-')) label = `${roles[event.slice(6)] || stage} agent recorded`;
    const row = node('li');
    const date = new Date(at);
    const time = node('time', Number.isNaN(date.getTime()) ? '' : dateTime.format(date));
    time.dateTime = at;
    row.append(node('span', label), time);
    history.append(row);
  }
  activity.append(history);
  detail.replaceChildren(meta, ...(links ? [links] : []),
    ...(plannerId ? [agentTrace('Planning agent', [plannerId])] : []), evidence, activity);
  if (focusedRound) {
    Array.from(detail.querySelectorAll('details')).find((entry) => entry.dataset.round === focusedRound)
      ?.querySelector('summary')?.focus({preventScroll: true});
  }
  detail.scrollTop = scroll;
}

function agentTrace(label, ids) {
  const trace = node('p', label, 'agent-trace run-meta');
  for (const id of ids) trace.append(node('code', id));
  return trace;
}

async function showSelected() {
  const target = selection;
  if (!target || !drawer.open) return;
  const stamp = JSON.stringify([runKey(target), target.updated, target.active_role]);
  if (stamp === detailStamp) return;
  const params = new URLSearchParams({repository: target.name, issue: String(target.id ?? target.number)});
  try {
    const payload = await read(`/api/run?${params}`);
    if (selection !== target || !drawer.open) return;
    detailStamp = stamp;
    renderDetail(payload);
  } catch (error) {
    if (selection === target && drawer.open) el('detail').replaceChildren(node('p', error.message, 'notice'));
  }
}

function connectionStatus(text, cssClass) {
  const status = el('connection');
  if (status.textContent !== text) status.textContent = text;
  status.className = `connection ${cssClass}`.trim();
}

async function refresh() {
  try {
    updateRepositories(await read('/api/runs'));
    renderBoard();
    connectionStatus('Live · local', 'is-live');
    el('message').hidden = true;
    await showSelected();
  } catch (_) {
    connectionStatus('Disconnected', 'stale');
    el('message').textContent = 'The dashboard stopped responding. Keep its command running and reopen the private URL.';
    el('message').hidden = false;
    if (!repositories.length) el('empty').hidden = true;
  } finally {
    setTimeout(refresh, 2000);
  }
}

el('repository').addEventListener('change', renderBoard);
drawer.addEventListener('close', () => {
  if (selection) focusCard(runKey(selection));
  selection = null;
});
if (session) refresh();
else {
  connectionStatus('Private link required', '');
  el('empty').textContent = 'Open the private dashboard URL printed by $factory dashboard.';
}
