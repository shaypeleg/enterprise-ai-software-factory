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
const scmFields = ['provider', 'api_url', 'repository', 'credential_source', 'token_env', 'queue_label'];
const limits = ['max_rounds', 'check_timeout_seconds', 'agent_timeout_seconds'];
const testButton = element('test-connection');
const allowUnverified = element('allow-unverified');
let step = 0;
let settings;
let verified = false;
let busy = false;

function notice(text, info = false) {
  messages.hidden = !text;
  messages.textContent = text;
  messages.classList.toggle('info', info);
}

async function request(options = {}, endpoint = '/api/setup') {
  const selected = element('workspace-repository').value;
  const query = selected && endpoint !== '/api/finish' ? `?repository=${encodeURIComponent(selected)}` : '';
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
  updateActions();
  element('back').hidden = index === 0;
  if (index === 2) {
    element('permission-target').textContent = `${element('repository').value} · ${element('api_url').value}`;
  }
  if (focus) panels[index].querySelector('h2').focus();
}

function updateActions() {
  const saving = step === 2;
  next.disabled = busy || (saving && !verified && !allowUnverified.checked);
  next.textContent = saving ? (verified ? 'Save configuration' : 'Save unverified configuration') : 'Continue';
  testButton.disabled = busy;
  element('unverified-option').hidden = verified;
}

function connectionStatus(text, passed = false) {
  verified = passed;
  allowUnverified.checked = false;
  element('connection-status').textContent = text;
  updateActions();
}

function invalidateConnection() {
  connectionStatus('Connection unverified.');
}

function describeAccess() {
  const provider = element('provider').value;
  element('permissions').textContent = provider === 'github'
    ? 'Use a repository-scoped GitHub credential. Queue access needs issue read permission. Publishing additionally needs Contents and Pull requests write permission.'
    : 'Use a GitLab credential with access to this project. Reading requires API access. Publishing additionally needs API write access and permission to push candidate branches.';
}

function describeCredentialSource() {
  const useVariable = element('credential_source').value === 'environment';
  element('token-variable').hidden = !useVariable;
  element('token_env').disabled = !useVariable;
  element('token_env').required = useVariable;
  element('credential-help').textContent = useVariable
    ? `Enter the variable name, for example ${defaultTokenName()}. Set its value in the terminal before launching setup.`
    : 'Use an existing Git credential or a saved CLI login for this Git host.';

  const provider = element('provider').value;
  let host = '';
  try {
    const origin = new URL(element('git_url').value);
    if (origin.protocol === 'https:' && /^[a-z0-9][a-z0-9.-]*$/i.test(origin.hostname)) host = origin.hostname;
  } catch { /* The user can fill in the origin on the repository step. */ }
  const showLogin = !useVariable && host && ['gitlab', 'github'].includes(provider);
  element('workspace-login').hidden = !showLogin;
  element('workspace-access-help').hidden = !settings?.repositories;
  if (showLogin) {
    const gitlab = provider === 'gitlab';
    element('login-command').textContent = gitlab
      ? `glab auth login --hostname ${host} --use-keyring`
      : `gh auth login --hostname ${host}`;
    element('login-prompt-help').textContent = gitlab
      ? 'Choose Token and paste it at the terminal prompt. If asked for a Git protocol, choose the one your checkout already uses. Choose HTTPS for the API.'
      : 'Follow the sign-in prompts. If asked for a Git protocol, choose the one your checkout already uses.';
  }
}

function defaultTokenName() {
  return element('provider').value === 'github' ? 'FACTORY_GITHUB_TOKEN' : 'FACTORY_GITLAB_TOKEN';
}

function validateTokenName() {
  const field = element('token_env');
  field.setCustomValidity('');
  if (!field.disabled && (!field.checkValidity() || /^(gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}$/.test(field.value))) {
    field.setCustomValidity(`Enter a variable name such as ${defaultTokenName()}. Set its value in the terminal.`);
  }
}

