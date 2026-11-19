"""Local browser setup and project skill installation."""

import hashlib
import json
import os
import secrets
import tempfile
import time
import webbrowser
from http.server import HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .check_defaults import suggest
from .config import Refused, load, require, validate, validate_git_url, validate_scm
from .credentials import require_api_origin
from .local_ui import ASSETS as BROWSER_ASSETS, LocalHandler, private_url
from .model_catalog import available_models
from .project import configuration_path, detect, state_path
from .runtime import ROLES
from .scm import Provider, SCMHTTPError
from .workspace import setup_target, workspace_models, workspace_parent


SOURCE = Path(__file__).resolve().parent.parent
ASSETS = Path(__file__).resolve().with_name("setup_ui")
PRESET = ".factory-settings.json"
PORTABLE = ("version", "git_url", "base_branch", "scm", "models", "checks", "protected_paths",
            "max_rounds", "agent_timeout_seconds", "check_timeout_seconds")
SCM_FIELDS = ("provider", "api_url", "repository", "queue_label")


def managed_root():
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share").resolve()
    return data / "enterprise-ai-software-factory" / "bundles"


def managed_bundle(directory):
    """Keep an immutable copy independent of the temporary npm package cache."""
    paths = [SOURCE / name for name in ("config.example.json", "LICENSE", "NOTICE")]
    paths.extend((SOURCE / "factory").glob("*.py"))
    for surface in (ASSETS, ASSETS.with_name("dashboard_ui")):
        paths.extend(surface / filename for filename, _ in BROWSER_ASSETS.values())
    paths.extend(path for path in (SOURCE / "skills" / "factory").rglob("*")
                 if path.is_file() and "__pycache__" not in path.parts)
    files = [(path.relative_to(SOURCE), path.read_bytes()) for path in sorted(paths)]
    digest = hashlib.sha256()
    for path, content in files:
        digest.update(path.as_posix().encode() + b"\0" + hashlib.sha256(content).digest())
    bundle = directory / digest.hexdigest()
    if not bundle.is_dir():
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(dir=directory) as temp:
            staging = Path(temp) / "bundle"
            for path, content in files:
                destination = staging / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            try:
                staging.rename(bundle)
            except OSError:
                if not bundle.is_dir():
                    raise
    return bundle / "skills" / "factory"


def install_skill(root, managed=False):
    directory = root / ".agents" / "skills"
    require(not any(path.is_symlink() for path in (directory.parent, directory)),
            "Cannot install inside a symlinked .agents or skills directory.")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "factory"
    bundles = managed_root()
    source = managed_bundle(bundles) if managed else SOURCE / "skills" / "factory"
    if target.is_symlink() and target.resolve() == source:
        return target
    if managed and target.is_symlink():
        previous = target.resolve()
        if previous.parent.parent.parent == bundles and previous.parts[-2:] == ("skills", "factory"):
            with tempfile.TemporaryDirectory(dir=directory) as temp:
                replacement = Path(temp) / "factory"
                replacement.symlink_to(source, target_is_directory=True)
                replacement.replace(target)
            return target
    require(not target.exists() and not target.is_symlink(), "A different factory skill is already installed.")
    target.symlink_to(source, target_is_directory=True)
    return target


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def preset_path(root):
    path = root / PRESET
    require(not path.is_symlink() and (not path.exists() or path.is_file()),
            f"{PRESET} must be a regular project file.")
    return path


