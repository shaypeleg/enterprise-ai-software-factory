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
- Node.js 18+ with npm and `npx` for the `npx` installer.
- A dedicated target checkout with a credential-free HTTPS or SSH `origin`.
- Operator-owned configuration and a private state directory **outside** that checkout.
- An OpenAI model available to your organization, selected in configuration.
- SCM credentials scoped to the configured repository. Keep production credentials
  entirely outside this controller and its workers.
- macOS or Linux; the pilot uses POSIX locks and process groups.

This uses `codex exec`, not Claude agent definitions or Claude subscription
authentication. Each role is a fresh Codex invocation; routing and state are
implemented in Python. Model IDs can be configured per role (`intake`, `spec`,
`build`, `code`, `security`, `ux`, `ui`, `handoff`), falling back to `default`.

## Install the skill with npx

Install the [OpenAI Codex CLI](https://developers.openai.com/codex/cli/) if needed.
In your application’s existing Git checkout, run:

```bash
cd /path/to/your/application
npx --package=git+https://github.com/shaypeleg/enterprise-ai-software-factory.git -- factory-install
```

Approve npm’s install prompt if it appears. `npx` fetches the installer directly
from GitHub. The installer keeps the runtime in your user data directory
(`$XDG_DATA_HOME` or `~/.local/share`) so it remains available after npm clears
its cache. It links `.agents/skills/factory` to that local copy and opens the
browser wizard. Leave this machine-local symlink out of version control.

If the browser cannot open, use the private URL printed in your terminal. From
another directory, pass `--project /path/to/your/application`. Add `--no-browser`
to print the private URL without opening a tab:

```bash
npx --package=git+https://github.com/shaypeleg/enterprise-ai-software-factory.git -- \
  factory-install --project /path/to/your/application --no-browser
```

For source development, clone the factory separately. The Python installer uses
that checkout as the skill’s runtime:

```bash
git clone https://github.com/shaypeleg/enterprise-ai-software-factory.git
cd enterprise-ai-software-factory
python3 install.py --project /path/to/your/application
```

If the project already uses a symlink from the Python installer, remove that
symlink before switching to `npx`. Your saved configuration stays in your user
configuration directory.

## Configure in the browser

The wizard shows the full delivery workflow, including the future staging and
production CI steps. It detects the application's Git origin, suggests the
repository and branch, and lets you select GitHub or GitLab and edit the API
endpoint. For HTTPS, use the exact origin URL. For SSH, the factory uses the
HTTPS equivalent for Git operations. Self-managed hosts that cannot be identified
by name need an explicit provider selection.

Choose a default OpenAI model ID and optionally override it for each role. Model
suggestions are examples. Use IDs available to your OpenAI API project. Add the
application's checks as JSON argument arrays, for example
`[["npm", "test"], ["npm", "run", "lint"]]`. The access step asks you to approve
those commands and the selected endpoint. Supply `CODEX_API_KEY` and the configured
SCM token separately through your secret manager or launch environment. The
wizard saves configuration locally and does not request or test credential values.

To share settings with teammates, select **Also save a shareable preset** to write
`.factory-settings.json` in the application root. Review it before committing.
Keep token values, local paths, and job state outside this file. Each developer
installs the skill locally and approves the preset in the wizard before it can be
used. An existing private setup takes precedence over a project preset. Changing
local settings starts a new job state directory and preserves the previous one.

## Invoke in Codex

Open the target application in Codex. Select **Factory** from `/skills` or type
`$factory`. Codex's skill mechanism does not register a custom `/factory` slash
command. If the skill picker has not refreshed, reopen the project or restart Codex.
See the [Codex skill documentation](https://learn.chatgpt.com/docs/build-skills).

```text
$factory setup
$factory queue
$factory status
$factory run 42
$factory publish 42
```

Replace `42` with an open issue carrying the configured label. `run` prepares a
local candidate. `publish` opens a draft PR/MR for a passing candidate. You can
explicitly request `$factory run 42 --publish` to do both. Setup can be re-opened
with `$factory setup`.

For direct CLI use, run `python3 -m factory --project /path/to/application validate`
from the factory checkout. The existing `--config /path/to/config.json` option is
available for operator-managed, out-of-tree configuration using
[`config.example.json`](config.example.json).

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
