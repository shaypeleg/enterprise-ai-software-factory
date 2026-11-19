"""Local browser setup and project skill installation."""

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import Refused, load, require, validate
from .project import configuration_path, detect, state_path
from .runtime import ROLES
from .workspace import setup_target


SOURCE = Path(__file__).resolve().parent.parent
ASSETS = Path(__file__).resolve().with_name("setup_ui")
PRESET = ".factory-settings.json"
PORTABLE = ("version", "git_url", "base_branch", "scm", "models", "checks", "protected_paths",
            "max_rounds", "agent_timeout_seconds", "check_timeout_seconds")


def managed_root():
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share").resolve()
    return data / "enterprise-ai-software-factory" / "bundles"


def managed_bundle(directory):
    """Keep an immutable copy independent of the temporary npm package cache."""
    paths = [SOURCE / name for name in ("config.example.json", "LICENSE", "NOTICE")]
    paths.extend((SOURCE / "factory").glob("*.py"))
    paths.extend(ASSETS / name for name in ("index.html", "style.css", "app.js"))
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


def initial(root):
    info = detect(root)
    defaults = json.loads((SOURCE / "config.example.json").read_text())
    defaults.update({key: info[key] for key in ("checkout", "git_url", "base_branch", "scm")})
    defaults.update(models={"default": ""}, checks=[])
    path = configuration_path(root)
    message = ""
    if path.is_file():
        defaults = load(path)
    else:
        try:
            preset = preset_path(root)
            if preset.is_file():
                require(preset.stat().st_size <= 65536, "Project preset is too large.")
                stored = json.loads(preset.read_text())
                require(isinstance(stored, dict) and stored.get("version") == 1, "Unsupported project preset.")
                candidate = dict(defaults, **{key: stored[key] for key in PORTABLE if key in stored})
                candidate["state_dir"] = str(state_path(root, "preview"))
                validate(candidate, path)
                defaults = candidate
                message = "Loaded the project preset. Review its endpoints and every check command before saving."
        except (Refused, OSError, ValueError, TypeError, AttributeError, KeyError):
            message = "The project preset could not be loaded. Review or replace it from this wizard."
    return {"config": {key: defaults[key] for key in PORTABLE}, "project": str(root),
            "origin": info["git_url"], "originHint": info["origin_hint"],
            "presetMessage": message, "hasConfig": path.is_file(), "roles": list(ROLES)}


def save(root, data):
    require(isinstance(data, dict) and data.get("consent") is True, "Confirm project access before saving.")
    config = {key: data["config"][key] for key in PORTABLE}
    config["scm"] = {key: config["scm"][key] for key in
                     ("provider", "api_url", "repository", "token_env", "queue_label")}
    require(set(config["models"]).issubset({"default", *ROLES}), "Unknown model role.")
    config.update(execution_mode="local-pilot", checkout=str(root),
                  state_dir=str(state_path(root, secrets.token_hex(8))),
                  release={"auto_merge": False, "deploy": False})
    path = configuration_path(root)
    require(config["git_url"] == detect(root)["git_url"],
            "The clone URL must match the detected Git origin (or its HTTPS equivalent).")
    require(config["scm"]["token_env"] not in ("CODEX_API_KEY", "OPENAI_API_KEY"),
            "Use separate environment variables for Git credentials and OpenAI credentials.")
    validate(config, path)
    if path.is_file():
        previous = load(path)
        candidate = dict(config, state_dir=previous["state_dir"])
        if candidate == previous:
            config = previous
    if data.get("share") is True:
        # Only portable values. Keep reviewable instructions separate from the executable config.
        save_json(preset_path(root), {key: config[key] for key in PORTABLE})
    save_json(path, config)
    return {"path": str(path), "shared": data.get("share") is True}


def server_for(root, repositories=None):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        timeout = 5

        def log_message(self, *args):
            pass  # Do not log requests or capability URLs.

        def reply(self, status, data, kind="application/json; charset=utf-8"):
            if not isinstance(data, bytes):
                data = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; "
                             "style-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
                             "base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            return hmac.compare_digest(self.headers.get("X-Factory-Session", "").encode(), token.encode())

        def valid_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

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
            if url.path == "/api/setup" and self.authorized():
                try:
                    result = initial(self.selected_repository(url.query))
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
            assets = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                      "/style.css": ("style.css", "text/css")}
            if self.path in assets:
                name, kind = assets[self.path]
                return self.reply(200, (ASSETS / name).read_bytes(), kind + "; charset=utf-8")
            self.reply(404, {"error": "Setup page unavailable."})

        def do_POST(self):
            url = urlsplit(self.path)
            if (url.path not in ("/api/setup", "/api/finish") or not self.valid_host() or not self.authorized() or
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
                result = save(path, json.loads(self.rfile.read(size)))
            except Refused as error:
                return self.reply(400, {"error": str(error)})
            except (ValueError, TypeError, KeyError, AttributeError, OSError):
                return self.reply(400, {"error": "Could not save settings. Check the inputs and file permissions."})
            self.server.configured.add(result["path"])
            self.server.saved = repositories is None
            self.reply(200, result)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 0.5
    server.saved = False
    server.configured = set()
    return server, f"http://127.0.0.1:{server.server_port}/#{token}"


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
