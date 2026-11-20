import json
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from argparse import Namespace
from contextlib import redirect_stdout
from unittest.mock import patch

from factory.__main__ import workspace_command
from factory.cascade import cascade_jobs
from factory.config import Refused
from factory.engine import State
from factory.workspace import discover, select_jobs, workspace_root


def git(path, *args):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=path,
                          check=True, capture_output=True, text=True).stdout.strip()


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name).resolve()

    def repository(self, name):
        path = self.directory / name
        path.mkdir()
        git(path, "init", "-b", "main")
        git(path, "-c", "user.name=Test", "-c", "user.email=test@example.org",
            "commit", "--allow-empty", "-m", "Initial")
        return path

    def test_discovers_initialized_and_missing_submodules(self):
        upstream = self.repository("upstream")
        parent = self.repository("parent")
        git(parent, "-c", "protocol.file.allow=always", "submodule", "add", str(upstream), "backend")
        git(parent, "update-index", "--add", "--cacheinfo",
            f"160000,{git(upstream, 'rev-parse', 'HEAD')},missing")
        self.assertEqual(workspace_root(parent / "backend"), parent)
        self.assertEqual(discover(parent), {".": parent, "backend": parent / "backend", "missing": None})

    def test_manifest_supports_sibling_checkouts_and_rejects_escape(self):
        backend = self.repository("api")
        frontend = self.repository("ui")
        manifest = self.directory / ".factory-workspace.json"
        manifest.write_text(json.dumps({"repositories": {"backend": "api", "frontend": "ui"}}))
        self.assertEqual(workspace_root(frontend), self.directory)
        self.assertEqual(discover(self.directory), {"backend": backend, "frontend": frontend})
        (self.directory / "link").symlink_to(backend)
        manifest.write_text(json.dumps({"repositories": {"backend": "link"}}))
        with self.assertRaisesRegex(Refused, "without symlinks"):
            discover(self.directory)
        manifest.write_text(json.dumps({"repositories": {"backend": "../outer"}}))
        with self.assertRaisesRegex(Refused, "must be relative"):
            discover(self.directory)

    def test_parent_default_routes_child_issues_and_allows_explicit_targets(self):
        parent, backend, frontend = self.directory, self.directory / "be", self.directory / "fe"
        repositories = {".": parent, "be": backend, "fe": frontend}
        with patch("factory.workspace.configured", side_effect=lambda path: {"scm": {}, "path": path}), \
                patch("factory.workspace.Provider") as provider:
            issue = provider.return_value.issue
            issue.return_value = {"body": "Feature\n```factory-routes\nbe=17\nfe=23\n```\n"}
            jobs = select_jobs(repositories, ["42"], follow_parent=True)
            self.assertEqual([(alias, number, config["path"]) for alias, number, config in jobs],
                             [("be", 17, backend), ("fe", 23, frontend)])
            issue.assert_called_once_with(42)
            issue.return_value = {"body": "Parent change"}
            jobs = select_jobs(repositories, ["42", "be=19"], follow_parent=True)
            self.assertEqual([(alias, number) for alias, number, _ in jobs], [(".", 42), ("be", 19)])
            select_jobs(repositories, [".=42"], follow_parent=True)
            self.assertEqual(issue.call_count, 2)
            issue.return_value = {"body": "```factory-routes\nbe=1\nunknown=3\n```"}
            with self.assertRaisesRegex(Refused, "Unknown repository"):
                select_jobs(repositories, ["42"], follow_parent=True)
            issue.return_value = {"body": "```factory-routes\nbe=1\nbe=1\n```"}
            with self.assertRaisesRegex(Refused, "selected twice"):
                select_jobs(repositories, ["42"], follow_parent=True)

    def test_workspace_command_runs_each_child_without_publishing(self):
        backend = self.directory / "backend"
        frontend = self.directory / "frontend"
        records = {".": self.directory, "be": backend, "fe": frontend}
        args = Namespace(project=self.directory, action="run", jobs=["42"])
        with patch("factory.__main__.workspace_root", return_value=self.directory), \
                patch("factory.__main__.discover", return_value=records), \
                patch("factory.workspace.configured", side_effect=lambda path: {"scm": {}, "path": path}), \
                patch("factory.workspace.Provider") as provider, \
                patch("factory.__main__.operate", side_effect=[{"stage": "ready"}, {"stage": "needs-human"}]) as run:
            provider.return_value.issue.return_value = {"body": "```factory-routes\nbe=17\nfe=23\n```"}
            with redirect_stdout(io.StringIO()) as stdout:
                workspace_command(args)
            self.assertEqual([(row["repository"], row["issue"], row["result"]["stage"])
                              for row in json.loads(stdout.getvalue())],
                             [("be", 17, "ready"), ("fe", 23, "needs-human")])
            self.assertEqual([call.args for call in run.call_args_list],
                             [({"scm": {}, "path": backend}, "run", 17),
                              ({"scm": {}, "path": frontend}, "run", 23)])

    def test_workspace_cascade_uses_one_issue_and_one_durable_plan(self):
        parent = self.repository("parent")
        backend = self.repository("parent/backend")
        frontend = self.repository("parent/frontend")
        records = {".": parent, "be": backend, "fe": frontend}
        configs = {path: {"checkout": str(path), "git_url": f"https://example.com/team/{path.name}.git",
                          "scm": {"repository": f"team/{path.name}"},
                          "state_dir": str(self.directory / (path.name + "-state"))}
                   for path in records.values()}
        issue = {"number": 42, "title": "Add report status", "body": "Expose status and show it in the UI.",
                 "url": "https://example.com/team/parent/-/issues/42"}
        args = Namespace(project=parent, action="run", jobs=["42"])
        with (patch("factory.__main__.workspace_root", return_value=parent),
              patch("factory.__main__.discover", return_value=records),
              patch("factory.workspace.configured", side_effect=configs.__getitem__),
              patch("factory.workspace.Provider") as provider,
              patch("factory.cascade.codex_executable", return_value="/test/codex"),
              patch("factory.cascade.Codex") as codex,
              patch("factory.__main__.operate", return_value={"stage": "ready"}) as run):
            provider.return_value.issue.return_value = issue
            codex.return_value.plan_workspace.return_value = {"verdict": "READY", "report": "Two tasks", "tasks": [
                {"repository": "be", "scope": "Expose status."},
                {"repository": "fe", "scope": "Render status."},
            ]}
            for _ in range(2):
                with redirect_stdout(io.StringIO()) as stdout:
                    workspace_command(args)
                results = json.loads(stdout.getvalue())
                self.assertEqual([row["repository"] for row in results], ["be", "fe"])
                self.assertTrue(all(row["parent_issue_url"] == issue["url"] for row in results))
            codex.return_value.plan_workspace.assert_called_once()
            self.assertEqual([call.args[:3] for call in run.call_args_list[:2]], [
                (configs[backend], "run", 42), (configs[frontend], "run", 42)])
            route = run.call_args_list[0].kwargs["route"]
            self.assertIn("Expose status.", route.current_issue()["body"])
            self.assertEqual(route.current_issue()["workspace_parent"], configs[parent]["git_url"])
            for call in provider.return_value.update_workspace_note.call_args_list:
                self.assertEqual(call.args[0], 42)
                self.assertIn("`team/backend` — Checks and reviews passed", call.args[1])
                self.assertIn("`team/frontend` — Checks and reviews passed", call.args[1])
            for checkout, stage in ((backend, "ready"), (frontend, "needs-human")):
                with State(configs[checkout]) as state:
                    state.save({"id": -42, "issue": issue, "stage": stage}, "fixture")
            args.action = "publish"
            with self.assertRaisesRegex(Refused, "fe issue #42 is not ready"):
                workspace_command(args)
            self.assertEqual(run.call_count, 4)
            with State(configs[frontend]) as state:
                state.save({"id": -42, "issue": issue, "stage": "ready"}, "fixture")
            run.return_value = {"stage": "published"}
            with redirect_stdout(io.StringIO()):
                workspace_command(args)
            self.assertEqual([call.args[1] for call in run.call_args_list[-2:]], ["publish", "publish"])
            codex.return_value.plan_workspace.assert_called_once()

    def test_workspace_resume_reuses_quick_plan_with_no_new_planning_or_publish(self):
        parent = self.repository("parent")
        frontend = self.repository("parent/frontend")
        records = {".": parent, "fe": frontend}
        configs = {path: {"checkout": str(path), "git_url": f"https://example.com/team/{path.name}.git",
                          "scm": {"repository": f"team/{path.name}"},
                          "state_dir": str(self.directory / (path.name + "-state"))}
                   for path in records.values()}
        issue = {"number": 42, "title": "Change a default", "body": "Default to Feedback",
                 "url": "https://example.com/team/parent/-/issues/42"}
        args = Namespace(project=parent, action="run", jobs=["42"])
        with (patch("factory.__main__.workspace_root", return_value=parent),
              patch("factory.__main__.discover", return_value=records),
              patch("factory.workspace.configured", side_effect=configs.__getitem__),
              patch("factory.workspace.Provider") as provider,
              patch("factory.cascade.codex_executable", return_value="/test/codex"),
              patch("factory.cascade.Codex") as codex,
              patch("factory.__main__.operate", side_effect=[{"stage": "needs-human"}, {"stage": "ready"}]) as run,
              redirect_stdout(io.StringIO()) as stdout):
            provider.return_value.issue.return_value = issue
            codex.return_value.plan_workspace.return_value = {"verdict": "QUICK", "report": "One task", "tasks": [
                {"repository": "fe", "scope": "Change the existing default and test it."},
            ]}
            workspace_command(args)
            args.action = "resume"
            stdout.truncate(0)
            stdout.seek(0)
            workspace_command(args)
            self.assertEqual(json.loads(stdout.getvalue())[0]["result"]["stage"], "ready")
            codex.return_value.plan_workspace.assert_called_once()
            self.assertEqual([call.args[1] for call in run.call_args_list], ["run", "resume"])
            self.assertTrue(all(call.kwargs["route"].quick for call in run.call_args_list))
            self.assertEqual(provider.return_value.update_workspace_note.call_count, 2)

    def test_workspace_cascade_refuses_a_repository_outside_the_workspace(self):
        parent = self.repository("parent")
        config = {"state_dir": str(self.directory / "state")}
        issue = {"number": 42, "body": "Frontend change"}
        with (patch("factory.cascade.codex_executable", return_value="/test/codex"),
              patch("factory.cascade.Codex") as codex):
            codex.return_value.plan_workspace.return_value = {"verdict": "READY", "report": "Plan", "tasks": [
                {"repository": "other", "scope": "Change outside workspace."}]}
            with self.assertRaisesRegex(Refused, "Unknown repository"):
                cascade_jobs({".": parent}, config, None, issue, "run")
