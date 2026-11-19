"""Discover repositories and route explicitly scoped jobs in a local workspace."""

import json
import os
import re
import subprocess
from pathlib import Path

from .config import Refused, load, require
from .project import configuration_path, git_read, project_root
from .scm import Provider


MANIFEST = ".factory-workspace.json"


def workspace_root(cwd):
    cwd = Path(cwd).resolve()
    for parent in (cwd, *cwd.parents):
        if (parent / MANIFEST).is_file():
            return parent
    root = project_root(cwd)
    while superproject := git_read(root, "rev-parse", "--show-superproject-working-tree"):
        root = Path(superproject).resolve()
    return root


def checked_repository(root, relative):
    path = root / relative
    require(not any(part in ("", ".", "..") for part in relative.split("/")),
            f"Repository path must be relative to the workspace: {relative!r}")
    require(not Path(relative).is_absolute() and path.resolve() == path,
            f"Repository path must stay inside the workspace without symlinks: {relative!r}")
    if not (path / ".git").exists():
        return None
    return path if project_root(path) == path else None


def submodule_paths(root):
    try:
        result = subprocess.run(["git", "ls-files", "--stage", "-z"], cwd=root,
                                check=True, capture_output=True, timeout=10)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise Refused(f"Could not list Git submodules in {root}.") from None
    for entry in result.stdout.split(b"\0"):
        if entry.startswith(b"160000 "):
            metadata, path = entry.split(b"\t", 1)
            if metadata.endswith(b" 0"):
                yield os.fsdecode(path)


def discover(root):
    manifest = root / MANIFEST
    repositories = {}
    if (root / ".git").exists() and project_root(root) == root:
        repositories["."] = root

    def discover_submodules(checkout):
        for relative in submodule_paths(checkout):
            alias = (checkout / relative).relative_to(root).as_posix()
            path = checked_repository(root, alias)
            repositories[alias] = path
            if path is not None:
                discover_submodules(path)

    if manifest.exists():
        require(not manifest.is_symlink() and manifest.is_file(), f"{MANIFEST} must be a regular file.")
        data = json.loads(manifest.read_text())
        require(isinstance(data, dict) and isinstance(data.get("repositories"), dict),
                f"{MANIFEST} needs a repositories object mapping names to relative paths.")
        seen = {root}
        for alias, relative in data["repositories"].items():
            require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", alias) and isinstance(relative, str),
                    "Use repository names with letters, numbers, underscores, slashes, dots or dashes.")
            path = checked_repository(root, relative)
            candidate = root / relative
            require(candidate not in seen, f"Checkout is listed more than once: {relative}")
            seen.add(candidate)
            repositories[alias] = path
    elif repositories:
        discover_submodules(root)
    require(repositories, "No repositories found. Add checkouts to .factory-workspace.json.")
    return repositories


def setup_target(repositories, alias):
    require(alias in repositories, f"Unknown repository {alias!r}. Run workspace list.")
    path = repositories[alias]
    require(path is not None, f"Repository {alias!r} has no local checkout. Initialize it first.")
    return path


def setup_start(repositories):
    available = [alias for alias, path in repositories.items() if path is not None]
    require(available, "Initialize at least one repository first.")
    return next((alias for alias in available if not configuration_path(repositories[alias]).is_file()), available[0])


def workspace_parent(checkout, repositories=None):
    checkout = Path(checkout)
    if repositories is None:
        root = workspace_root(checkout)
        if root == checkout:
            return None
        repositories = discover(root)
    parent = repositories.get(".")
    return parent if parent != checkout and checkout in repositories.values() else None


def workspace_models(parent):
    require(parent is not None and configuration_path(parent).is_file(),
            "Save the parent repository’s model settings in workspace setup first.")
    config = configured(parent)
    require(config.get("model_source") != "workspace", "The parent repository needs its own model settings.")
    return config["models"]


def configured(path):
    settings = configuration_path(path)
    require(settings.is_file(), f"Set up {path} in the browser before running jobs.")
    config = load(settings)
    require(Path(config["checkout"]) == path, f"Configuration points outside its checkout: {path}")
    return config


def parent_routes(body):
    lines = body.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == "```factory-routes"]
    if not starts:
        return []
    require(len(starts) == 1, "Parent issue must contain at most one factory-routes block.")
    block = lines[starts[0] + 1:]
    end = next((i for i, line in enumerate(block) if line.strip() == "```"), None)
    require(end is not None, "Close the parent issue's factory-routes block with three backticks.")
    routes = [line.strip() for line in block[:end] if line.strip()]
    require(routes and len(routes) <= 20, "Parent issue must list between 1 and 20 repository issues.")
    require(all(re.fullmatch(r"[^=\s]+=[1-9][0-9]*", route) and not route.startswith(".=")
                for route in routes), "Each route must use CHILD_REPOSITORY=ISSUE.")
    return routes


def expand_jobs(repositories, selectors, follow_parent):
    for selector in selectors:
        if not re.fullmatch(r"[1-9][0-9]*", selector):
            yield selector
            continue
        require("." in repositories, "This workspace has no parent Git checkout. Use REPOSITORY=ISSUE.")
        if follow_parent:
            parent = configured(setup_target(repositories, "."))
            issue = Provider(parent["scm"], checkout=parent.get("checkout"), git_url=parent.get("git_url")).issue(int(selector))
            routes = parent_routes(issue.get("body") or "")
            if routes:
                yield from routes
                continue
        yield ".=" + selector


def select_jobs(repositories, selectors, follow_parent=False):
    """Validate the entire batch before a job can begin."""
    seen, jobs = set(), []
    for selector in expand_jobs(repositories, selectors, follow_parent):
        alias, separator, number = selector.rpartition("=")
        require(separator and re.fullmatch(r"[1-9][0-9]*", number),
                f"Expected an issue number or REPOSITORY=ISSUE, got {selector!r}.")
        require((alias, number) not in seen, f"Job selected twice: {selector}")
        seen.add((alias, number))
        jobs.append((alias, int(number), configured(setup_target(repositories, alias))))
    return jobs
