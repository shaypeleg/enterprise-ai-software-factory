'use strict';

const session = location.hash.slice(1);
const element = (id) => document.getElementById(id);
const form = element('setup');
const panels = [...form.querySelectorAll('.panel')];
const steps = [...document.querySelectorAll('.steps li')];
const next = element('next');
const messages = element('message');
const roles = {
  intake: ['Intake', 'Scope and assess risk'],
  spec: ['Specification', 'Define acceptance criteria'],
  build: ['Builder', 'Implement the candidate'],
  code: ['Code review', 'Check behavior and test quality'],
  security: ['Security review', 'Assess security and dependencies'],
  ux: ['UX review', 'Check interaction and accessibility when applicable'],
  ui: ['UI review', 'Check design and layout when applicable'],
  handoff: ['Handoff', 'Summarize evidence for review'],
};
const fields = ['git_url', 'base_branch'];
const scmFields = ['provider', 'api_url', 'repository', 'token_env', 'queue_label'];
const limits = ['max_rounds', 'check_timeout_seconds', 'agent_timeout_seconds'];
let step = 0;
let settings;

function notice(text, info = false) {
  messages.hidden = !text;
  messages.textContent = text;
  messages.classList.toggle('info', info);
}

async function request(options = {}, endpoint = '/api/setup') {
  const selected = element('workspace-repository').value;
  const query = selected && endpoint === '/api/setup' ? `?repository=${encodeURIComponent(selected)}` : '';
  const result = await fetch(endpoint + query, {
    ...options,
    headers: {'X-Factory-Session': session, ...options.headers},
  });
  const body = await result.json();
  if (!result.ok) throw new Error(body.error);
  return body;
}

function showStep(index, focus = true) {
  step = index;
  for (const [position, panel] of panels.entries()) {
    panel.hidden = position !== index;
    steps[position].toggleAttribute('aria-current', position === index);
    if (position === index) steps[position].setAttribute('aria-current', 'step');
  }
  next.textContent = index === 2 ? 'Save configuration' : 'Continue';
  element('back').hidden = index === 0;
  if (index === 2) {
    element('permission-target').textContent = `${element('repository').value} · ${element('api_url').value}`;
  }
  if (focus) panels[index].querySelector('h2').focus();
}

function describeAccess() {
  const provider = element('provider').value;
  element('permissions').textContent = provider === 'github'
    ? 'Use a repository-scoped GitHub credential. Queue access needs issue read permission. Publishing additionally needs Contents and Pull requests write permission.'
    : 'Use a project-scoped GitLab credential. Reading requires API access. Publishing additionally needs API write access and permission to push candidate branches.';
}

function providerChanged() {
  const provider = element('provider').value;
  if (!provider) return;
  let host = '';
  try { host = new URL(element('git_url').value).host; } catch { /* An empty endpoint can be filled in. */ }
  element('api_url').value = !host ? '' : provider === 'github'
    ? (host === 'github.com' ? 'https://api.github.com' : `https://${host}/api/v3`)
    : `https://${host}/api/v4`;
  element('token_env').value = `FACTORY_${provider.toUpperCase()}_TOKEN`;
  describeAccess();
}

function buildRoleInputs(values, names) {
  element('default-model').value = values.default ?? '';
  for (const name of names) {
    const description = roles[name];
    if (!description) continue;
    const label = document.createElement('label');
    label.className = 'role';
    const title = document.createElement('span');
    title.textContent = description[0];
    const detail = document.createElement('small');
    detail.textContent = description[1];
    title.append(detail);
    const input = document.createElement('input');
    input.id = `model-${name}`;
    input.setAttribute('list', 'model-ids');
    input.placeholder = 'Use default model';
    input.autocomplete = 'off';
    input.value = values[name] ?? '';
    label.append(title, input);
    element('roles').append(label);
  }
}

