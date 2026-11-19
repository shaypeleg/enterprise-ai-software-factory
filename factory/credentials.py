"""Resolve repository credentials in the controller, outside agent and check runs."""

import os
import shutil
import subprocess
from urllib.parse import urlsplit

from .config import Refused, require, validate_git_url
from .project import discovered_remote, git_read


def require_api_origin(scm, git_url):
    endpoint, clone = urlsplit(scm["api_url"]), urlsplit(git_url)
    endpoint_host = (endpoint.hostname, endpoint.port or 443)
    clone_host = (clone.hostname, clone.port or 443)
    trusted_host = (("api.github.com", 443)
                    if scm["provider"] == "github" and clone_host == ("github.com", 443)
                    else clone_host)
    require(endpoint.scheme == "https" and endpoint_host == trusted_host,
            "The factory only sends tokens to the Git origin’s HTTPS host or the official GitHub API in this mode.")


def credential_fields(argv, checkout, request):
    helper_env = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR",
                  "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
                  "GH_CONFIG_DIR", "GLAB_CONFIG_DIR", "DBUS_SESSION_BUS_ADDRESS", "DISPLAY", "WAYLAND_DISPLAY")
    env = {key: os.environ[key] for key in helper_env if key in os.environ}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/usr/bin/false", SSH_ASKPASS="/usr/bin/false",
               GCM_INTERACTIVE="Never", GH_PROMPT_DISABLED="1")
    try:
        result = subprocess.run(argv, cwd=checkout, input=request, capture_output=True, text=True,
                                env=env, timeout=8)
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return {}
    return (dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            if result.returncode == 0 else {})


def workspace_credential(checkout, git_url, provider):
    remote = urlsplit(git_url)
    fields = credential_fields(["git", "-c", "credential.interactive=false", "credential", "fill"],
                               checkout, f"url={git_url}\n\n")
    returned_path = fields.get("path")
    if (fields.get("protocol") == "https" and fields.get("host", "").lower() == remote.netloc.lower()
            and (returned_path is None or returned_path == remote.path.lstrip("/"))
            and fields.get("username") and fields.get("password")):
        return fields["username"], fields["password"]

    cli = "gh" if provider == "github" else "glab"
    # Finder-launched apps may not have Homebrew's directories on PATH.
    executable = shutil.which(cli) or shutil.which(cli, path="/opt/homebrew/bin:/usr/local/bin")
    login = f"{cli} auth login --hostname {remote.hostname}"
    if cli == "glab":
        login += " --use-keyring"
    require(executable, f"No reusable Git credential was found. Install {cli} and run `{login}`, or use a token variable.")
    fields = credential_fields([executable, "auth", "git-credential", "get"], checkout,
                               f"protocol=https\nhost={remote.netloc}\npath={remote.path.lstrip('/')}\n\n")
    require(fields.get("username") and fields.get("password"),
            f"No saved {cli} login for {remote.netloc}. Run `{login}`, then retry.")
    return fields["username"], fields["password"]


def repository_credential(scm, checkout=None, git_url=None):
    if scm.get("credential_source", "environment") == "workspace":
        require(checkout is not None and git_url is not None, "Workspace credentials require a target checkout.")
        validate_git_url(git_url, scm["repository"])
        origin = git_read(checkout, "config", "--get", "remote.origin.url")
        require(discovered_remote(origin) == git_url, "The Git origin changed. Reopen setup before reusing credentials.")
        require_api_origin(scm, git_url)
        return workspace_credential(checkout, git_url, scm["provider"])
    name = scm["token_env"]
    require(name not in ("CODEX_API_KEY", "OPENAI_API_KEY"), "Use separate Git and OpenAI credential variables.")
    token = os.environ.get(name)
    require(token, f"{name} is missing. Set it in the launch environment and reopen setup or rerun the command.")
    return ("x-access-token" if scm["provider"] == "github" else "oauth2"), token
