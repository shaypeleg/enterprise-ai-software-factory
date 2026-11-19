import base64
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from factory.config import Refused, load
from factory.engine import Engine, State
from factory.runtime import Codex, clean_env, execute
from factory.scm import NoRedirect, Provider


def git(path, *args):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=path,
                          check=True, capture_output=True, text=True).stdout.strip()


class FakeProvider:
    def __init__(self):
        self.current = {"number": 7, "title": "Add subtraction", "body": "Subtract two numbers",
                        "url": "https://example.com/team/app/issues/7", "state": "opened",
                        "labels": ["factory:ready"]}

    def issue(self, number):
        return dict(self.current)


class FakeRuntime:
    def __init__(self, behavior=None):
        self.calls = []
        self.behavior = behavior or {}

    def run(self, role, worktree, context, artifact_dir):
        self.calls.append(role)
        if role == "build":
            (worktree / "calc.py").write_text("def subtract(a, b):\n    return a - b\n")
        action = self.behavior.get(role)
        if callable(action):
            return action(worktree, context)
        return {"verdict": action or {"intake": "ACCEPT", "spec": "READY", "build": "READY",
                "handoff": "READY"}.get(role, "PASS"), "report": "Verified test evidence",
                "risk": "low", "surfaces": []}


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "app"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "user.email", "test@localhost")
        (self.repo / "README.md").write_text("Calculator\n")
        (self.repo / "old.txt").write_text("Delete me\n")
        git(self.repo, "add", "README.md", "old.txt")
        git(self.repo, "commit", "-m", "Initial")
        git(self.repo, "remote", "add", "origin", "https://example.com/team/app.git")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.config = {"version": 1, "execution_mode": "local-pilot", "checkout": str(self.repo),
                       "state_dir": str(self.root / "private"), "base_branch": "main",
                       "git_url": "https://example.com/team/app.git", "scm": {
                           "provider": "gitlab", "api_url": "https://example.com/api/v4",
                           "repository": "team/app", "token_env": "FACTORY_TEST_TOKEN",
                           "queue_label": "factory:ready"}, "models": {"default": "test-model"},
                       "max_rounds": 2, "agent_timeout_seconds": 10, "check_timeout_seconds": 5,
                       "checks": [["python3", "-c", "from calc import subtract; assert subtract(5, 3) == 2"]],
                       "protected_paths": ["infra/**"], "release": {"auto_merge": False, "deploy": False}}
        self.provider, self.runtime = FakeProvider(), FakeRuntime()
        self.state = State(self.config)
        self.engine = Engine(self.config, self.state, self.provider, self.runtime)
        # Offline fixture: pin the local base. Actual worktree/Git/gate/state code still runs.
        self.new_job()

    def new_job(self):
        self.state.save({"issue": dict(self.provider.current), "branch": "factory/issue-7",
                         "base": self.base, "stage": "intake", "round": 1, "results": {}}, "fixture")

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def test_pipeline_runs_real_checks_and_stops_for_human(self):
        job = self.engine.run(7)
        self.assertEqual(job["stage"], "ready")
        self.assertEqual(self.runtime.calls, ["intake", "spec", "build", "code", "security", "handoff"])
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)
        self.assertEqual(git(self.engine.path(job), "rev-parse", "HEAD"), self.base)
        self.assertEqual(git(self.repo, "ls-remote", "--heads", str(self.repo)),
                         self.base + "\trefs/heads/main")
        events = self.state.db.execute("SELECT event FROM events").fetchall()
        self.assertIn(("gate-pass",), events)

    def test_inherited_models_are_resolved_once_per_job(self):
        self.config["model_source"] = "workspace"
        with (patch("factory.engine.workspace_parent", return_value=self.root),
              patch("factory.engine.workspace_models", side_effect=[{"default": "first"}, {"default": "changed"}]) as resolve,
              patch.object(self.engine, "prepare"),
              patch.object(self.engine, "stage", side_effect=Refused("interrupted"))):
            for number, expected in [(8, "first"), (8, "first"), (9, "changed")]:
                self.provider.current["number"] = number
                with self.assertRaisesRegex(Refused, "interrupted"):
                    self.engine.run(number)
                self.assertEqual(self.state.get(number)["models"], {"default": expected})
        self.assertEqual(resolve.call_count, 2)

    def test_authenticated_git_uses_workspace_credential(self):
        self.config["scm"]["credential_source"] = "workspace"
        git(self.repo, "config", "credential.helper", "")
        git(self.repo, "config", "--add", "credential.helper",
            "!f() { printf 'username=fixture-user\\npassword=fixture-token\\n'; }; f")
        result = self.engine.git(self.repo, "config", "--get", "http.extraHeader", authenticate=True)
        self.assertEqual(result.stdout.strip(), b"Authorization: Basic " +
                         base64.b64encode(b"fixture-user:fixture-token"))

    def test_base_fetch_uses_ssh_only_for_workspace_credentials_with_ssh_origin(self):
        ssh_origin = "git@example.com:team/app.git"
        for origin, source, expected in (
            (ssh_origin, "workspace", ("--no-recurse-submodules", ssh_origin)),
            (ssh_origin, "environment", (self.config["git_url"],)),
            (self.config["git_url"], "workspace", (self.config["git_url"],)),
        ):
            with self.subTest(origin=origin, source=source):
                git(self.repo, "remote", "set-url", "origin", origin)
                self.config["scm"]["credential_source"] = source
                verified = self.engine.verify_checkout()
                job = self.state.get(7)
                del job["base"]
                with patch.object(self.engine, "git") as command:
                    command.return_value.stdout = self.base.encode()
                    command.return_value.returncode = 0
                    self.engine.prepare(job, verified)
                flags = {"ssh": True} if len(expected) == 2 else {"authenticate": True}
                command.assert_any_call(self.repo, "fetch", "--no-tags", *expected, "main", **flags)
                self.assertEqual(job["base"], self.base)

    def test_ssh_fetch_passes_only_the_ssh_socket_to_git(self):
        with (patch.dict(os.environ, {"SSH_AUTH_SOCK": "/example/agent", "CODEX_API_KEY": "test-key"}),
              patch("factory.engine.subprocess.run") as command):
            command.return_value.returncode = 0
            self.engine.git(self.repo, "fetch", "git@example.com:team/app.git", "main", ssh=True)
        env = command.call_args.kwargs["env"]
        self.assertEqual(env["SSH_AUTH_SOCK"], "/example/agent")
        self.assertIn("StrictHostKeyChecking=yes", env["GIT_SSH_COMMAND"])
        self.assertIn("ForwardAgent=no", env["GIT_SSH_COMMAND"])
        self.assertNotIn("GIT_CONFIG_VALUE_0", env)
        self.assertNotIn("CODEX_API_KEY", env)

    def test_parent_candidate_preserves_unchanged_submodule_pointer(self):
        git(self.repo, "update-index", "--add", "--cacheinfo", f"160000,{self.base},backend")
        git(self.repo, "commit", "-m", "Pin backend")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.new_job()
        job = self.engine.run(7)
        self.assertEqual(job["stage"], "ready")
        worktree = self.engine.path(job)
        git(worktree, "add", "calc.py")
        tree = git(worktree, "write-tree")
        job["commit"] = git(worktree, "commit-tree", tree, "-p", self.base, "-m", "Candidate")
        git(worktree, "reset", "HEAD", "--", "calc.py")
        self.assertEqual(self.engine.commit_fingerprint(job), job["fingerprint"])
        git(worktree, "update-index", "--force-remove", "backend")
        with self.assertRaisesRegex(Refused, "index changed"):
            self.engine.fingerprint(job)

    def test_gate_failure_never_runs_review_and_exhausts_rounds(self):
        self.config["checks"] = [["python3", "-c", "raise SystemExit(1)"]]
        job = self.engine.run(7)
        self.assertEqual(job["stage"], "needs-human")
        self.assertEqual(self.runtime.calls.count("build"), 2)
        self.assertNotIn("code", self.runtime.calls)

    def test_review_changes_returns_to_builder(self):
        self.runtime.behavior["security"] = "CHANGES"
        job = self.engine.run(7)
        self.assertEqual(job["stage"], "needs-human")
        self.assertEqual(self.runtime.calls.count("security"), 2)
        self.assertNotIn("handoff", self.runtime.calls)

    def test_ui_routes_both_specialists(self):
        original = self.runtime.run

        def run(role, *args):
            result = original(role, *args)
            if role == "intake":
                result["surfaces"] = ["ui"]
            return result

        self.runtime.run = run
        self.engine.run(7)
        self.assertIn("ux", self.runtime.calls)
        self.assertIn("ui", self.runtime.calls)

    def test_intake_needs_info_stops_before_build(self):
        self.runtime.behavior["intake"] = "NEEDS-INFO"
        self.assertEqual(self.engine.run(7)["stage"], "needs-info")
        self.assertEqual(self.runtime.calls, ["intake"])

    def test_readonly_mutation_stops_and_cannot_resume_as_success(self):
        def corrupt(worktree, context):
            (worktree / "README.md").write_text("Tampered")
            raise Refused("simulated runtime failure")

        self.runtime.behavior["spec"] = corrupt
        with self.assertRaisesRegex(Refused, "read-only"):
            self.engine.run(7)
        self.assertEqual(self.state.get(7)["stage"], "needs-human")
        self.assertEqual(self.engine.run(7)["stage"], "needs-human")

    def test_crash_resumes_from_persisted_stage(self):
        def crash(worktree, context):
            raise Refused("runtime unavailable")

        self.runtime.behavior["build"] = crash
        with self.assertRaises(Refused):
            self.engine.run(7)
        self.assertEqual(self.state.get(7)["stage"], "build")
        self.runtime.behavior.clear()
        self.assertEqual(self.engine.run(7)["stage"], "ready")
        self.assertEqual(self.runtime.calls.count("intake"), 1)

    def test_protected_addition_and_deletion_are_blocked(self):
        job = self.state.get(7)
        self.engine.prepare(job, self.engine.verify_checkout())
        root = self.engine.path(job)
        (root / ".github").mkdir()
        (root / ".github" / "new.yml").write_text("unsafe")
        with self.assertRaisesRegex(Refused, "protected"):
            self.engine.safe_paths(job)
        (root / ".github" / "new.yml").unlink()
        (root / "README.md").unlink()
        self.config["protected_paths"].append("README.md")
        with self.assertRaisesRegex(Refused, "protected"):
            self.engine.safe_paths(job)

    def test_deleted_file_is_absent_from_fingerprint(self):
        job = self.engine.run(7)
        root = self.engine.path(job)
        (root / "old.txt").unlink()
        before = self.engine.fingerprint(job)
        git(root, "add", "old.txt")
        with self.assertRaisesRegex(Refused, "index"):
            self.engine.fingerprint(job)
        git(root, "reset", "HEAD", "--", "old.txt")
        self.assertEqual(before, self.engine.fingerprint(job))

    def test_candidate_commit_matches_reviewed_bytes_including_deletion(self):
        job = self.engine.run(7)
        root = self.engine.path(job)
        (root / "old.txt").unlink()
        expected = self.engine.fingerprint(job)
        git(root, "add", "--", "calc.py", "old.txt")
        tree = git(root, "write-tree")
        job["commit"] = git(root, "commit-tree", tree, "-p", self.base, "-m", "Fixture")
        git(root, "reset", "HEAD", "--", "calc.py", "old.txt")
        self.assertEqual(expected, self.engine.commit_fingerprint(job))

    def test_git_clean_filter_cannot_change_reviewed_bytes(self):
        job = self.engine.run(7)
        root = self.engine.path(job)
        expected = self.engine.fingerprint(job)
        git(root, "config", "filter.upper.clean", "tr a-z A-Z")
        (root / ".gitattributes").write_text("calc.py filter=upper\n")
        git(root, "add", "--", "calc.py")
        tree = git(root, "write-tree")
        job["commit"] = git(root, "commit-tree", tree, "-p", self.base, "-m", "Fixture")
        self.assertNotEqual(expected, self.engine.commit_fingerprint(job))

    def test_symlinked_directory_cannot_escape_candidate(self):
        job = self.engine.run(7)
        root = self.engine.path(job)
        (root / "nested").mkdir()
        (root / "nested" / "data").write_text("safe")
        git(root, "add", "nested/data")
        # Keep a tracked nested path while swapping its parent for an external symlink.
        (root / "nested" / "data").unlink()
        (root / "nested").rmdir()
        (root / "nested").symlink_to(self.root)
        git(root, "reset", "HEAD", "--", "nested/data")
        # The symlink itself is untracked; safe_paths rejects its publication.
        with self.assertRaisesRegex(Refused, "symlink"):
            self.engine.safe_paths(job)

    def test_stale_candidate_cannot_publish(self):
        job = self.engine.run(7)
        (self.engine.path(job) / "calc.py").write_text("tampered")
        with self.assertRaisesRegex(Refused, "changed since review"):
            self.engine.publish(7)

    def test_issue_change_rejected_on_resume(self):
        self.provider.current["body"] = "New scope"
        with self.assertRaisesRegex(Refused, "issue changed"):
            self.engine.run(7)

    def test_origin_mismatch_rejected(self):
        git(self.repo, "remote", "set-url", "origin", "https://evil.example/repo.git")
        with self.assertRaisesRegex(Refused, "origin"):
            self.engine.run(7)

    def test_checks_cannot_inherit_model_or_scm_secrets(self):
        self.config["checks"] = [["python3", "-c", "import os; assert not any("
            "k in os.environ for k in ('CODEX_API_KEY', 'FACTORY_TEST_TOKEN', 'AWS_SECRET_ACCESS_KEY'))"]]
        with patch.dict(os.environ, {"CODEX_API_KEY": "secret", "FACTORY_TEST_TOKEN": "secret",
                                     "AWS_SECRET_ACCESS_KEY": "secret"}):
            self.assertEqual(self.engine.run(7)["stage"], "ready")

    def test_base_advance_blocks_publication_before_any_push(self):
        self.engine.run(7)
        actual_git = self.engine.git

        def changed_base(cwd, *args, **kwargs):
            if args[0] == "fetch":
                return subprocess.CompletedProcess(args, 0, b"", b"")
            if args == ("rev-parse", "FETCH_HEAD"):
                return subprocess.CompletedProcess(args, 0, b"f" * 40 + b"\n", b"")
            self.assertNotEqual(args[0], "push")
            return actual_git(cwd, *args, **kwargs)

        self.engine.git = changed_base
        with self.assertRaisesRegex(Refused, "base branch advanced"):
            self.engine.publish(7)

    def test_changed_configuration_cannot_reuse_old_evidence(self):
        self.state.close()
        self.config["models"]["default"] = "new-model"
        try:
            with self.assertRaisesRegex(Refused, "configuration changed"):
                State(self.config)
        finally:
            self.config["models"]["default"] = "test-model"
            self.state = State(self.config)

    def test_include_file_cannot_hide_git_url_rewrites(self):
        include = self.root / "git-override"
        include.write_text('[http]\n    followRedirects = true\n')
        git(self.repo, "config", "include.path", str(include))
        with self.assertRaisesRegex(Refused, "overrides"):
            self.engine.run(7)

    def test_local_url_rewrite_rejected(self):
        git(self.repo, "config", "url.https://evil.example/.insteadOf", "https://example.com/")
        with self.assertRaises(Refused):
            self.engine.run(7)

    def test_one_worker_per_state_directory(self):
        with self.assertRaisesRegex(Refused, "another factory"):
            State(self.config)

    def test_publication_resumes_after_ambiguous_pr_failure_without_second_commit(self):
        self.engine.run(7)
        actual_git = self.engine.git
        push_calls = []

        def offline_git(cwd, *args, **kwargs):
            if args[0] == "fetch":
                # FETCH_HEAD is a plain ref pointing at the fixture's base.
                (self.repo / ".git" / "FETCH_HEAD").write_text(self.base + "\n")
                return subprocess.CompletedProcess(args, 0, b"", b"")
            if args[0] == "ls-remote":
                return subprocess.CompletedProcess(args, 2, b"", b"")
            if args[0] == "push":
                push_calls.append(args)
                return subprocess.CompletedProcess(args, 0, b"", b"")
            return actual_git(cwd, *args, **kwargs)

        self.engine.git = offline_git
        self.provider.create_request = lambda *args: (_ for _ in ()).throw(Refused("connection lost"))
        with self.assertRaisesRegex(Refused, "connection lost"):
            self.engine.publish(7)
        commit = self.state.get(7)["commit"]
        self.provider.create_request = lambda *args: {"web_url": "https://example.com/mr/9"}
        result = self.engine.publish(7)
        self.assertEqual(result["stage"], "published")
        self.assertEqual(result["commit"], commit)
        self.assertEqual(push_calls[0], push_calls[1])
        self.assertNotIn("--force", push_calls[0])
        self.assertEqual(git(self.engine.path(result), "rev-parse", "HEAD"), self.base)

    def test_config_validation_rejects_insecure_url_and_deployment(self):
        path = self.root / "config.json"
        path.write_text(json.dumps(self.config))
        self.assertEqual(load(path)["scm"]["provider"], "gitlab")
        self.config["scm"]["api_url"] = "http://example.com"
        path.write_text(json.dumps(self.config))
        with self.assertRaisesRegex(Refused, "HTTPS"):
            load(path)
        self.config["scm"]["api_url"] = "https://example.com/api/v4"
        self.config["release"]["auto_merge"] = True
        path.write_text(json.dumps(self.config))
        with self.assertRaisesRegex(Refused, "cannot merge"):
            load(path)


