import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from .config import Refused, load
from .engine import Engine, State
from .project import configuration_path, project_root
from .runtime import Codex
from .setup import install_skill, serve
from .scm import Provider


def main():
    parser = argparse.ArgumentParser(description="Codex factory pilot; never merges or deploys")
    parser.add_argument("--config", help="override the current project’s private configuration")
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="target Git checkout (default: current directory)")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "setup"):
        command = commands.add_parser(name)
        command.add_argument("--no-browser", action="store_true", help="print the private URL without opening it")
        if name == "install":
            command.add_argument("--managed", action="store_true", help=argparse.SUPPRESS)
    for name in ("validate", "queue", "status"):
        commands.add_parser(name)
    for name in ("run", "publish"):
        command = commands.add_parser(name)
        command.add_argument("issue", type=int)
        if name == "run":
            command.add_argument("--publish", action="store_true", help="publish a passing candidate as a draft PR/MR")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command in ("install", "setup"):
            if args.config:
                raise Refused("Browser setup writes the current project’s default private configuration.")
            root = project_root(args.project)
            if args.command == "install":
                target = install_skill(root, managed=args.managed)
                print(f"Installed Codex skill at {target}", flush=True)
            serve(root, open_browser=not args.no_browser)
            return
        path = Path(args.config) if args.config else configuration_path(project_root(args.project))
        if not path.is_file():
            raise Refused("No configuration saved. Run the factory setup command first.")
        config = load(path)
        provider = Provider(config["scm"])
        if args.command == "validate":
            print("Valid local-pilot configuration. No credentials or external services checked.")
            return
        if args.command == "queue":
            result = provider.queue()
        else:
            with State(config) as state:
                executable = shutil.which("codex")
                if args.command == "run":
                    if not executable:
                        raise Refused("install the OpenAI Codex CLI before running agents")
                    print("Local pilot: worktrees and environment filtering are not enterprise isolation.",
                          file=sys.stderr)
                engine = Engine(config, state, provider, Codex(config, executable))
                result = (state.all() if args.command == "status" else
                          engine.run(args.issue) if args.command == "run" else engine.publish(args.issue))
                if args.command == "run" and args.publish and result["stage"] == "ready":
                    result = engine.publish(args.issue)
        print(json.dumps(result, indent=2))
    except (Refused, KeyError, OSError, ValueError) as error:
        print("factory: " + str(error), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
