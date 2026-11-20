import argparse
import json
import os
import sys
from pathlib import Path

from .cascade import jobs_for_command, update_ticket
from .config import Refused, load, require
from .dashboard import serve_dashboard
from .engine import Engine, State
from .project import configuration_path, project_root
from .runtime import Codex, codex_executable
from .setup import install_skill, serve
from .scm import Provider
from .workspace import configured, discover, setup_start, setup_target, workspace_root


def operate(config, command, issue=None, publish=False, route=None):
    provider = Provider(config["scm"], checkout=config["checkout"], git_url=config["git_url"])
    if command == "queue":
        return provider.queue()
    with State(config) as state:
        executable = codex_executable() if command in ("run", "resume") else None
        if command in ("run", "resume"):
            require(executable, "install the OpenAI Codex CLI before running agents")
            print("Local pilot: worktrees and environment filtering are not enterprise isolation.",
                  file=sys.stderr)
        engine = Engine(config, state, provider, Codex(config, executable))
        if command == "status":
            return state.all()
        if command == "publish":
            return engine.publish(issue, route=route)
        result = engine.run(issue, route=route, resume=command == "resume")
        return engine.publish(issue, route=route) if publish and result["stage"] == "ready" else result


def report(result):
    print(json.dumps(result, indent=2))


def workspace_command(args):
    root = workspace_root(args.project)
    repositories = discover(root)
    action = args.action
    if action == "setup":
        selected = args.repository or setup_start(repositories)
        serve(setup_target(repositories, selected), open_browser=not args.no_browser,
              repositories=repositories)
        return
    if action == "dashboard":
        settings = {alias: configuration_path(path) if path else None for alias, path in repositories.items()}
        serve_dashboard(root.name, settings, open_browser=not args.no_browser)
        return
    if action == "list":
        report({alias: {"checkout": str(path) if path else None,
                        "configured": bool(path and configuration_path(path).is_file())}
                for alias, path in repositories.items()})
        return
    path_results = {}
    if action in ("run", "resume", "publish"):
        jobs = jobs_for_command(repositories, args.jobs, action)
        if action == "publish":
            for alias, issue, config, route in jobs:
                with State(config) as state:
                    job = state.get(-issue if route else issue)
                    require(job and job["stage"] in ("ready", "published"), f"{alias} issue #{issue} is not ready to publish.")
    else:
        jobs = []
        for alias, path in repositories.items():
            if path is None:
                path_results[alias] = {"status": "checkout-required"}
            elif not configuration_path(path).is_file():
                path_results[alias] = {"status": "setup-required"}
            else:
                jobs.append((alias, None, configured(path), None))
    results = []
    failed = False
    notes = {}
    for alias, issue, config, route in jobs:
        row = {"repository": alias}
        if issue is not None:
            row["issue"] = issue
        if route:
            row["parent_issue_url"] = route.source["url"]
            note = notes.setdefault((route.parent_git_url, issue), (route, []))[1]
            note.append((config["scm"]["repository"], row))
        try:
            route_option = {"route": route} if route else {}
            row["result"] = operate(config, action, issue, **route_option)
        except (Refused, OSError, KeyError, ValueError) as error:
            row["error"] = str(error)
            failed = True
        if issue is None:
            path_results[alias] = {key: value for key, value in row.items() if key != "repository"}
        else:
            results.append(row)
        if failed and action == "publish":
            break  # Preserve earlier requests and report exactly what was attempted.
    for route, rows in notes.values():
        if action == "publish" and failed:
            continue  # Keep the complete run note when publishing stopped partway through the batch.
        try:
            update_ticket(route, [(repo, row.get("result", {}).get("stage")) for repo, row in rows])
        except (Refused, OSError, KeyError, ValueError) as error:
            for _, row in rows:
                row["parent_note_error"] = str(error)
            failed = True
    report(results if action in ("run", "resume", "publish") else path_results)
    if failed:
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Codex factory pilot; never merges or deploys")
    parser.add_argument("--config", help="override the current project’s private configuration")
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="target Git checkout (default: current directory)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("help", help="explain setup, the job workflow, and publishing")
    for name in ("install", "setup", "dashboard"):
        command = commands.add_parser(name)
        command.add_argument("--no-browser", action="store_true", help="print the private URL without opening it")
        if name == "install":
            command.add_argument("--managed", action="store_true", help=argparse.SUPPRESS)
            command.add_argument("--workspace", action="store_true", help="install for a workspace with a repository selector")
            command.add_argument("--skip-setup", action="store_true", help="install or update the skill without opening setup")
    for name in ("validate", "queue", "status"):
        commands.add_parser(name)
    for name in ("run", "resume", "publish"):
        command = commands.add_parser(name)
        command.add_argument("issue", type=int)
        if name == "run":
            command.add_argument("--publish", action="store_true", help="publish a passing candidate as a draft PR/MR")
    workspace = commands.add_parser("workspace", help="work with multiple Git checkouts")
    actions = workspace.add_subparsers(dest="action", required=True)
    for action in ("list", "queue", "status"):
        actions.add_parser(action)
    actions.add_parser("dashboard").add_argument("--no-browser", action="store_true")
    setup = actions.add_parser("setup")
    setup.add_argument("repository", nargs="?")
    setup.add_argument("--no-browser", action="store_true")
    for action in ("run", "resume", "publish"):
        operation = actions.add_parser(action)
        operation.add_argument("jobs", nargs="+", metavar="ISSUE|REPOSITORY=ISSUE")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "help":
            guide = Path(__file__).resolve().parents[1] / "skills" / "factory" / "HELP.md"
            print(guide.read_text(encoding="utf-8"), end="")
            return
        if args.command == "workspace":
            require(not args.config, "Workspace commands use each checkout’s saved configuration.")
            workspace_command(args)
            return
        if args.command in ("install", "setup"):
            if args.config:
                raise Refused("Browser setup writes the current project’s default private configuration.")
            repositories = None
            if getattr(args, "workspace", False):
                root = workspace_root(args.project)
                repositories = discover(root)
                selected = setup_target(repositories, setup_start(repositories))
            else:
                root = selected = project_root(args.project)
            if args.command == "install":
                target = install_skill(root, managed=args.managed)
                print(f"Installed Codex skill at {target}", flush=True)
                if args.skip_setup:
                    return
            serve(selected, open_browser=not args.no_browser, repositories=repositories)
            return
        if args.command == "dashboard":
            root = project_root(args.project)
            settings = Path(args.config).resolve() if args.config else configuration_path(root)
            serve_dashboard(root.name, {root.name: settings}, open_browser=not args.no_browser)
            return
        path = Path(args.config) if args.config else configuration_path(project_root(args.project))
        if not path.is_file():
            raise Refused("No configuration saved. Run the factory setup command first.")
        config = load(path)
        if args.command == "validate":
            print("Valid local-pilot configuration. No credentials or external services checked.")
            return
        report(operate(config, args.command, getattr(args, "issue", None), getattr(args, "publish", False)))
    except (Refused, KeyError, OSError, ValueError) as error:
        print("factory: " + str(error), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("factory: interrupted. Use resume to continue an existing run.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
