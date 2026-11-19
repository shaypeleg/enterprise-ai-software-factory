# Enterprise target and acceptance gates

## Decision

Support GitHub and GitLab through provider adapters. Use Codex/OpenAI for code
work, deterministic software for orchestration, and your deployment platform
for release control. Automatically deploy a passing factory candidate to
staging; require an authorized human's manual merge before production.

The local implementation is a pilot control loop. This document specifies what
must be added and verified before enterprise rollout. It does not certify the
code or imply that external protections have been configured.

## Separate four identities

1. **Intake/controller:** can read allowlisted issues and metadata. Verifies
   who requested execution and whether the request is still authorized. Cannot
   merge, approve, deploy or administer repositories.
2. **Coding/check workers:** receive a disposable source snapshot and bounded
   task. Have no SCM write token, deployment credentials, Docker socket, host
   home, shared Git credentials or access to the controller's database.
   Model access goes through a scoped proxy or broker so repository scripts
   cannot read the organization's API key. Never expose production data.
3. **Publisher:** receives the reviewed patch and commit-bound evidence. Can
   write only candidate branches and create/update PRs/MRs. Does not execute
   repository code. Has no approval, merge, repository administration or
   deployment capability. Enforce branch restrictions server-side.
4. **Deployment identities:** trusted CI deploys to staging automatically after
   verified gates; a separate production identity is available only to the
   protected production pipeline. Neither token enters an agent workspace.

The pilot filters SCM and cloud credentials from agent and check environments.
For local sign-in, the Codex process uses the account profile and its permission
profile denies role commands access to the file-based login cache. API-key mode
uses a fresh profile and passes the key to the Codex process. Pilot processes
still share a host, and configured check commands run as the local user. Worker
isolation and a model-auth proxy remain required before enterprise rollout.

## Exact staging and production contract

Use one feature PR/MR into protected `main`, with a staging deployment per
candidate. A separate staging branch is optional when required by an existing
release train, but is not necessary for environment separation.

1. Pin the base commit and candidate source tree. Run deterministic checks
   using trusted pipeline definitions, plus code/security model reviews and
   UI/UX reviews when relevant. Build a deployable artifact once, record its
   immutable digest and provenance, and retain test/review evidence.
2. A trusted controller attests that all required factory verdicts apply to
   that exact candidate commit and policy version. Sign/store this evidence
   outside the candidate checkout. The pilot's JSON `release_intent` is not
   such an attestation.
3. CI independently verifies controller identity, repository, source branch,
   candidate SHA, current base, artifact digest and required statuses. It must
   reject missing, stale, ambiguous or unsigned evidence. The AI saying
   "APPROVE" or a user applying a label does not satisfy this gate.
4. If those gates pass, deploy to staging automatically. Run smoke, integration
   and relevant end-to-end checks, then report the environment URL and artifact
   digest on the PR/MR. A failed deployment/check blocks release and leaves
   production untouched. Use test identities, sanitized fixtures and isolated data.
5. An authorized human reviews code, evidence and that staging environment.
   Their **manual merge is the production authorization**. The factory service
   account cannot merge, enable auto-merge or satisfy required human approval.
6. The protected production pipeline verifies that this merge includes the
   reviewed candidate, the required human approved it, the relevant staging
   checks/sign-off are current, and the target artifact is the staged digest.
   Promote that artifact; do not silently rebuild different bytes. Confirm
   health after release and retain an explicit rollback to a known digest.

Bind human approvals to the candidate SHA and artifact digest. New commits
invalidate approvals, tests, model reviews and staging evidence. If the base
advances, use a merge queue/merged-results pipeline or rebase, rerun the full
pipeline and restage. Never reuse a green result from an older tree.

A merge commit or squash has a different SHA from the staged source commit.
Choose either a fast-forward-only release after ensuring the base has not
advanced, or a merge queue that stages the actual integration result. If using
squash, verify tree/provenance equivalence explicitly; comparing SHA strings
alone is insufficient. Disable competing jobs that also deploy `main` without
checking the staging evidence.

For a single shared staging environment, enforce a deployment lock and record
the currently deployed candidate/digest. Human sign-off must refer to that
exact deployment; a later overwrite invalidates it. Per-PR environments avoid
most of this ambiguity.

## Provider enforcement

### GitHub

Use a dedicated GitHub App with selected-repository installation and minimal
permissions. Separate issue intake/read access, candidate publication, and CI
deployment tokens. Configure rulesets/branch protection for `main`: no direct
pushes, required independent human approval, required CI/staging statuses,
dismiss stale approvals, and no factory bypass permission. Protect policy and
workflow changes with CODEOWNERS and independent review.

Run untrusted PR build/test code with read-only permissions and no deployment
secrets. A separate trusted workflow verifies signed provenance and deploys
the artifact. Do not check out/execute PR code in a privileged
`pull_request_target` workflow or blindly trust a `workflow_run` artifact.
Protect staging/production environments and use short-lived cloud federation
where supported. Availability of environment reviewer/protection features
depends on the repository visibility and GitHub plan.

