# Factory help

The factory turns labeled GitHub or GitLab issues into reviewed code changes. It uses OpenAI models through Codex, runs project checks locally, and can open a draft pull request (PR) or merge request (MR) when you ask.

## Get started

1. In the target project, use `$factory setup` to open the browser wizard. Confirm the repository, use the `auto` model setting or choose a model for each role, and review the commands the factory should run as checks.
2. Use **Test connection** to confirm that the factory can read issues. Model runs use your local Codex sign-in or an explicitly set `CODEX_API_KEY`. If needed, check your account with `codex login status`.
3. Create the configured issue label if it is missing. The default is `factory:ready` (one colon). In GitLab, use **Manage → Labels → New label**. In GitHub, use **Issues → Labels → New label**. Apply the exact label to an open issue. One label is enough.
4. Run `$factory dashboard` and open the private browser link to watch the job. Use `$factory queue` to confirm the issue appears, then start it with `$factory run 42`, replacing `42` with your issue number. Labeling an issue adds it to the queue. Starting it is a separate command.

## Follow a run

1. **Intake and specification.** Assess the issue, then define the work and acceptance criteria.
2. **Build and checks.** Make changes in a separate Git worktree and run the configured checks.
3. **Review and handoff.** Run code and security reviews and, when intake flags a UI change, UI and UX reviews. Failed checks or requests for changes can send the job back to build within the configured limit. The handoff summarizes the evidence and marks the job `ready` when all gates pass. A job can stop early for missing information or human review.

Use `$factory status` for recorded jobs or `$factory dashboard` for the live stage board. Green tags show passing checks and reviews, red tags show failures or changes requested, and dotted tags show work in progress. Open a card for reports and issue links. If you close the tab, reopen the same private URL while the dashboard command is running.

## Publish and release

Inspect the ready candidate and use `$factory publish 42` to push the candidate branch and open a draft PR/MR. Use `$factory run 42 --publish` to request both steps together. Staging and production releases require your own CI integration. The intended flow stages the verified candidate automatically and uses an authorized human’s manual merge to approve production.

## Work across repositories

- `$factory workspace list` shows repository aliases. Use `$factory workspace setup` to configure each checkout. Set models on the parent once. Children use them by default, with a repository-level override available.
- `$factory workspace run 42` starts with a parent issue and follows its `factory-routes` block if present. Each routed issue needs its own repository’s queue label.
- `$factory workspace run backend=17 frontend=23` selects issues directly. Replace the aliases with those printed by `workspace list`. Use `$factory workspace publish backend=17` to publish a ready candidate for a specific repository.
- `$factory workspace dashboard` shows live jobs across the workspace. Use `$factory workspace status` for a terminal snapshot.

Open this guide in Codex with `$factory help`, or from an installed checkout run `python3 .agents/skills/factory/launch.py help`. Help works before setup and requires no credentials.