function providerChanged() {
  const provider = element('provider').value;
  if (!provider) return;
  let host = '';
  try { host = new URL(element('git_url').value).host; } catch { /* An empty endpoint can be filled in. */ }
  element('api_url').value = !host ? '' : provider === 'github'
    ? (host === 'github.com' ? 'https://api.github.com' : `https://${host}/api/v3`)
    : `https://${host}/api/v4`;
  element('token_env').value = defaultTokenName();
  describeCredentialSource();
  describeAccess();
  invalidateConnection();
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

const value = (id) => element(id).value.trim();

function connection() {
  const scm = Object.fromEntries(scmFields.map((key) => [key, value(key)]));
  if (scm.credential_source !== 'environment') delete scm.token_env;
  return {git_url: value('git_url'), scm};
}

function collect() {
  const config = {version: 1};
  for (const key of fields) config[key] = value(key);
  config.scm = connection().scm;
  config.models = {default: value('default-model')};
  for (const role of settings.roles) {
    if (value(`model-${role}`)) config.models[role] = value(`model-${role}`);
  }
  config.checks = JSON.parse(value('checks'));
  config.protected_paths = value('protected_paths').split('\n').map((line) => line.trim()).filter(Boolean);
  for (const key of limits) config[key] = Number(value(key));
  return {config, consent: element('consent').checked, share: element('share').checked,
    allowUnverified: allowUnverified.checked};
}

function setBusy(flag) {
  busy = flag;
  element('workspace-repository').disabled = flag;
  element('back').disabled = flag;
  updateActions();
}

testButton.addEventListener('click', async () => {
  validateTokenName();
  if (!element('token_env').reportValidity()) return;
  const payload = JSON.stringify(connection());
  setBusy(true);
  connectionStatus('Checking authenticated issue access…');
  try {
    await request({method: 'POST', headers: {'Content-Type': 'application/json'}, body: payload}, '/api/connection');
    if (payload === JSON.stringify(connection())) {
      connectionStatus('Verified. The token can read issues in this repository.', true);
    }
  } catch (error) {
    if (payload === JSON.stringify(connection())) connectionStatus(`Unverified. ${error.message}`);
  } finally {
    setBusy(false);
  }
});

for (const key of ['git_url', 'api_url', 'repository', 'token_env']) {
  element(key).addEventListener('input', invalidateConnection);
}
element('git_url').addEventListener('input', describeCredentialSource);
element('token_env').addEventListener('input', validateTokenName);
allowUnverified.addEventListener('change', updateActions);
element('use-suggested-checks').addEventListener('click', () => {
  element('checks').value = JSON.stringify(settings.suggestedChecks, null, 2);
  element('checks-suggestion').textContent = settings.suggestedChecksHint;
});

form.noValidate = true;
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy || (step === 2 && !verified && !allowUnverified.checked)) return;
  notice('');
  if (step === 2) validateTokenName();
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
  setBusy(true);
  next.textContent = 'Saving…';
  try {
    const saved = await request({method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    form.hidden = true;
    document.querySelector('.steps').hidden = true;
    element('saved').hidden = false;
    element('saved-path').textContent = saved.shared
      ? `Private config saved at ${saved.path}. The project preset was also saved.`
      : `Private config saved at ${saved.path}.`;
    if (!saved.verified) element('saved-path').textContent += ' Connection unverified.';
    element('finish').hidden = !settings.repositories;
    element('skill-command').textContent = settings.repositories ? '$factory workspace list' : '$factory';
    const command = settings.repositories ? '$factory workspace' : '$factory';
    element('dashboard-command').textContent = `${command} dashboard`;
    element('queue-command').textContent = `${command} queue`;
    const issue = settings.repositories && settings.selected !== '.' ? `${settings.selected}=42` : '42';
    element('run-command').textContent = `${command} run ${issue}`;
    element('saved-queue-label').textContent = payload.config.scm.queue_label;
    if (settings.repositories) {
      element('workspace-repository').selectedOptions[0].textContent = repositoryLabel(settings.selected, 'configured');
    }
    element('saved').querySelector('h2').focus();
  } catch (error) {
    notice(error.message);
  } finally {
    setBusy(false);
  }
});

element('back').addEventListener('click', () => { notice(''); showStep(step - 1); });
element('provider').addEventListener('change', providerChanged);
element('credential_source').addEventListener('change', () => {
  if (element('credential_source').value === 'workspace') element('token_env').value = defaultTokenName();
  describeCredentialSource();
  validateTokenName();
  invalidateConnection();
});

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
    element('workspace-label-help').hidden = !settings.repositories;
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
    element('credential_source').value = config.scm.credential_source ?? 'environment';
    if (!element('token_env').value) element('token_env').value = defaultTokenName();
    for (const key of limits) element(key).value = config[key];
    element('checks').value = config.checks.length ? JSON.stringify(config.checks, null, 2) : '';
    element('checks-suggestion').textContent = settings.checksHint;
    element('protected_paths').value = config.protected_paths.join('\n');
    element('roles').replaceChildren();
    buildRoleInputs(config.models, settings.roles);
    element('consent').checked = false;
    element('share').checked = false;
    invalidateConnection();
    describeAccess();
    describeCredentialSource();
    validateTokenName();
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