### GitLab

Configure an explicit API host and allowlisted project IDs/paths; support
self-managed TLS using your organization's trusted CA, never by disabling TLS
verification. Restrict which roles can apply the queue label or validate the
label event actor separately; a label name alone is not an authorization check.

Use scoped project/group access tokens or an approved application identity.
Protect `main` against direct pushes from the factory. Require human approval,
successful pipelines and staging evidence; reset approvals on new commits and
prevent author/bot self-approval. Protect CI configuration through CODEOWNERS.

Use a separate protected deployment project/pipeline when MR jobs cannot
safely receive staging credentials. Verify the exact source project, candidate
SHA and signed evidence before triggering it. Protected environments and
deployment approvals can reinforce production authorization. GitLab documents
deployment approvals as Premium/Ultimate; validate your self-managed version
and license rather than assuming the feature exists. MR approval and deployment
approval are different controls; this design does not require human approval
for staging after all factory gates pass.

## Runtime hardening

- Ephemeral non-root worker containers or VMs; read-only root filesystem where
  possible; drop capabilities; seccomp/AppArmor or equivalent; no host mounts,
  shared credentials or privileged nested containers. Worktrees are not a
  tenancy boundary. Use independent clones/source archives in enterprise workers.
- Default-deny egress. Allow only approved dependency mirrors and model proxy
  endpoints by stage. Prevent cloud metadata/private-network access. Model
  network access and code/test network access need different policies.
- Trusted controller image, pinned Codex version, pinned dependency tooling and
  lockfiles, image/package scanning, SBOM, secret scan and artifact provenance.
  Checks still execute untrusted repository code even if the command is trusted.
- Budgets: per-job wall time, process count, CPU/RAM/disk, model requests/tokens,
  organization spend caps, queue depth and tenant concurrency; a kill switch
  that revokes execution and credentials. Timeouts alone are not spend caps.
- Treat issues, comments, repository instructions, dependency output and model
  responses as untrusted. Prompts are not an authorization mechanism. Require
  schema validation, allowlisted tools and deterministic policy enforcement.
- SSO/RBAC for operators; separate policy administrators, production approvers
  and bot roles. Resolve data residency, source-code handling, model retention,
  audit export and organization API policy with your security team.

## Reliability and evidence

The pilot has local SQLite transactions, a single-worker lock, bounded rounds,
timeouts, paginated provider reads, bounded read retries and idempotent PR/MR
reconciliation. It does not have a durable distributed work queue, cross-host
leases, failover, remote backups, a webhook service, webhook authentication,
metrics/SLOs, a secret broker or immutable audit storage.

For hosted operation, use a transactional database plus durable queue/outbox,
leases with heartbeats and fencing, unique provider/project/issue/run keys,
idempotent side-effect handlers, bounded exponential backoff with jitter and
provider-aware rate limiting. Verify webhook signatures/tokens on raw requests,
validate event type/project/actor, reject replay, deduplicate deliveries and
re-fetch authoritative issue state before starting. GitLab and GitHub have
different webhook authentication mechanisms; implement their native checks.

Retain a tamper-evident audit trail of operator identity, event ID, issue
revision, policy/prompt/model/CLI versions, base/candidate SHA, diffs, verdicts,
tool invocations, token usage, cost, checks, artifact provenance/digest,
staging deployment, human sign-off, merge, production deployment and rollback.
Redact secrets; encrypt storage; restrict access; define retention and restore tests.

## Acceptance before enterprise use

- Exercise both providers against disposable private repositories: label intake,
  nested projects, permissions, branch protection, API rate limits, pagination,
  stale approvals, draft requests and ambiguous request-creation responses.
- Verify no worker can read SCM/model/deployment secrets, access another tenant,
  alter controller state/policy, escape the sandbox, or directly push/merge `main`.
- Inject failures at every stage and side-effect boundary, including process
  death during commit/push/PR creation, replayed events, expired tokens, database
  failover, interrupted staging, stale base, concurrent candidates and disk exhaustion.
- Prove all failed/absent checks block staging; all missing/stale staging or
  human evidence blocks production; new commits reset the evidence; production
  receives exactly the tested artifact; rollback and audit restore work.
- Conduct a threat model, independent security review and load/soak test.
  Choose availability/recovery targets and validate them with real operations.

## Source references

- [Codex non-interactive execution, schema output and automation credentials](https://developers.openai.com/codex/non-interactive-mode/)
- [Codex security](https://developers.openai.com/codex/security/)
- [GitLab deployment approvals](https://docs.gitlab.com/ci/environments/deployment_approvals/)
- [GitLab merge request approvals](https://docs.gitlab.com/user/project/merge_requests/approvals/)
- [GitHub deployment environments and plan limitations](https://docs.github.com/en/actions/managing-workflow-runs-and-deployments/managing-deployments/managing-environments-for-deployment)
