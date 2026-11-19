import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factory.dashboard import list_runs, run_detail, server_for
from factory.engine import State


class DashboardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        checkout = self.root / "repo"
        checkout.mkdir()
        example = Path(__file__).resolve().parents[1] / "config.example.json"
        self.config = json.loads(example.read_text())
        self.config.update(checkout=str(checkout), state_dir=str(self.root / "runs"),
                           models={"default": "fixture-model"})
        self.settings = self.root / "config.json"
        self.settings.write_text(json.dumps(self.config))
        self.job = {"issue": {"number": 12, "title": "Fix settings", "body": "Private issue details"},
                    "stage": "gate", "round": 1, "results": {}}

    def test_reads_updates_while_worker_holds_lock(self):
        self.assertEqual(list_runs(self.settings), [])
        self.assertFalse(Path(self.config["state_dir"]).exists())
        with State(self.config) as state:
            state.save(self.job, "transition")
            self.assertEqual(list_runs(self.settings)[0]["stage"], "gate")
            state.save(self.job, "started-gate")
            self.assertEqual(list_runs(self.settings)[0]["active_role"], "gate")
            self.job.update(stage="build", round=2)
            self.job["results"]["feedback"] = "A check failed"
            state.save(self.job, "revision-required")
            self.job.update(stage="gate")
            state.save(self.job, "transition")
            self.job["results"]["gate"] = {"verdict": "PASS", "fingerprint": "private-fingerprint"}
            state.save(self.job, "gate-pass")
            self.job.update(stage="review")
            self.job["results"]["code"] = {"verdict": "PASS", "report": "Reviewed changes"}
            state.save(self.job, "stage-code")
            result = run_detail(self.settings, 12)
            self.assertEqual(result["rounds"][1]["gate"], {"verdict": "FAIL", "report": "A check failed"})
            self.assertEqual(result["rounds"][2]["gate"]["verdict"], "PASS")
            self.assertEqual(result["rounds"][2]["code"]["report"], "Reviewed changes")
            self.assertEqual(result["job"]["stage"], "review")
            state.save(self.job, "started-security")
            row = list_runs(self.settings)[0]
            self.assertEqual(row["active_role"], "security")
            self.assertEqual(row["review_roles"], ("code", "security"))
            self.assertEqual(row["rounds"][1]["gate"]["verdict"], "FAIL")
            self.assertEqual(row["rounds"][2]["gate"]["verdict"], "PASS")
            self.assertNotIn("Reviewed changes", json.dumps(row))
            self.assertNotIn("Private issue details", json.dumps(result))
            self.assertNotIn("private-fingerprint", json.dumps(result))
        self.assertIsNone(list_runs(self.settings)[0]["active_role"])
        self.assertIsNone(run_detail(self.settings, 12)["job"]["active_role"])

    def test_dashboard_api_requires_a_local_private_session(self):
        with patch("factory.dashboard.HTTPServer") as server_factory:
            server_factory.return_value.server_port = 42000
            server, url = server_for("Test project", {".": self.settings})
            handler_type = server_factory.call_args.args[1]
        self.assertEqual(server_factory.call_args.args[0], ("127.0.0.1", 0))
        handler = handler_type.__new__(handler_type)
        handler.server = server
        handler.path = "/api/runs"
        handler.headers = {"Host": "127.0.0.1:42000"}
        with patch.object(handler, "reply") as reply:
            handler.do_GET()
            self.assertEqual(reply.call_args.args[0], 403)
            handler.headers["X-Factory-Session"] = url.split("#", 1)[1]
            handler.headers["Host"] = "example.org:42000"
            handler.do_GET()
            self.assertEqual(reply.call_args.args[0], 403)
            handler.headers["Host"] = "127.0.0.1:42000"
            handler.do_GET()
            self.assertEqual(reply.call_args.args[0], 200)
            self.assertEqual(reply.call_args.args[1]["repositories"][0]["runs"], [])
            handler.path = "/api/run?repository=unknown&issue=12"
            handler.do_GET()
            self.assertEqual(reply.call_args.args[0], 404)
            handler.do_POST()
            self.assertEqual(reply.call_args.args[0], 405)