def initial(root, parent=None):
    info = detect(root)
    defaults = json.loads((SOURCE / "config.example.json").read_text())
    defaults.update({key: info[key] for key in ("checkout", "git_url", "base_branch", "scm")})
    defaults["scm"]["credential_source"] = "workspace"
    checks, suggested_checks_hint = suggest(root)
    checks_hint = suggested_checks_hint
    defaults["checks"] = checks
    path = configuration_path(root)
    message = ""
    from_preset = False
    if path.is_file():
        defaults = load(path)
        checks_hint = "Using the checks already saved for this checkout."
    else:
        try:
            preset = preset_path(root)
            if preset.is_file():
                require(preset.stat().st_size <= 65536, "Project preset is too large.")
                stored = json.loads(preset.read_text())
                require(isinstance(stored, dict) and stored.get("version") == 1, "Unsupported project preset.")
                candidate = dict(defaults, **{key: stored[key] for key in PORTABLE if key in stored})
                if "model_source" in stored:
                    candidate["model_source"] = stored["model_source"]
                candidate["state_dir"] = str(state_path(root, "preview"))
                validate(candidate, path)
                defaults = candidate
                from_preset = True
                if "checks" in stored:
                    checks_hint = "Using the checks from the project preset. Review them before saving."
                message = "Loaded the project preset. Review its endpoints and every check command before saving."
        except (Refused, OSError, ValueError, TypeError, AttributeError, KeyError):
            message = "The project preset could not be loaded. Review or replace it from this wizard."
    defaults["scm"] = selected_scm(defaults["scm"])
    parent_models = None
    if parent is not None:
        try:
            parent_models = workspace_models(parent)
        except (Refused, OSError, ValueError, KeyError):
            pass
    model_source = defaults.get("model_source", "repository")
    if parent_models and not path.is_file() and not from_preset:
        model_source = "workspace"
    return {"config": {key: defaults[key] for key in PORTABLE}, "project": str(root),
            "origin": info["git_url"], "originHint": info["origin_hint"],
            "presetMessage": message, "hasConfig": path.is_file(), "roles": list(ROLES),
            "modelSource": model_source, "parentName": parent.name if parent else None,
            "parentModels": parent_models,
            "checksHint": checks_hint, "suggestedChecks": checks, "suggestedChecksHint": suggested_checks_hint}


def connection_key(config):
    scm = config["scm"]
    source = scm.get("credential_source", "environment")
    return (config["git_url"], scm["provider"], scm["api_url"].rstrip("/"),
            scm["repository"], source, scm.get("token_env") if source == "environment" else None)


def check_clone(root, git_url, scm):
    require(git_url == detect(root)["git_url"],
            "The clone URL must match the detected Git origin (or its HTTPS equivalent).")
    require(scm.get("token_env") not in ("CODEX_API_KEY", "OPENAI_API_KEY"),
            "Use separate environment variables for Git credentials and OpenAI credentials.")


def selected_scm(scm):
    selected = {key: scm[key] for key in SCM_FIELDS}
    source = scm.get("credential_source", "environment")
    if source == "environment":
        selected["token_env"] = scm["token_env"]
    else:
        selected["credential_source"] = source
    return selected


def verify_connection(root, config):
    scm = selected_scm(config["scm"])
    git_url = config["git_url"]
    validate_scm(scm)
    validate_git_url(git_url, scm["repository"])
    check_clone(root, git_url, scm)
    require_api_origin(scm, git_url)
    if scm.get("credential_source", "environment") == "environment":
        require(os.environ.get(scm["token_env"]),
                f"{scm['token_env']} is missing. Set it in the launch environment, then reopen setup.")
    try:
        Provider(scm, checkout=root, git_url=git_url).test_issue_access()
    except SCMHTTPError as error:
        explanations = {
            401: "The server rejected the token. Check that it is valid and belongs to this Git host.",
            403: "The token lacks issue access, or the server blocked the request.",
            404: "Check the API base URL, the full repository path, and the token’s access to the project.",
            429: "The API rate limit was reached. Try again later.",
        }
        detail = explanations.get(error.status, "Check the API endpoint and your network access.")
        raise Refused(f"HTTP {error.status}. {detail}") from None
    except ValueError:
        raise Refused("The endpoint did not return a repository issues response. Check the API base URL.") from None
    return connection_key(config)


def save(root, data, parent=None):
    require(isinstance(data, dict) and data.get("consent") is True, "Confirm project access before saving.")
    config = {key: data["config"][key] for key in PORTABLE}
    config["scm"] = selected_scm(config["scm"])
    source = data["config"].get("model_source", "repository")
    require(source in ("repository", "workspace"), "invalid model source")
    if source == "workspace":
        require(root != parent, "The parent repository needs its own model settings.")
        workspace_models(parent)
        config.update(model_source=source, models={"default": "auto"})
    require(set(config["models"]).issubset({"default", *ROLES}), "Unknown model role.")
    config.update(execution_mode="local-pilot", checkout=str(root),
                  state_dir=str(state_path(root, secrets.token_hex(8))),
                  release={"auto_merge": False, "deploy": False})
    path = configuration_path(root)
    check_clone(root, config["git_url"], config["scm"])
    validate(config, path)
    if path.is_file():
        previous = load(path)
        candidate = dict(config, state_dir=previous["state_dir"])
        previous_scm = previous["scm"]
        default_name = f"FACTORY_{config['scm']['provider'].upper()}_TOKEN"
        had_default_name = (previous_scm.get("credential_source") == "workspace"
                            and previous_scm.get("token_env") == default_name)
        if (candidate == previous or (had_default_name and
                candidate == dict(previous, scm=selected_scm(previous_scm)))):
            # Preserve state for a legacy workspace setup that saved a harmless variable name.
            config = previous
    if data.get("share") is True:
        # Only portable values. Keep reviewable instructions separate from the executable config.
        preset = {key: config[key] for key in PORTABLE}
        preset["scm"] = selected_scm(config["scm"])
        if "model_source" in config:
            preset["model_source"] = config["model_source"]
        save_json(preset_path(root), preset)
    save_json(path, config)
    return {"path": str(path), "shared": data.get("share") is True}


