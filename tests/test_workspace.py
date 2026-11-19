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
from factory.config import Refused
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
