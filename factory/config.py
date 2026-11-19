"""Validate operator-owned configuration before executing anything."""

import json
import re
from pathlib import Path
from urllib.parse import urlsplit


class Refused(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Refused(message)


def load(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text())
    require(config.get("version") == 1, "unsupported configuration version")
    require(config.get("execution_mode") == "local-pilot", "only local-pilot is implemented")
    scm = config["scm"]
    require(scm["provider"] in ("github", "gitlab"), "provider must be github or gitlab")
    url = urlsplit(scm["api_url"])
    require(url.scheme == "https" and url.hostname and not (
        url.username or url.password or url.query or url.fragment
    ), "api_url must be HTTPS without credentials, query, or fragment")
    require(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", scm["token_env"]), "invalid token_env")
    require(re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", scm["repository"]),
            "repository must be an explicit owner/project or group/subgroup/project")
    require(all(part not in (".", "..") for part in scm["repository"].split("/")),
            "invalid repository path")
    require(isinstance(scm["queue_label"], str) and scm["queue_label"].strip(), "queue_label is required")
    require(scm["queue_label"].isascii() and "," not in scm["queue_label"], "invalid queue label")
    for key in ("checkout", "state_dir"):
        require(Path(config[key]).is_absolute(), key + " must be absolute")
        config[key] = str(Path(config[key]).resolve())
    checkout, state = Path(config["checkout"]), Path(config["state_dir"])
    require(checkout.is_dir(), "checkout does not exist")
    require(state != checkout and checkout not in state.parents,
            "state_dir must be outside the target checkout")
    require(path != checkout and checkout not in path.parents,
            "configuration must live outside the target checkout")
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", config["base_branch"])
            and ".." not in config["base_branch"], "invalid base branch")
    remote = urlsplit(config["git_url"])
    require(remote.scheme == "https" and remote.hostname and not (
        remote.username or remote.password or remote.query or remote.fragment
    ) and remote.path.strip("/").removesuffix(".git") == scm["repository"],
            "git_url must be the explicit HTTPS clone URL of scm.repository")
    for key in ("max_rounds", "agent_timeout_seconds", "check_timeout_seconds"):
        require(type(config[key]) is int and 1 <= config[key] <= 3600, "invalid " + key)
    require(config["max_rounds"] <= 10, "max_rounds cannot exceed 10")
    require(isinstance(config["checks"], list) and config["checks"], "real checks are required")
    for command in config["checks"]:
        require(isinstance(command, list) and command and all(
            isinstance(arg, str) and arg and "\0" not in arg for arg in command
        ), "checks must be nonempty argv arrays, never shell strings")
    require(isinstance(config["protected_paths"], list) and all(
        isinstance(p, str) and p for p in config["protected_paths"]
    ), "protected_paths must be a list of patterns")
    models = config["models"]
    require(isinstance(models, dict) and "default" in models, "models.default is required")
    for model in models.values():
        require(isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", model)
                and not model.startswith("REPLACE"), "select an available OpenAI model explicitly")
    require(config.get("release") == {"auto_merge": False, "deploy": False},
            "this runner cannot merge or deploy")
    return config
