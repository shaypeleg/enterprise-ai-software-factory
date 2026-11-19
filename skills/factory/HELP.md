# Factory help

The factory turns labeled GitHub or GitLab issues into reviewed code changes. It uses OpenAI models through Codex, runs project checks locally, and can open a draft pull request (PR) or merge request (MR) when you ask.

## Get started

1. In the target project, use `$factory setup` to open the browser wizard. Confirm the repository, choose a default OpenAI model and any role-specific overrides, and review the commands the factory should run as checks.
2. Use **Test connection** to confirm that the factory can read issues. Use the workspace’s Git credentials or the configured Git token environment variable. Agent runs use a separate `CODEX_API_KEY`.
3. Apply the configured queue label to an open issue. The default is `factory:ready`. Run `$factory queue` to list matching issues, then `$factory run 42`, replacing `42` with your issue number.

## Follow a run

1. **Intake and specification.** Assess the issue, then define the work and acceptance criteria.
2. **Build and checks.** Make changes in a separate Git worktree and run the configured checks.
3. **Review and handoff.** Run code and security reviews and, when intake flags a UI change, UI and UX reviews. Failed checks or requests for changes can send the job back to build within the configured limit. The handoff summarizes the evidence and marks the job `ready` when all gates pass. A job can stop early for missing information or human review.

Use `$factory status` for recorded jobs or `$factory dashboard` for a read-only live browser view of stages, checks, and reviews. If you close the tab, reopen the same private URL while the dashboard command is running. Run the command again if it stopped.

## Publish and release

Inspect the ready candidate and use `$factory publish 42` to push the candidate branch and open a draft PR/MR. Use `$factory run 42 --publish` to request both steps together. Staging and production releases require your own CI integration. The intended flow stages the verified candidate automatically and uses an authorized human’s manual merge to approve production.

## Work across repositories

- `$factory workspace list` shows repository aliases. Use `$factory workspace setup` to configure each checkout from a repository selector.
- `$factory workspace run 42` starts with a parent issue and follows its `factory-routes` block if present. Each routed issue needs its own repository’s queue label.
- `$factory workspace run backend=17 frontend=23` selects issues directly. Replace the aliases with those printed by `workspace list`. Use `$factory workspace publish backend=17` to publish a ready candidate for a specific repository.
- `$factory workspace dashboard` shows live jobs across the workspace. Use `$factory workspace status` for a terminal snapshot.

Open this guide in Codex with `$factory help`, or from an installed checkout run `python3 .agents/skills/factory/launch.py help`. Help works before setup and requires no credentials.