class AdapterTests(unittest.TestCase):
    def provider(self, kind):
        return Provider({"provider": kind, "api_url": "https://example.com/api",
                         "repository": "team/app", "token_env": "FACTORY_TEST_TOKEN",
                         "queue_label": "factory:ready"})

    def test_nested_gitlab_project_is_encoded(self):
        config = self.provider("gitlab").config
        config["repository"] = "team/subgroup/app"
        self.assertEqual(Provider(config).project, "/projects/team%2Fsubgroup%2Fapp")

    def test_github_issue_queue_excludes_prs(self):
        provider = self.provider("github")
        row = {"number": 2, "title": "Feature", "body": "Build", "html_url": "https://example.com/2",
               "state": "open", "labels": [{"name": "factory:ready"}]}
        provider.request = lambda *args: [row, dict(row, pull_request={"url": "pr"})]
        self.assertEqual(len(provider.queue()), 1)

    def test_pagination_reads_second_page(self):
        provider = self.provider("gitlab")
        calls = []

        def request(method, url):
            calls.append(url)
            return [{}] * 100 if "&page=1" in url else [{"last": True}]

        provider.request = request
        self.assertEqual(len(list(provider.pages("/issues", {}))), 101)
        self.assertEqual(len(calls), 2)

    def test_no_credential_forwarding_on_redirect(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example"))

    def test_duplicate_draft_creation_is_reconciled(self):
        for kind in ("github", "gitlab"):
            provider = self.provider(kind)
            provider.pages = lambda *args: iter([{"number": 9, "state": "open" if kind == "github" else "opened"}])
            provider.request = lambda *args: self.fail("must not POST a duplicate")
            self.assertEqual(provider.create_request({"number": 7}, "factory/issue-7", "main")["number"], 9)

    def test_gitlab_creation_is_draft_and_targets_configured_base(self):
        provider = self.provider("gitlab")
        provider.pages = lambda *args: iter([])
        calls = []
        provider.request = lambda *args: calls.append(args) or {"web_url": "https://example.com/mr"}
        provider.create_request({"number": 7, "title": "Feature"}, "factory/issue-7", "develop")
        self.assertEqual(calls[0][2]["title"], "Draft: Feature")
        self.assertEqual(calls[0][2]["target_branch"], "develop")

    def test_post_is_not_retried(self):
        provider = self.provider("github")
        with patch.dict(os.environ, {"FACTORY_TEST_TOKEN": "secret"}):
            with patch.object(provider.opener, "open", side_effect=HTTPError("url", 503, "", {}, None)) as call:
                with self.assertRaisesRegex(Refused, "503"):
                    provider.request("POST", "/pulls", {})
                self.assertEqual(call.call_count, 1)

    def test_removed_queue_label_refuses_intake(self):
        provider = self.provider("gitlab")
        provider.request = lambda *args: {"iid": 7, "title": "Feature", "description": "",
            "web_url": "https://example.com/7", "state": "opened", "labels": []}
        with self.assertRaisesRegex(Refused, "queue label"):
            provider.issue(7)

    def test_transient_get_failure_is_retried(self):
        provider = self.provider("github")
        from io import BytesIO
        response = BytesIO(b'{"ok": true}')
        error = HTTPError("url", 429, "", {}, None)
        with patch.dict(os.environ, {"FACTORY_TEST_TOKEN": "secret"}):
            with patch.object(provider.opener, "open", side_effect=[error, response]) as call:
                with patch("factory.scm.time.sleep"):
                    self.assertEqual(provider.request("GET", "/test"), {"ok": True})
                self.assertEqual(call.call_count, 2)


class RuntimeTests(unittest.TestCase):
    def test_clean_environment_drops_credentials(self):
        with patch.dict(os.environ, {"FACTORY_GITLAB_TOKEN": "secret", "AWS_SECRET_ACCESS_KEY": "secret",
                                     "SSH_AUTH_SOCK": "secret", "CODEX_API_KEY": "secret"}):
            env = clean_env("/private/home")
        self.assertEqual(env["HOME"], "/private/home")
        self.assertFalse(any("TOKEN" in key or "KEY" in key or "SSH" in key for key in env))

    def test_timeout_is_a_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(Refused, "timed out"):
                execute(["python3", "-c", "import time; time.sleep(30)"], temp, clean_env(temp),
                        0.05, Path(temp) / "log")

    def test_codex_arguments_and_schema_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            captured = []

            def fake_execute(argv, cwd, env, timeout, log, stdin):
                self.assertTrue(Path(env["CODEX_HOME"]).is_dir())
                captured.append((argv, env))
                Path(argv[argv.index("--output-last-message") + 1]).write_text(json.dumps({
                    "verdict": "PASS", "report": "Reviewed", "risk": "low", "surfaces": []}))

            runtime = Codex({"models": {"default": "test-model", "security": "review-model"},
                             "agent_timeout_seconds": 10}, "/trusted/codex")
            with patch.dict(os.environ, {"CODEX_API_KEY": "model-secret", "SCM_TOKEN": "scm-secret"}):
                with patch("factory.runtime.execute", side_effect=fake_execute):
                    runtime.run("security", root, {}, root / "logs")
            argv, env = captured[0]
            self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
            self.assertEqual(argv[argv.index("--model") + 1], "review-model")
            self.assertNotIn("SCM_TOKEN", env)
            self.assertIn('shell_environment_policy.inherit="none"', argv)

    def test_roles_use_the_signed_in_codex_profile_with_no_api_key(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            profile = root / "signed-in"
            profile.mkdir()

            def fake_execute(argv, cwd, env, timeout, log, stdin):
                self.assertEqual(Path(env["CODEX_HOME"]), profile)
                self.assertNotEqual(Path(env["HOME"]), profile)
                self.assertNotIn("SCM_TOKEN", env)
                self.assertNotIn("CODEX_API_KEY", env)
                self.assertNotIn("--sandbox", argv)
                self.assertIn("--ignore-user-config", argv)
                self.assertIn("permissions.factory-role.network.enabled=false", argv)
                self.assertTrue(any(f'"{profile / "auth.json"}" = "deny"' in arg for arg in argv))
                verdict = "READY" if log.stem == "build" else "PASS"
                Path(argv[argv.index("--output-last-message") + 1]).write_text(json.dumps({
                    "verdict": verdict, "report": "Test result", "risk": "low", "surfaces": []}))

            login = subprocess.CompletedProcess([], 0, "", "Logged in using ChatGPT")
            with patch.dict(os.environ, {"CODEX_HOME": str(profile), "SCM_TOKEN": "scm-secret"}, clear=True):
                runtime = Codex({"models": {"default": "auto", "security": "review-model"},
                                 "agent_timeout_seconds": 15}, "/trusted/codex")
                with patch("factory.runtime.subprocess.run", return_value=login) as status:
                    with patch("factory.runtime.execute", side_effect=fake_execute) as execute_role:
                        self.assertEqual(runtime.run("build", root, {}, root / "logs")["verdict"], "READY")
                        self.assertEqual(runtime.run("security", root, {}, root / "logs",
                                                     models={"default": "parent-review-model"})["verdict"], "PASS")
            status.assert_called_once()
            self.assertEqual(execute_role.call_count, 2)
            build_args, review_args = [call.args[0] for call in execute_role.call_args_list]
            self.assertIn('permissions.factory-role.extends=":workspace"', build_args)
            self.assertIn('permissions.factory-role.extends=":read-only"', review_args)
            self.assertNotIn("--model", build_args)
            self.assertEqual(review_args[review_args.index("--model") + 1], "parent-review-model")

    def test_missing_codex_login_refuses_before_launching_a_role(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            root = Path(temp)
            runtime = Codex({"models": {"default": "test"}, "agent_timeout_seconds": 15}, "/trusted/codex")
            status = subprocess.CompletedProcess([], 1, "", "Not logged in")
            with patch("factory.runtime.subprocess.run", return_value=status):
                with patch("factory.runtime.execute") as execute_role:
                    with self.assertRaisesRegex(Refused, "Sign in to Codex"):
                        runtime.run("build", root, {}, root / "logs")
            execute_role.assert_not_called()

    def test_invalid_codex_verdict_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def fake_execute(argv, *args):
                Path(argv[argv.index("--output-last-message") + 1]).write_text('{"verdict":"APPROVE"}')

            runtime = Codex({"models": {"default": "test"}, "agent_timeout_seconds": 1}, "codex")
            with patch.dict(os.environ, {"CODEX_API_KEY": "secret"}):
                with patch("factory.runtime.execute", side_effect=fake_execute):
                    with self.assertRaisesRegex(Refused, "schema"):
                        runtime.run("handoff", root, {}, root / "logs")


if __name__ == "__main__":
    unittest.main()
