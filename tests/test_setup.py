import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factory.config import Refused, load
from factory.project import configuration_path, detect, discovered_remote
from factory.setup import PRESET, initial, install_skill, save


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
