# Enterprise AI Software Factory — Codex pilot

A Codex/OpenAI port inspired by the
[ZazenCodes Claude Code v2 factory](https://github.com/zazencodes/zazencodes-season-3/tree/main/src/software-factory-claude-code-v2).
This version supports GitHub and GitLab issue
queues, produces reviewed candidate changes, and can publish a draft PR/MR.
It has **no merge, PR approval, or deployment capability**.

**Maturity: tested local foundation, not an enterprise production service.**
No live model/provider/deployment integration has been verified. See
[the enterprise acceptance gates](ENTERPRISE.md) before using company code.
The end-to-end deployment adapter is deliberately unconfigured until the
deployment platform is chosen.

## Runtime support and configuration

**The implemented agent runtime is OpenAI Codex CLI only.** Claude Code is not
currently supported by this port, and there is no configuration switch that
enables it. Adding Claude Code would require a separate runtime adapter and
tests. The original Claude implementation is available in the upstream link above.

You can configure OpenAI models per role, GitHub or GitLab (including enterprise
hosts), the target repository, queue label, base branch, checks, protected paths,
timeouts and maximum revision rounds in an operator-owned JSON file. The
`scm.provider` setting selects the Git hosting provider; it does not select an
AI runtime. This pilot deliberately refuses configuration that enables factory
merges or deployments.

## Intended release flow

1. An authorized operator applies a configurable issue label on GitHub or GitLab.
2. The controller fetches the issue and pins the base commit.
3. Codex performs intake, specification and implementation in a separate worktree.
4. Operator-owned checks must pass. Independent code and security reviews must
   both pass; UI/UX reviewers also run when intake identifies UI work.
5. A final handoff checks the evidence. Any failure is revised within a bounded
   round budget or parked for a human. Nothing is published on a failed run.
6. With publication enabled, the publisher creates a `factory/issue-N` branch
   and draft PR/MR, recording the exact candidate commit.
7. **Target integration:** trusted CI independently validates that candidate,
   builds an immutable artifact and automatically deploys it to staging.
   A human does not need to authorize staging after the factory's gates pass.
8. You review the staging result, mark the PR/MR ready and manually merge it.
   Production CI promotes the verified artifact after validating the merge and
   staging evidence. The factory cannot approve or perform that merge.

Steps 1–6 are implemented here. Steps 7–8 are a concrete integration contract in
[ENTERPRISE.md](ENTERPRISE.md), not a working deployment integration.

Use the same PR/MR throughout. A long-lived staging branch adds a second merge
and makes it harder to prove that production received what was tested. Prefer
an isolated staging/preview environment per candidate. If there is only one
shared staging environment, serialize deployments and invalidate the previous
candidate's sign-off when the environment changes.

## Requirements

- Python 3.9+ standard library, Git, and the OpenAI Codex CLI. No Python dependencies.
- A dedicated HTTPS target checkout with `origin` matching `git_url` exactly.
- Operator-owned configuration and a private state directory **outside** that checkout.
- An OpenAI model available to your organization, selected in configuration.
- SCM credentials scoped to the configured repository. Keep production credentials
  entirely outside this controller and its workers.
- macOS or Linux; the pilot uses POSIX locks and process groups.

This uses `codex exec`, not Claude agent definitions or Claude subscription
authentication. Each role is a fresh Codex invocation; routing and state are
implemented in Python. Model IDs can be configured per role (`intake`, `spec`,
`build`, `code`, `security`, `ux`, `ui`, `handoff`), falling back to `default`.

## Clone the factory

```bash
git clone https://github.com/shaypeleg/enterprise-ai-software-factory.git
cd enterprise-ai-software-factory
```

The repository root contains `factory/`, `tests/`, and `config.example.json`.
Run all commands below from this root. Install the
[OpenAI Codex CLI](https://developers.openai.com/codex/cli/) if needed, then
verify the required tools:

```bash
python3 --version
git --version
codex --version
```

No Python package installation is required. This repository hosts the factory
controller; the target repository is the application you want it to work on.
Use a separate dedicated clone of that application. For example, replacing the
GitLab URL with your actual application URL:

```bash
mkdir -p "$HOME/dev"
git clone https://gitlab.example.com/team/application.git "$HOME/dev/factory-target"
```

The factory repository can be hosted on GitHub while the application and its
issue queue are hosted on GitLab, or the reverse.

## Configure

Create private directories for configuration and runtime state, outside the
target application checkout. Preserve any configuration already present:

```bash
mkdir -p "$HOME/.config/enterprise-ai-software-factory"
mkdir -p "$HOME/.local/state/enterprise-ai-software-factory"
chmod 700 "$HOME/.config/enterprise-ai-software-factory"
chmod 700 "$HOME/.local/state/enterprise-ai-software-factory"
cp -n config.example.json "$HOME/.config/enterprise-ai-software-factory/config.json"
```

Edit the copied JSON file before running. Use literal absolute paths in JSON;
`~` and `$HOME` are not expanded by the configuration loader.

- `checkout`: the absolute path of the application clone, such as
  `/Users/your-name/dev/factory-target`.
- `state_dir`: the absolute path of the private state directory created above,
  such as `/Users/your-name/.local/state/enterprise-ai-software-factory`.
- `git_url`, `base_branch`: the exact HTTPS remote and intended PR/MR target.
- `scm`: the provider, API URL, repository, token environment variable and queue label.
- `models.default`: replace the model placeholder with an available OpenAI model ID.
- `checks`: actual test/lint/typecheck/build commands as argument arrays. The
  example command deliberately fails until replaced. Dependency installation
  must be an explicit check/setup command, using your existing lockfile tooling.
- `protected_paths`: additional infrastructure, policy and release files.

GitLab supports self-managed hosts and nested groups, for example
`api_url: https://gitlab.company.example/api/v4` and
`repository: engineering/platform/application`.

For GitHub use `provider: github`, `api_url: https://api.github.com`,
`repository: organization/application`, an appropriate `git_url`, and a
`token_env` such as `FACTORY_GITHUB_TOKEN`. GitHub Enterprise Server can use its
explicit HTTPS API base such as `https://git.company.example/api/v3`.

For example, a GitHub target application uses these fields (replace the owner
and application names; retain the other fields from the example configuration):

```json
{
  "git_url": "https://github.com/YOUR_ORG/YOUR_APP.git",
  "scm": {
    "provider": "github",
    "api_url": "https://api.github.com",
    "repository": "YOUR_ORG/YOUR_APP",
    "token_env": "FACTORY_GITHUB_TOKEN",
    "queue_label": "factory:ready"
  }
}
```

Apply the configured queue label to an open issue in that target application.
`git_url` must match the application's `origin` exactly. The factory repository
URL above is the source-installation URL, not automatically your application's URL.

Provision tokens in your secret manager/environment; do not put them in JSON.
The local pilot reads `CODEX_API_KEY` for Codex and the configured token variable
for SCM calls. API keys are an automation authentication option, with their own
organization billing and policies; a ChatGPT subscription is not an API budget.

## Run

From the factory repository root, after setting credentials through your normal
secret-management mechanism:

```bash
FACTORY_CONFIG="$HOME/.config/enterprise-ai-software-factory/config.json"
python3 -m factory --config "$FACTORY_CONFIG" validate
python3 -m factory --config "$FACTORY_CONFIG" queue
python3 -m factory --config "$FACTORY_CONFIG" run 42
python3 -m factory --config "$FACTORY_CONFIG" status
```

Replace `42` with an open issue number carrying the configured label. The
`validate` command checks local configuration; it does not verify credentials,
model availability, provider permissions or deployment integrations. The shipped
example intentionally needs real paths, a model ID and project checks before use.

`run` makes only local changes. To explicitly authorize committing, pushing the
candidate branch and creating a draft PR/MR after the factory's gates pass:

```bash
python3 -m factory --config "$FACTORY_CONFIG" run 42 --publish
```

Or inspect a local `ready` job first, then use `publish 42`. Neither command
deploys; production/staging deployment credentials are never requested.
The `release_intent` output identifies staging and the exact commit for a future
trusted deployment adapter. It is **unsigned advisory data, not deployment authorization**.

`queue` is a paginated read of labeled open issues. The controller currently
processes one explicitly selected issue at a time. An external trusted scheduler
can poll this queue, apply authorization/rate limits, and call `run N --publish`.
There is no public webhook listener or built-in issue-label event daemon yet.
Never interpolate issue text into shell commands or accept an arbitrary repository
URL from a webhook. Resolve all work through the operator's allowlisted configuration.

## Recovery and guardrails

- SQLite snapshots and append-only application events are written transactionally
  with `synchronous=FULL`. One local worker holds the state directory lock.
- Restart an interrupted job with the same `run N`; incomplete stages are rerun.
  Read-only stage mutation parks the job instead of trusting a rerun.
- `max_rounds` bounds correction attempts. Each model/check has a timeout; model
  and check process groups are killed on timeout. Provider reads have bounded
  retries and pagination; ambiguous writes are reconciled on the next invocation.
- Publishing retries reuse the recorded commit and look up an existing PR/MR by
  branch. A conflicting branch/closed PR or changed base requires manual intervention.
- The reviewed fingerprint covers tracked and nonignored new files, contents and
  Git executable modes. Candidate mutation blocks publication. Stored commit blobs
  must also match, so Git clean filters cannot substitute unreviewed bytes. Protected additions,
  modifications, deletions and renames are rejected.
- Code and security reviews are mandatory on every job. Intake cannot disable them.
- Configuration changes require a fresh state directory so previous evidence
  cannot silently acquire a different policy. Do not delete old audit evidence.
- `needs-info`, `needs-human`, `rejected` and `published` are parked states in
  this pilot. There is no feedback/rescope command yet. Retain the old state and
  create a fresh controlled run after addressing the reason; inspect any existing
  candidate branch/PR first. This intentionally refuses to overwrite an older candidate.

Local state/logs contain source code and issue text. Keep them private and apply
your organization's encryption, retention and redaction policies. These local
events are not tamper-proof enterprise audit records. Ignored/generated files
are excluded from the source fingerprint; checks must not rely on stale build output.

## Verify the port

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q factory tests
```

Tests exercise real temporary Git repositories, worktrees, subprocess checks,
SQLite recovery and both provider adapters. Model calls and remote writes are
faked; the suite does not need credentials, spend model tokens, or contact SCM.

## Security boundary and license

Worktrees, prompts, a second model review, environment filtering and Codex's
sandbox are useful controls. **They do not make the worker an enterprise sandbox.**
Untrusted code and an agent can attack the surrounding host, shared Git metadata,
network, process credentials or dependencies. Run only trusted pilot projects
on a disposable host. Build/test workers need an external isolation boundary
before enterprise use. See [ENTERPRISE.md](ENTERPRISE.md).

This adaptation retains the reference's GPLv3 license; see [LICENSE](LICENSE)
and [NOTICE](NOTICE). Have your licensing team evaluate intended redistribution
and modifications. The reference also advertises a commercial licensing option.
