"""Read the current Codex model catalog without starting an agent."""

import json
import os
import re
import select
import subprocess
import tempfile
import time
from pathlib import Path

from .runtime import clean_env, codex_executable


def available_models():
    executable = codex_executable()
    if not executable:
        return []
    with tempfile.TemporaryDirectory(prefix="factory-models-") as temp:
        home = Path(temp)
        env = clean_env(home)
        if key := os.environ.get("CODEX_API_KEY"):
            env["CODEX_HOME"] = str(home / ".codex")
            env["CODEX_API_KEY"] = key
        else:
            env["CODEX_HOME"] = str(Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
                                    .expanduser().resolve())
        messages = [
            {"method": "initialize", "id": 1, "params": {
                "clientInfo": {"name": "factory-setup", "version": "0.1.0"}}},
            {"method": "initialized"},
            {"method": "model/list", "id": 2, "params": {"limit": 100}},
        ]
        command = [executable, "app-server", "--listen", "stdio://",
                   "--disable", "hooks", "--disable", "plugins",
                   "-c", 'model_provider="openai"', "-c", 'cli_auth_credentials_store="auto"']
        try:
            with subprocess.Popen(command, cwd=home, env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as server:
                try:
                    server.stdin.write(b"".join(json.dumps(message).encode() + b"\n" for message in messages))
                    server.stdin.flush()
                    return read_models(server.stdout)
                finally:
                    if server.poll() is None:
                        server.kill()
        except (OSError, ValueError):
            return []


def read_models(stream):
    deadline = time.monotonic() + 8
    pending = b""
    received = 0
    while time.monotonic() < deadline:
        if not select.select([stream], [], [], max(0, deadline - time.monotonic()))[0]:
            break
        chunk = os.read(stream.fileno(), 65536)
        received += len(chunk)
        if not chunk or received > 1024 * 1024:
            break
        *lines, pending = (pending + chunk).split(b"\n")
        for line in lines:
            response = json.loads(line)
            if isinstance(response, dict) and response.get("id") == 2:
                result = response.get("result")
                models = result.get("data") if isinstance(result, dict) else None
                if not isinstance(models, list):
                    return []
                catalog = {}
                for entry in models[:100]:
                    if not isinstance(entry, dict) or entry.get("hidden"):
                        continue
                    name = entry.get("model")
                    if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name):
                        display = entry.get("displayName")
                        catalog[name] = {"id": name, "label": display[:128] if isinstance(display, str) and display else name}
                return list(catalog.values())
    return []
