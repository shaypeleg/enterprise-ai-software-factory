"""Suggest a small check plan from repository files without executing commands."""

import json
import re
from pathlib import Path

from .project import git_read


def local_file(root, name):
    path = root / name
    return path if path.is_file() and path.resolve() == path else None


def node_checks(root):
    manifest = local_file(root, "package.json")
    if not manifest:
        return []
    try:
        scripts = json.loads(manifest.read_text()).get("scripts", {})
    except (OSError, ValueError, AttributeError):
        return []
    if not isinstance(scripts, dict):
        return []
    names = [name for name in ("test", "lint", "typecheck")
             if isinstance(scripts.get(name), str) and scripts[name].strip()
             and "no test specified" not in scripts[name].lower()]
    if not names:
        return []
    if local_file(root, "pnpm-lock.yaml"):
        return [["pnpm", "install", "--frozen-lockfile"], *[["pnpm", "run", name] for name in names]]
    if local_file(root, "package-lock.json") or local_file(root, "npm-shrinkwrap.json"):
        return [["npm", "ci"], *[["npm", "run", name] for name in names]]
    return []


def python_ci_checks(root):
    requirements = "scripts/ci/requirements.txt"
    ci = local_file(root, ".gitlab-ci.yml")
    if not (ci and local_file(root, requirements) and (root / "tests" / "ci").is_dir()
            and git_read(root, "-c", "core.excludesFile=/dev/null", "check-ignore", ".venv/pip")):
        return []
    try:
        text = ci.read_text()
    except (OSError, ValueError):
        return []
    if not re.search(r"(?m)^\s*-\s+python(?:3(?:\.\d+)?)? -m unittest discover -s tests/ci(?: -v)?\s*$", text):
        return []
    version = re.search(r"(?m)^\s*image:\s*['\"]?python:(3\.\d+)\b", text)
    python = "python" + version[1] if version else "python3"
    env = ".venv/bin/python"
    commands = [[python, "-m", "venv", ".venv"],
                [env, "-m", "pip", "install", "-r", requirements],
                [env, "-m", "unittest", "discover", "-s", "tests/ci", "-v"]]
    for script in re.findall(r"(?m)^\s*-\s+python(?:3(?:\.\d+)?)? (scripts/test_[A-Za-z0-9_]+\.py)\s*$", text):
        if local_file(root, script) and [env, script] not in commands:
            commands.append([env, script])
    return commands


def suggest(root: Path):
    checks = python_ci_checks(root)
    if checks:
        return checks, "Suggested from the Python checks in .gitlab-ci.yml. Review the commands before saving."
    checks = node_checks(root)
    if checks:
        return checks, "Suggested from the package.json scripts and lockfile. Review the commands before saving."
    return [["git", "diff", "--check", "HEAD", "--"]], (
        "Starting with a Git whitespace check. Add this repository’s tests or lint commands when available.")
