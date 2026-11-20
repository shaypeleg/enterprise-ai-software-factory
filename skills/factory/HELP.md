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
3. **Review and handoff.** Run code and security reviews. The full pipeline also routes UI and UX specialists when intake flags a UI change. The handoff summarizes the evidence and marks the job `ready` when checks and reviews pass.

Clear, low-risk changes can use the intake or workspace plan as their brief. These runs use fewer model calls and request low reasoning effort for build and code review when the selected model supports it. They still run every configured check and independent code and security reviews. Larger diffs or higher-risk findings trigger the full pipeline.

If a check fails, the builder gets the failed check and a short diagnostic excerpt, then the factory reruns the checks after repair. The configured round limit bounds retries. Use `$factory resume 42` to continue a stopped candidate after reviewing the blocker. The factory keeps its prior rounds and revalidates any saved candidate changes.

Use `$factory status` for recorded jobs or `$factory dashboard` for the live stage board. Green tags show passing checks and reviews, red tags show failures or changes requested, and dotted tags show work in progress. Open a card for reports and issue links. If you close the tab, reopen the same private URL while the dashboard command is running.

## Publish and release

Inspect the ready candidate and use `$factory publish 42` to push the candidate branch and open a draft PR/MR. Use `$factory run 42 --publish` to request both steps together. Staging and production releases require your own CI integration. The intended flow stages the verified candidate automatically and uses an authorized human’s manual merge to approve production.

## Work across repositories

- `$factory workspace list` shows repository aliases. Use `$factory workspace setup` to configure each checkout. Set models on the parent once. Children use them by default, with a repository-level override available.
- `$factory workspace run 42` plans and runs work across configured repositories from one labeled parent issue. It updates a status note on that issue with the repositories and outcomes. Open a run card to see the Codex agent IDs for its stages.
- `$factory workspace resume 42` resumes existing candidates and reuses the saved workspace plan. Use the same selectors as the original run.
- If the parent has an explicit `factory-routes` block, the factory uses those existing child issues and their queue labels.
- `$factory workspace publish 42` publishes the verified candidates for an automatic cascade when you ask.
- `$factory workspace run backend=17 frontend=23` selects issues directly. Replace the aliases with those printed by `workspace list`. Use `$factory workspace publish backend=17` to publish a ready candidate for a specific repository.
- `$factory workspace dashboard` shows live jobs across the workspace. Use `$factory workspace status` for a terminal snapshot.

Open this guide in Codex with `$factory help`, or from an installed checkout run `python3 .agents/skills/factory/launch.py help`. Help works before setup and requires no credentials.
