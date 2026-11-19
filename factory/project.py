"""Discover a Git target and locate its operator-owned factory settings."""

import hashlib
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from .config import Refused, require


def git_read(cwd, *args):
    try:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        raise Refused("Git project discovery timed out.") from None
    return result.stdout.strip() if result.returncode == 0 else ""


def project_root(cwd):
    root = git_read(cwd, "rev-parse", "--show-toplevel")
    require(root, "Open or specify the target Git checkout first.")
    return Path(root).resolve()


def discovered_remote(remote):
    """Return only credential-free HTTPS details. Never send raw origin to the browser."""
    shorthand = re.fullmatch(r"[\w.-]+@([\w.-]+):([\w./-]+)", remote)
    if shorthand:
        host, path = shorthand.groups()
        return "https://" + host.lower() + "/" + path
    try:
        url = urlsplit(remote)
        if url.scheme in ("ssh", "https") and url.hostname and not (
            url.password or url.query or url.fragment or (url.scheme == "https" and url.username)
        ) and re.fullmatch(r"/[\w./-]+", url.path):
            if url.scheme == "https":
                return remote
            return "https://" + url.hostname + url.path
    except ValueError:
        pass
    return ""


def configuration_path(root):
    home = Path.home()
    directory = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config").resolve()
    digest = hashlib.sha256(os.fsencode(root)).hexdigest()[:24]
    return directory / "enterprise-ai-software-factory" / "projects" / digest / "config.json"


def state_path(root, revision):
    home = Path.home()
    directory = Path(os.environ.get("XDG_STATE_HOME") or home / ".local" / "state").resolve()
    return directory / "enterprise-ai-software-factory" / "projects" / configuration_path(root).parent.name / revision


def detect(root):
    remote = git_read(root, "config", "--get", "remote.origin.url")
    git_url = discovered_remote(remote)
    url = urlsplit(git_url)
    if url.hostname == "github.com":
        provider, api_url = "github", "https://api.github.com"
    elif url.hostname and "gitlab" in url.hostname:
        provider, api_url = "gitlab", f"https://{url.netloc}/api/v4"
    else:
        provider, api_url = "", ""
    tracking = git_read(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    branch = (tracking[7:] if tracking.startswith("origin/") else
              git_read(root, "symbolic-ref", "--short", "HEAD"))
    return {"checkout": str(root), "git_url": git_url, "base_branch": branch,
            "scm": {"provider": provider, "api_url": api_url,
                    "repository": url.path.strip("/").removesuffix(".git"),
                    "token_env": ("FACTORY_" + provider.upper() + "_TOKEN") if provider else "",
                    "queue_label": "factory:ready"},
            "origin_hint": ("SSH origin detected. The factory will use HTTPS for the same project."
                            if git_url and not remote.startswith("https://") else
                            "Detected from Git origin." if git_url else
                            "No usable origin found. Configure a credential-free Git origin to run the factory.")}
