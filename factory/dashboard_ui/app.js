'use strict';

const session = location.hash.slice(1);
const el = (id) => document.getElementById(id);
const stages = {intake: 'Intake', spec: 'Specification', build: 'Build', gate: 'Checks', review: 'Reviews',
  handoff: 'Handoff', ready: 'Ready to publish', published: 'Draft published', rejected: 'Rejected',
  'needs-human': 'Needs human review', 'needs-info': 'Needs more information'};
const roles = {intake: 'Intake', spec: 'Specification', build: 'Build', gate: 'Checks', code: 'Code review',
  security: 'Security review', ux: 'UX review', ui: 'UI review', handoff: 'Handoff'};
const workflow = ['intake', 'spec', 'build', 'gate', 'review', 'handoff'];
const events = {created: 'Run created', transition: 'Stage changed', 'base-pinned': 'Base commit pinned',
  'gate-pass': 'Checks passed', 'revision-required': 'Revision requested', published: 'Draft PR/MR published',
  'commit-prepared': 'Candidate commit prepared', interrupted: 'Run interrupted',
  'unexpected-candidate-mutation': 'Candidate changed unexpectedly'};
const dateTime = new Intl.DateTimeFormat(undefined, {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit'});
let repositories = [];
let current = null;
let selection = null;
let displayed = '';
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

function statusDetails(payload) {
  el('project').textContent = payload.project;
  const filter = el('repository');
  const selected = filter.value;
  const options = payload.repositories.map(({name, status}) => [name, `${displayName(name)} · ${status.replaceAll('-', ' ')}`]);
  if (JSON.stringify(Array.from(filter.options).slice(1).map((entry) => [entry.value, entry.textContent])) !== JSON.stringify(options)) {
    filter.replaceChildren(new Option('All repositories', ''), ...options.map(([key, label]) => new Option(label, key)));
    filter.value = options.some(([name]) => name === selected) ? selected : '';
  }
  el('repository-filter').hidden = options.length < 2;
}

const displayName = (name) => name === '.' ? 'Parent' : name;

function matchingRuns() {
  const filter = el('repository').value;
  return repositories.filter(({name}) => !filter || name === filter)
    .flatMap(({name, runs}) => runs.map((job) => ({name, ...job})))
    .sort((a, b) => String(b.updated || '').localeCompare(String(a.updated || '')) || b.number - a.number);
}

function sameRun(job, other) {
  return Boolean(job && other && job.name === other.name && job.number === other.number);
}

function renderList() {
  const jobs = matchingRuns();
  const next = jobs.find((job) => sameRun(job, selection)) || jobs[0] || null;
  if (!sameRun(next, selection)) detailStamp = '';
  selection = next;
  const signature = JSON.stringify([jobs, selection?.name, selection?.number]);
  if (signature !== displayed) {
    displayed = signature;
    const focusedKey = document.activeElement?.dataset.run;
    const rows = jobs.map((job) => {
      const item = node('li');
      const button = node('button', '', 'run-link');
      button.type = 'button';
      button.dataset.run = JSON.stringify([job.name, job.number]);
      button.setAttribute('aria-pressed', String(sameRun(job, selection)));
      button.append(node('small', `${displayName(job.name)} · #${job.number}`), node('strong', job.title),
        node('span', `${stageLabel(job)} · Round ${job.round}`, 'run-meta'));
      button.addEventListener('click', () => {
        selection = job;
        renderList();
        showSelected(true);
      });
      item.append(button);
      return item;
    });
    el('runs').replaceChildren(...rows);
    if (focusedKey) Array.from(el('runs').querySelectorAll('button')).find((item) => item.dataset.run === focusedKey)?.focus();
  }
  el('count').textContent = String(jobs.length);
  el('empty').hidden = jobs.length > 0;
  const configured = repositories.some((repo) => repo.status === 'configured');
  el('empty').textContent = configured
    ? 'No recorded runs in this view. Start a labeled issue with $factory run, or use $factory workspace run for several repositories.'
    : 'No accessible run records. Configure a checkout with $factory setup or check your local state.';
  if (!selection) {
    el('run-title').textContent = 'Run details';
    el('detail').replaceChildren(node('p', 'Stage progress and review results will appear here as jobs run.', 'muted'));
  }
}

function stageLabel(job) {
  return job.last_event === 'interrupted' ? `Interrupted at ${stages[job.stage] || job.stage}` : stages[job.stage] || job.stage;
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

function formatTime(date) {
  if (!date) return '';
  const value = new Date(date);
  return Number.isNaN(value.getTime()) ? '' : dateTime.format(value);
}

function renderDetail(payload, repo) {
  const {job, events: entries, rounds, started} = payload;
  el('run-title').textContent = job.title;
  const time = Math.round((Date.parse(job.updated) - Date.parse(started)) / 1000);
  const duration = Number.isFinite(time) && time >= 0
    ? ` · ${Math.floor(time / 60) ? `${Math.floor(time / 60)}m ` : ''}${time % 60}s to last update` : '';
  const meta = node('p', `${displayName(repo)} · #${job.number} · ${stageLabel(job)} · Round ${job.round}${job.risk ? ` · ${job.risk} risk` : ''}${duration}`, 'run-meta');
  const links = node('p', '', 'links');
  for (const anchor of [link('View issue', job.issue_url), link('View draft PR/MR', job.request_url)]) {
    if (anchor) links.append(anchor);
  }
  const progress = node('ol', '', 'milestones');
  progress.setAttribute('aria-label', 'Stages in the current workflow');
  const active = workflow.indexOf(job.stage);
  for (const [index, stage] of workflow.entries()) {
    const item = node('li', stages[stage]);
    if (active === index) item.setAttribute('aria-current', 'step');
    if ((active > index && (job.round === 1 || index < 2)) || ['published', 'ready'].includes(job.stage)) item.className = 'complete';
    progress.append(item);
  }
  const key = JSON.stringify([repo, job.number]);
  const openRounds = el('detail').dataset.run === key
    ? new Map(Array.from(el('detail').querySelectorAll('details')).map((item) => [item.dataset.round, item.open]))
    : new Map();
  const evidence = node('section', '', 'evidence');
  evidence.append(node('h3', 'Checks and reviews by round'));
  const roundList = node('div', '', 'rounds');
  for (const [number, reports] of Object.entries(rounds).reverse()) {
    const details = node('details');
    details.dataset.round = number;
    details.open = openRounds.get(number) ?? (number === String(job.round));
    details.append(node('summary', `Round ${number}`));
    const chips = node('ul', '', 'verdicts');
    for (const role of Object.keys(roles)) {
      const result = reports[role];
      if (!result) continue;
      const state = ['READY', 'PASS', 'ACCEPT'].includes(result.verdict) ? 'good' : 'attention';
      chips.append(node('li', `${roles[role]} · ${result.verdict}`, state));
    }
    details.append(chips);
    for (const role of Object.keys(roles)) {
      if (!reports[role]?.report) continue;
      details.append(node('h4', roles[role]), node('pre', reports[role].report));
    }
    roundList.append(details);
  }
  if (roundList.childElementCount) evidence.append(roundList);
  else evidence.append(node('p', 'Results appear here as checks and reviews complete.', 'muted empty'));
  const activity = node('section', '', 'evidence');
  activity.append(node('h3', 'Recent activity'));
  const history = node('ol', '', 'history');
  for (const entry of entries.slice(-10).reverse()) {
    const role = entry.event.startsWith('stage-') ? roles[entry.event.slice(6)] : null;
    const label = role ? `${role} recorded` : events[entry.event] || entry.event;
    const row = node('li');
    const timeEl = node('time', formatTime(entry.at));
    timeEl.dateTime = entry.at;
    row.append(node('span', `${label} · ${stages[entry.stage] || entry.stage}`), timeEl);
    history.append(row);
  }
  activity.append(history);
  el('detail').dataset.run = key;
  el('detail').replaceChildren(meta, links, progress, evidence, activity);
}

async function showSelected(moveFocus = false) {
  const target = selection;
  if (!target) return;
  const stamp = JSON.stringify([target.name, target.number, target.updated, target.stage]);
  if (stamp === detailStamp) {
    if (moveFocus) el('run-title').focus();
    return;
  }
  const params = new URLSearchParams({repository: target.name, issue: String(target.number)});
  let payload;
  try {
    payload = await read(`/api/run?${params}`);
  } catch (error) {
    if (sameRun(target, selection)) showDetailError(error);
    return;
  }
  if (!sameRun(target, selection) || target.updated !== selection.updated) return;
  detailStamp = stamp;
  renderDetail(payload, target.name);
  if (moveFocus) el('run-title').focus();
}

function showDetailError(error) {
  el('detail').replaceChildren(node('p', error.message, 'notice'));
  detailStamp = '';
}

function connectionStatus(text, stale = false) {
  const status = el('connection');
  if (status.textContent !== text) status.textContent = text;
  status.classList.toggle('stale', stale);
}

async function refresh() {
  try {
    const payload = await read('/api/runs');
    current = payload;
    repositories = payload.repositories;
    statusDetails(payload);
    renderList();
    connectionStatus('Live · refreshes every 2s');
    el('message').hidden = true;
    await showSelected();
  } catch (error) {
    connectionStatus('Dashboard disconnected', true);
    el('message').textContent = 'The local dashboard stopped responding. Keep the dashboard command running and reopen its private URL if needed.';
    el('message').hidden = false;
    if (!current) el('empty').textContent = 'Live data is unavailable.';
  } finally {
    setTimeout(refresh, 2000);
  }
}

el('repository').addEventListener('change', () => { renderList(); showSelected(); });
if (session) refresh();
else {
  el('connection').textContent = 'Private link required';
  el('empty').textContent = 'Open the private dashboard URL printed in your terminal.';
}
