# Enterprise AI Software Factory — Codex pilot

![Factory workflow from a labeled issue through intake, build, checks, reviews, and handoff. The factory retries within its limit and opens a draft PR or MR only when explicitly asked. Your CI deploys to staging, then promotes to production after human review and a manual merge.](docs/factory-workflow.jpg)

The factory stops at a reviewed candidate or draft PR/MR. The lower track shows
staging and production steps to connect in your own CI. [CI integration requirements](ENTERPRISE.md)

## Getting started

1. **Install the skill from your application checkout.** You need macOS or Linux,
   Git, Python 3.9+, Node.js 18+ with npm, and the OpenAI Codex CLI.

   ```bash
   cd /path/to/your/application
   npx --package=git+https://github.com/shaypeleg/enterprise-ai-software-factory.git -- factory-install
   ```

2. **Complete the browser wizard.** Select the repository, pick a model, and
   review the suggested checks. Use **Test connection** to verify issue access
   with your workspace’s Git credentials or a Git token environment variable.
   For model runs, the skill can use your saved Codex sign-in. For several checkouts,
   add `--workspace` to installation. [Workspace setup](#work-with-multiple-repositories)
3. **Label an issue.** Create the configured issue label if it is missing. The
   default is `factory:ready` (one colon). In GitLab, go to **Manage → Labels →
   New label**. In GitHub, go to **Issues → Labels → New label**. Apply the
   exact label to an open issue. This is the only label the factory requires.
4. **Watch and run the issue in Codex.** Open the same checkout in Codex, start
   the live dashboard, and open the private browser link. Verify the issue is
   in the queue, then start it by number. Labeling an issue makes it eligible
   for the queue. The run starts when you invoke it.

   ```text
   $factory dashboard
   $factory queue
   $factory run 42
   ```

   Replace `42` with your issue number. Review the local candidate and run
   `$factory publish 42` when you want a draft PR/MR. In workspace mode, use
   `$factory workspace dashboard` to watch all configured checkouts and
   `$factory workspace run 42` to start from the parent issue.

## Scope of this pilot

The factory builds reviewed candidates for GitHub and GitLab issues using Codex.
It can publish draft PRs and MRs. The project takes inspiration from the
[ZazenCodes Claude Code v2 factory](https://github.com/zazencodes/zazencodes-season-3/tree/main/src/software-factory-claude-code-v2).

This is a local pilot. Production rollout requires worker isolation, live
provider validation, and CI integration. [Enterprise acceptance gates](ENTERPRISE.md)

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

## Requirements

- Python 3.9+ standard library, Git, and the OpenAI Codex CLI. No Python dependencies.
- Node.js 18+ with npm and `npx` for the `npx` installer.
- A dedicated target checkout with a credential-free HTTPS or SSH `origin`.
- Operator-owned configuration and a private state directory **outside** that checkout.
- An OpenAI model available to your organization, selected in configuration.
- SCM credentials scoped to the configured repository. Keep production credentials
  entirely outside this controller and its workers.
- macOS or Linux; the pilot uses POSIX locks and process groups.

Each role runs in its own `codex exec` session when you start a job from the
Codex app or terminal. The Python controller records routing, results and gates.
Model IDs can be configured per role (`intake`, `spec`,
`build`, `code`, `security`, `ux`, `ui`, `handoff`), falling back to `default`.

### Sign in for model runs

With `CODEX_API_KEY` unset, the factory uses the ChatGPT sign-in visible to the
local Codex CLI. Run `codex login status` to check. If the CLI cannot see your
desktop sign-in, run `codex login` on the machine that executes the skill.
Use a current Codex CLI with permission profiles. The wizard defaults to
`auto` so Codex can select an available model. Enter a model ID to override
that selection for any role. For headless jobs, setting `CODEX_API_KEY` explicitly uses
API-key authentication. GitHub and GitLab authentication is configured separately.
[Codex authentication](https://learn.chatgpt.com/docs/auth)

## Install the skill with npx

Use the command in [Getting started](#getting-started) from your application’s
Git checkout. The [OpenAI Codex CLI](https://developers.openai.com/codex/cli/)
installation guide covers the required CLI.

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

## Update an installed skill

Rerun the installer from the same project or workspace to update the managed
skill. `--skip-setup` applies the update without reopening the wizard. Saved
configuration and job state stay in place.

```bash
npx --package=git+https://github.com/shaypeleg/enterprise-ai-software-factory.git -- factory-install --skip-setup
```

For a multi-repository install, append `--workspace` and run from the workspace
root. To install a particular revision, append `#COMMIT_OR_TAG` to the package
URL and replace `COMMIT_OR_TAG` with its GitHub commit ID or tag. If Codex still
shows the previous skill, reopen the project or restart Codex.

The installer does not check for updates in the background. Check the
[repository’s commit history](https://github.com/shaypeleg/enterprise-ai-software-factory/commits/main/)
for changes. GitHub’s **Watch → Custom → Releases** can notify you when a
release is published. A push to `main` alone does not publish a release.

## Configure in the browser

The wizard shows the full delivery workflow, including the future staging and
production CI steps. It detects the application's Git origin, suggests the
repository and branch, and lets you select GitHub or GitLab and edit the API
endpoint. Keep the HTTPS clone URL set to the HTTPS equivalent of your origin.
With workspace credentials and an SSH origin, the factory fetches the job's
base branch over SSH using your existing SSH setup. Issue access and publishing
still use HTTPS. Self-managed hosts that cannot be identified by name need an
explicit provider selection.

Choose a default OpenAI model ID and optionally override it for each role. Use
IDs available to your OpenAI API project. New setups suggest checks from
recognized Python GitLab CI test commands or npm/pnpm scripts with a lockfile.
If there is no match, the wizard starts with `git diff --check`. Add project tests
when available. Existing saved checks take precedence, and **Use suggested
checks** replaces the draft when you choose it. Review the commands before saving.

**Agent timeout** defaults to 900 seconds (15 minutes) for each Codex invocation,
including its tool calls. **Check timeout** defaults to 300 seconds (5 minutes)
for each configured command, including dependency installation. These limits are
per invocation or command, so a full job with several stages and rounds can take
longer. Increase either limit in **Advanced check settings**, up to 3,600 seconds.

New setups default to **Use workspace Git credentials**. The factory first asks
the checkout’s Git credential helper for an HTTPS credential, then tries the
saved `glab` or `gh` login for the same host. SSH can remain your Git protocol.
Sign in once with `glab auth login --hostname YOUR_GITLAB_HOST --use-keyring`
or `gh auth login --hostname YOUR_GITHUB_HOST`, then use the skill from Codex
desktop without keeping a terminal open. For CI or a service account, choose
**Use a token environment variable** and set its value in the launch environment.
Existing selections are preserved until you change them in setup.
The token is kept out of the browser and saved settings. The test contacts the
Git origin’s HTTPS host and port, or the official GitHub API for `github.com`,
and checks issue read access. Push and PR/MR permissions are checked when used.
For a separate API hostname, use an environment token and save unverified.

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
$factory help
$factory setup
$factory queue
$factory status
$factory dashboard
$factory run 42
$factory publish 42
```

Use `$factory help` before or after setup to see how jobs, the dashboard, and
publishing work. [Factory help](skills/factory/HELP.md)

Replace `42` with an open issue carrying the configured label. `run` prepares a
local candidate. `publish` opens a draft PR/MR for a passing candidate. You can
explicitly request `$factory run 42 --publish` to do both. Setup can be re-opened
with `$factory setup`.

For direct CLI use, run `python3 -m factory --project /path/to/application validate`
from the factory checkout. The existing `--config /path/to/config.json` option is
available for operator-managed, out-of-tree configuration using
[`config.example.json`](config.example.json).

## Watch runs in the browser

Open `$factory dashboard` for the current checkout or `$factory workspace dashboard`
for all configured checkouts. The live board moves cards through Intake, Spec,
Build, Review and Approval. Jobs waiting on a person go to Needs human, and draft
publication or rejection moves them to Done. A card in Approval can be finishing
the handoff or waiting for you to request publication.

Each round shows green tags for passing checks or reviews, red for failures or
requested changes, and a dotted tag for the check or reviewer currently running.
Open a card for reports, recent activity, and links to the issue and draft PR/MR.
The page refreshes every two seconds while the dashboard command is running and
reads the job database alongside the worker.

Start the dashboard before or during a run. In a terminal from the target project,
use `python3 .agents/skills/factory/launch.py dashboard` (or `workspace dashboard`).
Keep that command running and start the job from Codex or another terminal. Use
`--no-browser` to print the private URL, and `Ctrl+C` in the dashboard terminal
to close it. The dashboard is read-only and displays jobs from the current saved configuration.

The `release_intent` output identifies staging and the exact commit for a future
trusted deployment adapter. It is **unsigned advisory data, not deployment authorization**.

`queue` is a paginated read of labeled open issues. The controller currently
processes selected issues one at a time. An external trusted scheduler
can poll this queue, apply authorization/rate limits, and call `run N --publish`.
There is no public webhook listener or built-in issue-label event daemon yet.
Never interpolate issue text into shell commands or accept an arbitrary repository
URL from a webhook. Resolve all work through the operator's allowlisted configuration.

## Work with multiple repositories

Install from the workspace root with `--workspace`:

```bash
npx --package=git+https://github.com/shaypeleg/enterprise-ai-software-factory.git -- factory-install --workspace
```

The setup wizard lists the parent checkout and its Git submodules. Choose each
repository you plan to use and save its model, check, queue and credential
settings. Finish workspace setup when you are done. Initialize any missing
submodules through your normal Git workflow before selecting them.

For a folder of independent clones, create `.factory-workspace.json` in the
folder before installing. For example:

```json
{
  "repositories": {
    "backend": "agent-backend",
    "frontend": "agent-frontend"
  }
}
```

Manifest paths refer to Git checkout roots inside the workspace. A manifest
controls which child repositories are listed. When the workspace itself is a Git
checkout, the alias `.` refers to it. Each checkout retains separate private
settings and job state. You can share its preset through the browser as usual.

In Codex:

```text
$factory workspace list
$factory workspace setup
$factory workspace queue
$factory workspace status
$factory workspace run 42
$factory workspace run agent-backend=17 agent-frontend=23
$factory workspace publish agent-backend=17 agent-frontend=23
```

A bare issue number defaults to the parent. To route a parent feature into
several repositories, add a fenced block like this to its issue description:

````markdown
```factory-routes
agent-backend=17
agent-frontend=23
```
````

Label the parent and each linked issue with their repository's configured queue
label. The factory runs the linked issues in the listed order and reports each
result separately. Every linked issue should describe the work for its repository.
An issue without a routing block runs in the parent. Use `.=42` to run just that
parent issue even if it declares routes. In a folder with no parent Git checkout,
use repository aliases with issue numbers.

Publishing is explicit. For a routed feature, publish the child issue pairs
reported by `run`. Each request is published to its own repository. Publication
stops at the first error and reports any requests already created. Retrying can
reuse those requests. A parent job can change parent files while retaining the
existing submodule pins. Updating those pins after child merges and testing the
combined change in an integration environment remain separate steps.

## Release environments

Use the same PR/MR throughout. A long-lived staging branch adds a second merge
and makes it harder to prove that production received what was tested. Prefer
an isolated staging/preview environment per candidate. If there is only one
shared staging environment, serialize deployments and invalidate the previous
candidate's sign-off when the environment changes.

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
