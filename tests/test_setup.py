import io
import json
import os
import subprocess
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from factory.config import Refused, load
from factory.project import configuration_path, detect, discovered_remote
from factory.scm import SCMHTTPError
from factory.setup import PRESET, connection_key, initial, install_skill, save, server_for, verify_connection


class ProjectSetupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        self.root = base / "app"
        self.root.mkdir()
        environment = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(base / "config"),
                                              "XDG_STATE_HOME": str(base / "state"),
                                              "XDG_DATA_HOME": str(base / "data")})
        environment.start()
        self.addCleanup(environment.stop)
        self.git("init", "-b", "main")
        self.git("remote", "add", "origin", "git@gitlab.example.com:team/platform/app.git")

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    def payload(self):
        config = initial(self.root)["config"]
        config.update(models={"default": "test-model", "security": "security-model"},
                      checks=[["python3", "-m", "unittest"]])
        return {"config": config, "consent": True, "share": True}

    def test_detects_gitlab_ssh_remote_and_ignores_inline_credentials(self):
        details = detect(self.root)
        self.assertEqual(details["scm"]["repository"], "team/platform/app")
        self.assertEqual(details["scm"]["api_url"], "https://gitlab.example.com/api/v4")
        self.assertEqual(details["base_branch"], "main")
        self.assertEqual(discovered_remote("https://user:secret@github.com/team/app.git"), "")

    def test_consent_required_and_preset_stays_inert(self):
        payload = self.payload()
        with self.assertRaisesRegex(Refused, "Confirm project access"):
            save(self.root, dict(payload, consent=False))
        self.assertFalse(configuration_path(self.root).exists())
        save(self.root, payload)
        local = configuration_path(self.root)
        first = load(local)
        preset = json.loads((self.root / PRESET).read_text())
        self.assertNotIn("checkout", preset)
        self.assertNotIn("state_dir", preset)
        self.assertEqual(preset["models"]["security"], "security-model")
        save(self.root, payload)
        self.assertEqual(load(local)["state_dir"], first["state_dir"])
        payload["config"]["models"]["security"] = "other-model"
        save(self.root, payload)
        self.assertNotEqual(load(local)["state_dir"], first["state_dir"])
        local.unlink()
        self.assertEqual(initial(self.root)["config"]["models"]["security"], "other-model")
        self.assertFalse(configuration_path(self.root).exists())

    def test_skill_install_does_not_overwrite_other_skill(self):
        skill = install_skill(self.root)
        self.assertTrue((skill / "SKILL.md").is_file())
        self.assertEqual(install_skill(self.root), skill)
        skill.unlink()
        skill.mkdir()
        with self.assertRaisesRegex(Refused, "different factory skill"):
            install_skill(self.root)

    def test_managed_skill_stores_runtime_outside_install_source(self):
        skill = install_skill(self.root, managed=True)
        runtime = skill.resolve().parents[1]
        self.assertTrue(runtime.is_relative_to(Path(os.environ["XDG_DATA_HOME"])))
        self.assertTrue((runtime / "factory" / "setup_ui" / "index.html").is_file())
        self.assertTrue((runtime / "LICENSE").is_file())
        result = subprocess.run(["python3", str(skill / "launch.py"), "--help"], cwd=self.root,
                                capture_output=True, text=True, check=True)
        self.assertIn("setup", result.stdout)
        self.assertEqual(install_skill(self.root, managed=True).resolve(), skill.resolve())

    def test_cannot_save_a_different_target_or_use_model_token_for_scm(self):
        payload = self.payload()
        payload["config"]["git_url"] = "https://example.com/team/platform/app.git"
        with self.assertRaisesRegex(Refused, "detected Git origin"):
            save(self.root, payload)
        payload = self.payload()
        payload["config"]["scm"]["token_env"] = "CODEX_API_KEY"
        with self.assertRaisesRegex(Refused, "separate environment variables"):
            save(self.root, payload)

    def test_connection_checks_project_issues_and_keeps_secrets_out_of_result(self):
        config = self.payload()["config"]
        with patch.dict(os.environ, {"FACTORY_GITLAB_TOKEN": "dummy-token"}), patch("factory.scm.build_opener") as opener:
            open_request = opener.return_value.open
            open_request.return_value = io.BytesIO(b"[]")
            evidence = verify_connection(self.root, config)
            request, = open_request.call_args.args
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.full_url,
                             "https://gitlab.example.com/api/v4/projects/team%2Fplatform%2Fapp/issues?per_page=1")
            self.assertEqual(request.get_header("Private-token"), "dummy-token")
            self.assertEqual(evidence, connection_key(config))
            self.assertNotIn("dummy-token", evidence)
        with patch.dict(os.environ, {"FACTORY_GITLAB_TOKEN": "dummy-token"}), patch("factory.setup.Provider") as provider:
            provider.return_value.test_issue_access.side_effect = SCMHTTPError(404)
            with self.assertRaisesRegex(Refused, "HTTP 404.*full repository path"):
                verify_connection(self.root, config)
            provider.return_value.test_issue_access.side_effect = SCMHTTPError(401)
            with self.assertRaisesRegex(Refused, "HTTP 401.*rejected the token"):
                verify_connection(self.root, config)

    def test_connection_refuses_to_send_token_to_a_different_origin(self):
        config = self.payload()["config"]
        with patch.dict(os.environ, {"FACTORY_GITLAB_TOKEN": "dummy-token"}), patch("factory.setup.Provider") as provider:
            for endpoint in ("https://other.example.com/api/v4", "https://gitlab.example.com:8443/api/v4"):
                with self.subTest(endpoint=endpoint):
                    config["scm"]["api_url"] = endpoint
                    with self.assertRaisesRegex(Refused, "only sends tokens"):
                        verify_connection(self.root, config)
            provider.assert_not_called()

    def test_connection_uses_official_github_api_and_reports_missing_token(self):
        self.git("remote", "set-url", "origin", "https://github.com/team/app.git")
        config = self.payload()["config"]
        with patch.dict(os.environ, {"FACTORY_GITHUB_TOKEN": ""}), patch("factory.setup.Provider") as provider:
            with self.assertRaisesRegex(Refused, "FACTORY_GITHUB_TOKEN is missing"):
                verify_connection(self.root, config)
            provider.assert_not_called()
        with patch.dict(os.environ, {"FACTORY_GITHUB_TOKEN": "dummy-token"}), patch("factory.setup.Provider") as provider:
            verify_connection(self.root, config)
            provider.return_value.test_issue_access.assert_called_once_with()

    def test_verified_save_requires_matching_connection_or_explicit_skip(self):
        with patch("factory.setup.HTTPServer") as factory_server:
            factory_server.return_value.server_port = 42000
            server, url = server_for(self.root)
            handler_type = factory_server.call_args.args[1]

        def post(path, payload):
            data = json.dumps(payload).encode()
            handler = handler_type.__new__(handler_type)
            handler.path = path
            handler.server = server
            handler.headers = Message()
            handler.headers["Origin"] = "http://127.0.0.1:42000"
            handler.headers["Host"] = "127.0.0.1:42000"
            handler.headers["X-Factory-Session"] = url.split("#", 1)[1]
            handler.headers["Content-Type"] = "application/json"
            handler.headers["Content-Length"] = str(len(data))
            handler.rfile = io.BytesIO(data)
            with patch.object(handler, "reply") as reply:
                handler.do_POST()
                return reply.call_args.args

        payload = self.payload()
        self.assertEqual(post("/api/setup", payload)[0], 400)
        with patch("factory.setup.verify_connection", side_effect=lambda _, cfg: connection_key(cfg)):
            self.assertEqual(post("/api/connection", payload["config"]), (200, {"verified": True}))
        payload["config"]["scm"]["token_env"] = "OTHER_TOKEN"
        self.assertEqual(post("/api/setup", payload)[0], 400)
        self.assertFalse(configuration_path(self.root).exists())
        status, result = post("/api/setup", dict(payload, allowUnverified=True))
        self.assertEqual(status, 200)
        self.assertIs(result["verified"], False)
