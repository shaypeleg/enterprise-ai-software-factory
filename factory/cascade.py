"""Plan and run local repository tasks that share one upstream issue."""

import hashlib
import re
import sys
from dataclasses import dataclass

from . import workspace
from .config import Refused, require
from .engine import State
from .project import configuration_path
from .runtime import Codex, codex_executable, recorded_agent_id


@dataclass(frozen=True)
class RoutedTask:
    provider: object
    source: dict
    parent_git_url: str
    scope: str
    planner_id: str = None
    quick: bool = False

    @property
    def branch(self):
        namespace = hashlib.sha256(self.parent_git_url.encode()).hexdigest()[:16]
        return f"factory/workspace-{namespace}-issue-{self.source['number']}"

    def current_issue(self):
        current = self.provider.issue(self.source["number"])
        require(current == self.source, "Parent issue changed after planning. Use a fresh factory state to replan.")
        return dict(current, body=f"Repository task\n\n{self.scope}\n\nParent issue\n\n{current['body']}",
                    workspace_parent=self.parent_git_url)


def validated_tasks(repositories, tasks):
    require(isinstance(tasks, list) and 1 <= len(tasks) <= 20,
            "Workspace plan must contain between 1 and 20 repository tasks.")
    seen = set()
    for task in tasks:
        require(isinstance(task, dict) and isinstance(task.get("repository"), str)
                and isinstance(task.get("scope"), str) and 0 < len(task["scope"].strip()) <= 8000,
                "Workspace plan contains an invalid task.")
        alias = task["repository"]
        require(alias not in seen, f"Workspace plan lists {alias!r} more than once.")
        seen.add(alias)
        checkout = workspace.setup_target(repositories, alias)
        config = workspace.configured(checkout)
        yield (alias, config, task["scope"])


def cascade_jobs(repositories, parent, provider, issue, command):
    number = issue["number"]
    require(0 < number <= 2**63 - 1, "Invalid parent issue number.")
    with State(parent) as state:
        plan = state.workspace_plan(number)
        if plan is None:
            if command != "run":
                return None
            require(issue["body"].strip(), "Workspace planning needs information. Add the scope to the parent issue description.")
            executable = codex_executable()
            require(executable, "Install the OpenAI Codex CLI before planning workspace work.")
            root = repositories["."]
            context = {"issue": issue, "repositories": {
                alias: {"path": str(path.relative_to(root)) if path else None,
                        "configured": bool(path and configuration_path(path).is_file())}
                for alias, path in repositories.items()}}
            print(f"Planning workspace work for parent issue #{number}.", file=sys.stderr, flush=True)
            artifacts = state.root / "workspace-plans" / str(number)
            result = Codex(parent, executable).plan_workspace(root, context, artifacts)
            report = result.get("report")
            require(isinstance(report, str) and report.strip(), "Workspace planner returned an empty report.")
            if result.get("verdict") == "NEEDS-INFO":
                raise Refused("Workspace planning needs information. " + report[:2000])
            require(result.get("verdict") in ("READY", "QUICK"), "Workspace planner returned an invalid verdict.")
            selected = list(validated_tasks(repositories, result.get("tasks")))
            plan = {"issue": issue, "report": report,
                    "quick": result["verdict"] == "QUICK" and len(selected) == 1,
                    "planner_id": recorded_agent_id(artifacts / "workspace-plan.jsonl"),
                    "tasks": [{"repository": alias, "scope": scope, "git_url": config["git_url"],
                               "checkout": config["checkout"]} for alias, config, scope in selected]}
            state.save_workspace_plan(number, plan)
        else:
            require(plan["issue"] == issue, "Parent issue changed after planning. Use a fresh factory state to replan.")
            selected = list(validated_tasks(repositories, plan["tasks"]))
            for (_, config, _), task in zip(selected, plan["tasks"]):
                require((config["git_url"], config["checkout"]) == (task["git_url"], task["checkout"]),
                        "A planned repository changed. Use a fresh factory state to replan.")
    return [(alias, number, config, RoutedTask(provider, issue, parent["git_url"], scope, plan["planner_id"],
                                             quick=plan.get("quick", False)))
            for alias, config, scope in selected]


def jobs_for_command(repositories, selectors, command):
    jobs = []
    for selector in selectors:
        if re.fullmatch(r"[1-9][0-9]*", selector) and "." in repositories and len(repositories) > 1:
            parent = workspace.configured(workspace.setup_target(repositories, "."))
            provider = workspace.Provider(parent["scm"], checkout=parent.get("checkout"), git_url=parent.get("git_url"))
            issue = provider.issue(int(selector))
            explicit = workspace.parent_routes(issue.get("body") or "")
            if explicit and command in ("run", "resume"):
                jobs.extend((*row, None) for row in workspace.select_jobs(repositories, explicit))
                continue
            if not explicit:
                cascade = cascade_jobs(repositories, parent, provider, issue, command)
                if cascade is not None:
                    jobs.extend(cascade)
                    continue
        jobs.extend((*row, None) for row in workspace.select_jobs(
            repositories, [selector], follow_parent=command in ("run", "resume")))
    seen = set()
    for alias, number, _, route in jobs:
        key = (alias, -number if route else number)
        require(key not in seen, f"Job selected twice: {alias} #{number}.")
        seen.add(key)
    return jobs


RESULT_LABELS = {
    "ready": "Checks and reviews passed; local candidate ready",
    "published": "Draft PR/MR published",
    "needs-info": "Needs more information",
    "needs-human": "Needs human review",
    "rejected": "Rejected at intake",
}


def update_ticket(route, results):
    route.current_issue()
    lines = ["### Factory workspace cascade", "", f"Parent issue #{route.source['number']}", ""]
    for repo, stage in results:
        status = RESULT_LABELS.get(stage, "Stopped with an error" if stage is None else "Incomplete")
        lines.append(f"- `{repo}` — {status}")
    route.provider.update_workspace_note(route.source["number"], "\n".join(lines))
