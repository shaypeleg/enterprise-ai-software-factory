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
from factory.scm import Provider, SCMHTTPError
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
        config["scm"]["credential_source"] = "environment"
        config["scm"]["token_env"] = f"FACTORY_{config['scm']['provider'].upper()}_TOKEN"
        config.update(models={"default": "test-model", "security": "security-model"},
                      checks=[["python3", "-m", "unittest"]])
        return {"config": config, "consent": True, "share": True}

    def test_detects_gitlab_ssh_remote_and_ignores_inline_credentials(self):
        details = detect(self.root)
        self.assertEqual(details["scm"]["repository"], "team/platform/app")
        self.assertEqual(details["scm"]["api_url"], "https://gitlab.example.com/api/v4")
        self.assertEqual(details["base_branch"], "main")
        self.assertEqual(discovered_remote("https://user:secret@github.com/team/app.git"), "")

    def test_suggests_local_ci_checks_and_skips_release_commands(self):
        (self.root / ".gitignore").write_text(".venv/\n")
        (self.root / "tests/ci").mkdir(parents=True)
        (self.root / "scripts/ci").mkdir(parents=True)
        (self.root / "scripts/ci/requirements.txt").touch()
        (self.root / "scripts/test_release_config.py").touch()
        (self.root / ".gitlab-ci.yml").write_text(
            "checks:\n  image: python:3.12-bookworm\n  script:\n"
            "    - python -m unittest discover -s tests/ci -v\n"
            "    - python scripts/test_release_config.py\n"
            "    - python scripts/publish.py\n")
        result = initial(self.root)
        self.assertEqual(result["config"]["checks"], [
            ["python3.12", "-m", "venv", ".venv"],
            [".venv/bin/python", "-m", "pip", "install", "-r", "scripts/ci/requirements.txt"],
            [".venv/bin/python", "-m", "unittest", "discover", "-s", "tests/ci", "-v"],
            [".venv/bin/python", "scripts/test_release_config.py"],
        ])
        self.assertEqual(result["config"]["scm"]["credential_source"], "workspace")

    def test_suggests_locked_node_checks_and_falls_back_without_manifest(self):
        self.assertEqual(initial(self.root)["config"]["checks"], [["git", "diff", "--check", "HEAD", "--"]])
        (self.root / "package.json").write_text(json.dumps({"scripts": {
            "test": 'echo "Error: no test specified" && exit 1', "lint": "eslint .", "deploy": "ship"}}))
        (self.root / "package-lock.json").write_text("{}")
        self.assertEqual(initial(self.root)["config"]["checks"], [["npm", "ci"], ["npm", "run", "lint"]])

    def test_workspace_credential_helper_is_used_by_setup_and_the_queue(self):
        self.git("config", "credential.helper", "")
        self.git("config", "--add", "credential.helper",
                 "!f() { test -z \"$CODEX_API_KEY\" && printf 'username=test-user\\npassword=test-token\\n'; }; f")
        config = initial(self.root)["config"]
        with patch.dict(os.environ, {"CODEX_API_KEY": "unrelated-token"}), patch("factory.scm.build_opener") as opener:
            opener.return_value.open.side_effect = [io.BytesIO(b"[]"), io.BytesIO(b"[]")]
            self.assertEqual(verify_connection(self.root, config), connection_key(config))
            scm = Provider(config["scm"], checkout=self.root, git_url=config["git_url"])
            self.assertEqual(scm.queue(), [])
            for args, _ in opener.return_value.open.call_args_list:
                self.assertEqual(args[0].get_header("Private-token"), "test-token")

    def test_workspace_credentials_refuse_unrelated_api_host_during_normal_use(self):
        config = initial(self.root)["config"]
        config["scm"]["api_url"] = "https://other.example.com/api/v4"
        with patch("factory.credentials.workspace_credential") as helper:
            scm = Provider(config["scm"], checkout=self.root, git_url=config["git_url"])
            with self.assertRaisesRegex(Refused, "only sends tokens"):
                scm.queue()
            helper.assert_not_called()

    def test_workspace_settings_discard_an_unused_token_field(self):
        payload = self.payload()
        scm = payload["config"]["scm"]
        scm.update(credential_source="workspace", token_env="dummy.credential")
        save(self.root, payload)
        local = configuration_path(self.root)
        self.assertNotIn("token_env", load(local)["scm"])
        self.assertNotIn("token_env", json.loads((self.root / PRESET).read_text())["scm"])

        # Existing settings from older installers must not repopulate that field.
        legacy = load(local)
        legacy["scm"]["token_env"] = "dummy.credential"
        local.write_text(json.dumps(legacy))
        self.assertNotIn("token_env", initial(self.root)["config"]["scm"])
        save(self.root, payload)
        self.assertNotIn("token_env", load(local)["scm"])

    def test_token_field_rejects_values_that_look_like_credentials(self):
        payload = self.payload()
        for value in ("dummy.credential", "ghp_" + "x" * 35):
            payload["config"]["scm"]["token_env"] = value
            with self.assertRaisesRegex(Refused, "Enter a Git token variable name"):
                save(self.root, payload)
        self.assertFalse(configuration_path(self.root).exists())

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
