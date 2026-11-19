# What changes from the Claude reference

The reference is a local, session-orchestrated factory rather than a hosted
enterprise service. Its useful ideas carry across: issue intake, scoped specs,
worktrees, deterministic test gates, independent reviews, bounded revision
rounds, persisted state and one request per issue.

The implementation is not portable by renaming `.claude` to `.codex`:

- `.claude/skills/factory/SKILL.md` depends on Claude's Agent/SendMessage tools
  and background-agent lifecycle. Python now owns stage sequencing and calls
  the documented `codex exec` interface.
- Agent frontmatter contains Claude model names, tool permissions and a Claude
  stop hook. The port uses configurable OpenAI model IDs, explicit CLI sandbox
  modes, structured schema results, and an independent deterministic check stage.
  It does not treat prompt/frontmatter restrictions as OS enforcement.
- `factory/bin/job` invokes `gh` directly for issue lists, comments, labels and
  PR merges. New adapters use explicit GitHub/GitLab APIs for the supported
  queue, issue and draft-request operations; the engine is provider-neutral.
- The original approver can return `APPROVE`, and `job merge` then syncs,
  retests, pushes and runs `gh pr merge`. The port replaces this with a handoff
  recommendation and has no merge/approval endpoint. Your human production
  authorization belongs to branch protection and trusted deployment CI.
- The original factory never deploys. Automatic staging and production
  promotion are new integrations, specified in ENTERPRISE.md; they were not
  predefined by the upstream project.
- The original gate skips protected additions. The new gate blocks additions,
  modifications, deletions and both sides of renames. It checks the local
  candidate and the actual published Git blobs against the reviewed fingerprint.
- Cost parsing in `job` inspects undocumented Claude transcript files. This
  port stores Codex JSONL events as private artifacts instead. It does not yet
  aggregate usage/cost or enforce a token/spend budget.
- The original dashboard, issue discussion/feedback commands, automatic
  needs-info resumption, concurrent agents/jobs and remote label/comment updates
  are not included in this pilot. Parked jobs require operator intervention;
  add these as separate features after validating the core against real providers.

This standalone repository contains the Codex adaptation. The original Claude
implementation and examples remain in the
[upstream repository](https://github.com/zazencodes/zazencodes-season-3/tree/main/src/software-factory-claude-code-v2).
GPLv3 licensing and upstream attribution are retained.

Before a real deployment, choose the hosting system for the controller and
workers, artifact registry and staging/production targets; then implement one
deployment adapter against the explicit release contract. Keep both SCM
adapters even if the first rollout uses only one provider.