function collect() {
  const value = (id) => element(id).value.trim();
  const config = {version: 1};
  for (const key of fields) config[key] = value(key);
  config.scm = Object.fromEntries(scmFields.map((key) => [key, value(key)]));
  config.models = {default: value('default-model')};
  for (const role of settings.roles) {
    if (value(`model-${role}`)) config.models[role] = value(`model-${role}`);
  }
  config.checks = JSON.parse(value('checks'));
  config.protected_paths = value('protected_paths').split('\n').map((line) => line.trim()).filter(Boolean);
  for (const key of limits) config[key] = Number(value(key));
  return {config, consent: element('consent').checked, share: element('share').checked};
}

form.noValidate = true;
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  notice('');
  for (const input of panels[step].querySelectorAll('input, select, textarea')) {
    if (!input.checkValidity()) {
      const details = input.closest('details');
      if (details) details.open = true;
      input.reportValidity();
      return;
    }
  }
  if (step < 2) return showStep(step + 1);
  let payload;
  try { payload = collect(); } catch {
    notice('Enter your check commands as a JSON array of argument arrays.');
    element('checks').focus();
    return;
  }
  next.disabled = true;
  element('workspace-repository').disabled = true;
  element('back').disabled = true;
  next.textContent = 'Saving…';
  try {
    const saved = await request({method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    form.hidden = true;
    document.querySelector('.steps').hidden = true;
    element('saved').hidden = false;
    element('saved-path').textContent = saved.shared
      ? `Private config saved at ${saved.path}. The project preset was also saved.`
      : `Private config saved at ${saved.path}.`;
    element('finish').hidden = !settings.repositories;
    element('skill-command').textContent = settings.repositories ? '$factory workspace list' : '$factory';
    if (settings.repositories) {
      element('workspace-repository').selectedOptions[0].textContent = repositoryLabel(settings.selected, 'configured');
    }
    element('saved').querySelector('h2').focus();
  } catch (error) {
    notice(error.message);
  } finally {
    next.disabled = false;
    element('workspace-repository').disabled = false;
    element('back').disabled = false;
    next.textContent = 'Save configuration';
  }
});

element('back').addEventListener('click', () => { notice(''); showStep(step - 1); });
element('provider').addEventListener('change', providerChanged);

function repositoryLabel(alias, state) {
  return `${alias === '.' ? 'Parent repository' : alias} · ${state}`;
}

async function loadRepository() {
  const selector = element('workspace-repository');
  selector.disabled = true;
  form.hidden = true;
  element('saved').hidden = true;
  document.querySelector('.steps').hidden = false;
  element('loading').hidden = false;
  notice('');
  try {
    if (!session) throw new Error('Open the private setup URL printed in your terminal.');
    settings = await request();
    const config = settings.config;
    if (settings.repositories) {
      element('repository-choice').hidden = false;
      selector.replaceChildren();
      for (const repo of settings.repositories) {
        const option = document.createElement('option');
        option.value = repo.alias;
        option.disabled = !repo.available;
        const state = !repo.available ? 'checkout required' : repo.configured ? 'configured' : 'setup required';
        option.textContent = repositoryLabel(repo.alias, state);
        selector.append(option);
      }
      selector.value = settings.selected;
    }
    element('project').textContent = settings.project;
    element('project').hidden = false;
    element('origin-hint').textContent = settings.originHint;
    for (const key of fields) element(key).value = config[key] ?? '';
    for (const key of scmFields) element(key).value = config.scm[key] ?? '';
    for (const key of limits) element(key).value = config[key];
    element('checks').value = config.checks.length ? JSON.stringify(config.checks, null, 2) : '';
    element('protected_paths').value = config.protected_paths.join('\n');
    element('roles').replaceChildren();
    buildRoleInputs(config.models, settings.roles);
    element('consent').checked = false;
    element('share').checked = false;
    describeAccess();
    notice(settings.presetMessage, true);
    showStep(0, false);
    form.hidden = false;
  } catch (error) {
    notice(error.message);
  } finally {
    selector.disabled = false;
    element('loading').hidden = true;
  }
}

element('workspace-repository').addEventListener('change', loadRepository);
element('finish').addEventListener('click', async () => {
  try {
    await request({method: 'POST'}, '/api/finish');
    element('finish').hidden = true;
    element('repository-choice').hidden = true;
  } catch (error) {
    notice(error.message);
  }
});
loadRepository();