def server_for(root, repositories=None):
    class Handler(LocalHandler):
        def selected_repository(self, query):
            selections = parse_qs(query).get("repository", [])
            if selections:
                require(repositories is not None and len(selections) == 1, "Invalid repository selection.")
                return setup_target(repositories, selections[0])
            return root

        def do_GET(self):
            if not self.valid_host():
                return self.reply(403, {"error": "Invalid setup host."})
            url = urlsplit(self.path)
            if url.path == "/api/models":
                if not self.authorized():
                    return self.reply(403, {"error": "Open the private setup URL printed in your terminal."})
                return self.reply(200, {"models": available_models()})
            if url.path == "/api/setup" and self.authorized():
                try:
                    path = self.selected_repository(url.query)
                    result = initial(path, workspace_parent(path, repositories))
                    if repositories is not None:
                        result["repositories"] = [
                            {"alias": alias, "available": path is not None,
                             "configured": bool(path and configuration_path(path).is_file())}
                            for alias, path in repositories.items()]
                        result["selected"] = next(alias for alias, path in repositories.items()
                                                  if path and str(path) == result["project"])
                    return self.reply(200, result)
                except (Refused, OSError, ValueError):
                    return self.reply(400, {"error": "Existing settings could not be loaded. Check the local configuration."})
            return self.asset(url.path, ASSETS)

        def do_POST(self):
            url = urlsplit(self.path)
            if (url.path not in ("/api/setup", "/api/connection", "/api/finish") or
                    not self.valid_host() or not self.authorized() or
                    self.headers.get("Origin") != f"http://127.0.0.1:{self.server.server_port}"):
                return self.reply(403, {"error": "Open the private setup URL printed in your terminal."})
            if self.server.saved:
                return self.reply(409, {"error": "Setup is already saved."})
            if url.path == "/api/finish":
                if repositories is None or not self.server.configured:
                    return self.reply(400, {"error": "Save a repository configuration before finishing."})
                self.server.saved = True
                return self.reply(200, {})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                require(self.headers.get_content_type() == "application/json" and 0 < size <= 65536,
                        "Expected setup JSON under 64 KB.")
                path = self.selected_repository(url.query)
                payload = json.loads(self.rfile.read(size))
                if url.path == "/api/connection":
                    self.server.verified.pop(path, None)
                    self.server.verified[path] = verify_connection(path, payload)
                    return self.reply(200, {"verified": True})
                verified = self.server.verified.get(path) == connection_key(payload["config"])
                require(verified or payload.get("allowUnverified") is True,
                        "Test this connection or explicitly choose to save it unverified.")
                result = save(path, payload, workspace_parent(path, repositories))
                result["verified"] = verified
            except Refused as error:
                return self.reply(400, {"error": str(error)})
            except (ValueError, TypeError, KeyError, AttributeError, OSError):
                return self.reply(400, {"error": "Could not complete this step. Check the configuration inputs."})
            self.server.configured.add(result["path"])
            self.server.saved = repositories is None
            self.reply(200, result)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 0.5
    server.saved = False
    server.configured = set()
    server.verified = {}
    return server, private_url(server)


def serve(root, open_browser=True, repositories=None):
    server, url = server_for(root, repositories)
    with server:
        print(f"Open factory setup: {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        deadline = time.monotonic() + 1800
        while not server.saved and time.monotonic() < deadline:
            server.handle_request()
        if server.saved:
            for path in sorted(server.configured):
                print(f"Configuration saved to {path}", flush=True)
        else:
            raise Refused("Setup expired. Run the setup command again.")
