"""Durable, single-worker pilot pipeline; publishing is a separate operation."""

import base64
import fcntl
import fnmatch
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .config import Refused, require
from .credentials import repository_credential
from .project import discovered_remote
from .runtime import ProcessFailed, check_diagnostics, clean_env, execute, recorded_agent_id
from .workspace import workspace_models, workspace_parent


PROTECTED = (".git*", ".github/**", ".gitlab/**", ".codex/**", ".agents/**",
             "AGENTS.md", "**/AGENTS.md", ".factory-settings.json", ".factory-workspace.json", "factory/**")
STOP = {"ready", "needs-human", "needs-info", "rejected", "published"}


def review_roles(job):
    roles = ("code", "security")
    if job.get("profile") != "quick" and "ui" in job["results"].get("intake", {}).get("surfaces", ()):
        roles += ("ux", "ui")
    return roles


def job_id(job):
    return job.get("id", job["issue"]["number"])


class State:
    def __init__(self, config):
        self.root = Path(config["state_dir"])
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(stat.S_IMODE(self.root.stat().st_mode) & 0o077 == 0,
                "state_dir must be private (chmod 700)")
        self.lock = open(self.root / "worker.lock", "a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise Refused("another factory worker holds this state directory") from None
        self.db = sqlite3.connect(str(self.root / "state.sqlite3"))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS workspace_plans (issue INTEGER PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY, job INTEGER NOT NULL,
                at TEXT NOT NULL, event TEXT NOT NULL, value TEXT NOT NULL
            );
        """)
        digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        previous = self.db.execute("SELECT value FROM metadata WHERE key='config'").fetchone()
        if previous and previous[0] != digest:
            self.close()
            raise Refused("configuration changed: use a new state_dir; existing evidence stays immutable")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('config', ?)", (digest,))

    def close(self):
        self.db.close()
        self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def get(self, number):
        row = self.db.execute("SELECT value FROM jobs WHERE id=?", (number,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, job, event):
        value = json.dumps(job, sort_keys=True)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO jobs VALUES (?, ?)", (job_id(job), value))
            self.db.execute("INSERT INTO events(job, at, event, value) VALUES (?, ?, ?, ?)",
                            (job_id(job), datetime.now(timezone.utc).isoformat(), event, value))

    def workspace_plan(self, number):
        row = self.db.execute("SELECT value FROM workspace_plans WHERE issue=?", (number,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_workspace_plan(self, number, plan):
        with self.db:
            self.db.execute("INSERT INTO workspace_plans VALUES (?, ?)", (number, json.dumps(plan)))

    def all(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT value FROM jobs ORDER BY id")]

    def has_event(self, job, event):
        return self.db.execute("SELECT 1 FROM events WHERE job=? AND event=? LIMIT 1",
                               (job_id(job), event)).fetchone() is not None


class Engine:
    def __init__(self, config, state, provider, runtime):
        self.config, self.state, self.provider, self.runtime = config, state, provider, runtime
        self.checkout = Path(config["checkout"])

    def git(self, cwd, *args, authenticate=False, ssh=False, check=True, index_file=None):
        # Ignore global/system Git credentials and hooks. Only network operations receive auth.
        with tempfile.TemporaryDirectory(prefix="git-", dir=self.state.root) as home:
            env = clean_env(home)
            env.update({"GIT_AUTHOR_NAME": "Codex factory", "GIT_AUTHOR_EMAIL": "factory@localhost",
                        "GIT_COMMITTER_NAME": "Codex factory", "GIT_COMMITTER_EMAIL": "factory@localhost"})
            if index_file:
                env["GIT_INDEX_FILE"] = str(index_file)
            if authenticate:
                user, token = repository_credential(self.config["scm"], self.checkout, self.config["git_url"])
                basic = base64.b64encode((user + ":" + token).encode()).decode()
                env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
                            "GIT_CONFIG_VALUE_0": "Authorization: Basic " + basic})
            elif ssh:
                env.update(GIT_SSH_VARIANT="ssh",
                           GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ForwardAgent=no")
                if os.environ.get("SSH_AUTH_SOCK"):
                    env["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
            argv = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                    "-c", "credential.helper=", "-c", "commit.gpgsign=false",
                    "-c", "http.followRedirects=false", *args]
            try:
                result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=120)
            except subprocess.TimeoutExpired:
                raise Refused("Git timed out; inspect job state before retrying") from None
            require(not check or result.returncode == 0, "Git command failed: " + args[0])
            return result

    def verify_checkout(self):
        root = self.git(self.checkout, "rev-parse", "--show-toplevel").stdout.decode().strip()
        require(Path(root).resolve() == self.checkout, "checkout must be the Git repository root")
        remote = self.git(self.checkout, "remote", "get-url", "origin").stdout.decode().strip()
        require(discovered_remote(remote) == self.config["git_url"], "origin differs from configured git_url")
        # Repositories with local URL rewrites could send a publisher token to another host.
        rewrites = self.git(self.checkout, "config", "--includes", "--local", "--get-regexp",
                            r"^(url\.|http\.|core\.sshcommand)", check=False)
        require(rewrites.returncode == 1, "remove local Git URL/HTTP/SSH overrides for this pilot")
        return remote

    def path(self, job):
        return self.state.root / "worktrees" / str(job_id(job))

    def prepare(self, job, origin):
        worktree = self.path(job)
        if not job.get("base"):
            use_ssh = (self.config["scm"].get("credential_source") == "workspace"
                       and not origin.lower().startswith("https://"))
            if use_ssh:
                self.git(self.checkout, "fetch", "--no-tags", "--no-recurse-submodules",
                         origin, self.config["base_branch"], ssh=True)
            else:
                self.git(self.checkout, "fetch", "--no-tags", self.config["git_url"],
                         self.config["base_branch"], authenticate=True)
            job["base"] = self.git(self.checkout, "rev-parse", "FETCH_HEAD").stdout.decode().strip()
            self.state.save(job, "base-pinned")
        if not worktree.exists():
            worktree.parent.mkdir(parents=True, exist_ok=True)
            # Detached worktrees avoid local branch ownership/collision problems.
            self.git(self.checkout, "worktree", "add", "--detach", str(worktree), job["base"])
        self.verify_candidate(job)

    def verify_candidate(self, job):
        path = self.path(job)
        expected = job["base"]
        require(self.git(path, "rev-parse", "HEAD").stdout.decode().strip() == expected,
                "candidate HEAD changed unexpectedly")
        require(self.git(path, "diff", "--cached", "--quiet", "--ignore-submodules=none",
                         check=False).returncode == 0,
                "candidate index changed unexpectedly")

    def changed(self, job):
        path = self.path(job)
        tracked = self.git(path, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z",
                           "--no-renames", "--ignore-submodules=none", job["base"], "--").stdout
        others = self.git(path, "ls-files", "--others", "--exclude-standard", "-z").stdout
        return sorted(set(os.fsdecode(p) for p in (tracked + others).split(b"\0") if p))

    def gitlink_entries(self, path):
        entries = self.git(path, "ls-files", "--stage", "-z").stdout
        links = {}
        for entry in entries.split(b"\0"):
            if entry.startswith(b"160000 "):
                metadata, name = entry.split(b"\t", 1)
                _, oid, stage = metadata.split()
                require(stage == b"0", "Unmerged submodule entry requires manual handling")
                links[name] = oid.decode()
        return links

    def fingerprint(self, job):
        self.verify_candidate(job)
        root = self.path(job)
        names = self.git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z").stdout
        digest = hashlib.sha256()
        links = self.gitlink_entries(root)
        for name in sorted(set(p for p in names.split(b"\0") if p)):
            path = root / os.fsdecode(name)
            require(not any(parent.is_symlink() for parent in path.parents if parent != root
                            and root in parent.parents), "symlinked candidate directory is unsafe")
            if name in links:
                require(not path.is_symlink() and (not path.exists() or
                        (path.is_dir() and not any(path.iterdir()))),
                        "Parent worktrees leave submodules uninitialized. Run the child as a separate job.")
                record = [os.fsdecode(name), "gitlink", links[name]]
            elif path.is_symlink():
                record = [os.fsdecode(name), "symlink", os.readlink(path)]
            elif path.is_file():
                content = hashlib.sha256()
                with path.open("rb") as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        content.update(chunk)
                record = [os.fsdecode(name), bool(path.stat().st_mode & 0o111), content.hexdigest()]
            else:
                require(not path.exists(), "submodules and special files require manual handling")
                continue  # A deleted file is absent from the candidate tree.
            digest.update(json.dumps(record).encode() + b"\n")
        return digest.hexdigest()

    def commit_fingerprint(self, job):
        """Check stored Git blobs too: clean filters must not change reviewed bytes."""
        path = self.path(job)
        tree = self.git(path, "ls-tree", "-rz", "--full-tree", job["commit"]).stdout
        records = []
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            metadata, name = entry.split(b"\t", 1)
            mode, kind, oid = metadata.split()
            if mode == b"160000" and kind == b"commit":
                records.append((name, [os.fsdecode(name), "gitlink", oid.decode()]))
                continue
            require(kind == b"blob", "Unexpected Git tree entry")
            blob = self.git(path, "cat-file", "blob", oid.decode()).stdout
            record = ([os.fsdecode(name), "symlink", os.fsdecode(blob)] if mode == b"120000"
                      else [os.fsdecode(name), mode == b"100755", hashlib.sha256(blob).hexdigest()])
            records.append((name, record))
        digest = hashlib.sha256()
        for _, record in sorted(records):
            digest.update(json.dumps(record).encode() + b"\n")
        return digest.hexdigest()

    def safe_paths(self, job):
        changed = self.changed(job)
        protected = PROTECTED + tuple(self.config["protected_paths"])
        hits = [p for p in changed if any(fnmatch.fnmatchcase(p, pattern) for pattern in protected)]
        require(not hits, "protected paths changed: " + ", ".join(hits))
        gitlinks = {os.fsdecode(name) for name in self.gitlink_entries(self.path(job))}
        require(not gitlinks.intersection(changed), "Submodule pointers require a separate human change.")
        require(not any((self.path(job) / p).is_symlink() for p in changed),
                "new or changed symlinks require manual handling")
        return changed

    def quick_change(self, job, changed):
        """Escalate if the actual diff outgrows the intake brief."""
        if len(changed) > 5 or any(
            path.startswith(".") or path.endswith((".lock", ".lockb")) or
            set(Path(path.lower()).parts) & {
                "auth", "security", "infra", "migrations", "package.json", "package-lock.json",
                "pyproject.toml", "go.mod", "go.sum", "requirements.txt", "pnpm-lock.yaml",
            } for path in changed
        ):
            return False
        path = self.path(job)
        diff = self.git(path, "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                        "--numstat", "-z", job["base"], "--").stdout
        lines, tracked = 0, set()
        for entry in filter(None, diff.split(b"\0")):
            added, deleted, name = entry.split(b"\t", 2)
            if not (added.isdigit() and deleted.isdigit()):
                return False
            lines += int(added) + int(deleted)
            tracked.add(os.fsdecode(name))
        for name in set(changed) - tracked:
            file = path / name
            if not file.is_file() or file.stat().st_size > 24_000:
                return False
            content = file.read_bytes()
            if b"\0" in content:
                return False
            lines += len(content.splitlines())
        return lines <= 180

    def artifact_dir(self, job, round_number=None):
        return self.state.root / "artifacts" / str(job_id(job)) / str(round_number or job["round"])

    def stage(self, job, role):
        before = self.fingerprint(job)
        context = {"issue": job["issue"], "base_commit": job["base"], "round": job["round"],
                   "results": job["results"], "policy": {"protected_paths": list(PROTECTED)
                   + self.config["protected_paths"], "merge": "human only", "deploy": "CI only"}}
        directory = self.artifact_dir(job)
        if job.get("profile"):
            context["profile"] = job["profile"]
        if role == "build" and (failure := job.get("check_failure")):
            log = self.artifact_dir(job, failure["round"]) / f"check-{failure['index']}.log"
            context["failed_check"] = {"summary": job["results"]["feedback"],
                                       "output": check_diagnostics(log)}
        try:
            self.state.save(job, "started-" + role)
            model_settings = {"models": job["models"]} if "models" in job else {}
            result = self.runtime.run(role, self.path(job), context, directory, **model_settings)
        finally:
            identifier = recorded_agent_id(directory / (role + ".jsonl"))
            if identifier:
                attempts = job.setdefault("agents", {}).setdefault(str(job["round"]), {}).setdefault(role, [])
                if identifier not in attempts:
                    attempts.append(identifier)
                    self.state.save(job, "agent-" + role)
            if role != "build":
                try:
                    require(before == self.fingerprint(job), "read-only stage modified the candidate")
                except Refused:
                    job["stage"] = "needs-human"
                    self.state.save(job, "unexpected-candidate-mutation")
                    raise
        job["results"][role] = result
        self.state.save(job, "stage-" + role)
        return result

    def gate(self, job):
        self.state.save(job, "started-gate")
        before = self.fingerprint(job)
        changed = self.safe_paths(job)
        require(changed, "builder produced no changes")
        if job.get("profile") == "quick" and not self.quick_change(job, changed):
            return False
        directory = self.artifact_dir(job)
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="checks-", dir=directory) as home:
            for index, command in enumerate(self.config["checks"]):
                try:
                    execute(command, self.path(job), clean_env(home), self.config["check_timeout_seconds"],
                            directory / f"check-{index}.log")
                except (Refused, OSError, KeyboardInterrupt) as error:
                    self.verify_gate_fingerprint(job, before)
                    if not isinstance(error, ProcessFailed):
                        raise
                    # Persist the check locator, not the command arguments or raw output.
                    job["check_failure"] = {"index": index, "round": job["round"]}
                    label = Path(command[0]).name
                    if label in ("npm", "pnpm", "yarn") and command[1:3] in (["run", "lint"], ["run", "test"]):
                        label += " " + " ".join(command[1:3])
                    message = f"Check {index + 1} ({label}) exited with status {error.returncode}."
                    raise ProcessFailed(error.returncode, message) from None
        self.verify_gate_fingerprint(job, before)
        job.pop("check_failure", None)
        for role in ("code", "security", "ux", "ui", "handoff", "feedback"):
            job["results"].pop(role, None)
        job["fingerprint"] = before
        job["results"]["gate"] = {"verdict": "PASS", "fingerprint": before}
        self.state.save(job, "gate-pass")
        return True

    def verify_gate_fingerprint(self, job, before):
        try:
            require(before == self.fingerprint(job), "a configured check modified candidate files")
        except Refused:
            self.state.save(job, "check-candidate-mutation")
            raise

    def retry(self, job, why):
        if job["round"] - job.get("round_start", 1) + 1 >= self.config["max_rounds"]:
            job["stage"] = "needs-human"
        else:
            job["round"] += 1
            job["stage"] = "build"
        job["results"]["feedback"] = why
        self.state.save(job, "revision-required")

    def reopen(self, job):
        require(not self.state.has_event(job, "unexpected-candidate-mutation"),
                "a read-only stage modified this candidate; inspect it and use fresh factory state")
        require(not self.state.has_event(job, "check-candidate-mutation"),
                "a configured check modified this candidate; inspect it and use fresh factory state")
        changed = self.safe_paths(job)
        results = job["results"]
        specified = results.get("spec", {}).get("verdict") == "READY" or job.get("profile") == "quick"
        job["round"] += 1
        job["round_start"] = job["round"]
        job.pop("fingerprint", None)
        for role in ("gate", "code", "security", "ux", "ui", "handoff"):
            results.pop(role, None)
        if changed and specified:
            job["stage"] = "gate"
            if results.get("build", {}).get("verdict") == "BLOCKED":
                results.pop("build")
        else:
            job["stage"] = "build" if specified else "spec"
        self.state.save(job, "resumed")

    def full_pipeline(self, job):
        job["profile"] = "full"
        for role in ("gate", "code", "security", "ux", "ui", "handoff"):
            job["results"].pop(role, None)
        job.pop("fingerprint", None)
        job["stage"] = "intake"
        self.state.save(job, "expanded-review")

    def run(self, number, route=None, *, resume=False):
        origin = self.verify_checkout()
        current = route.current_issue() if route else self.provider.issue(number)
        job = self.state.get(-number if route else number)
        if not job:
            require(not resume, "no existing factory run for this issue")
            job = {"issue": current, "branch": "factory/issue-" + str(number), "stage": "intake",
                   "round": 1, "results": {}}
            if route:
                job.update(id=-number, branch=route.branch, planner_id=route.planner_id)
                if route.quick:
                    job.update(profile="quick", stage="build")
            if self.config.get("model_source") == "workspace":
                job["models"] = workspace_models(workspace_parent(self.checkout))
            self.state.save(job, "created")
        if job["stage"] in STOP and not route and not resume:
            return job
        require(current == job["issue"], "issue changed; inspect it and use a fresh state_dir")
        if job["stage"] in STOP and not (resume and job["stage"] == "needs-human"):
            require(not resume or job["stage"] in ("ready", "published"),
                    f"a {job['stage']} run cannot resume; resolve intake and start with fresh factory state")
            return job
        self.prepare(job, origin)
        if resume and (job["stage"] == "needs-human" or (
            job["stage"] in ("review", "handoff") and self.fingerprint(job) != job.get("fingerprint")
        )):
            self.reopen(job)
        try:
            while job["stage"] not in STOP:
                stage = job["stage"]
                if stage in ("intake", "spec", "build"):
                    result = self.stage(job, stage)
                    verdict = result["verdict"]
                    if stage == "intake":
                        if (verdict == "QUICK" and job.get("profile") != "full"
                                and result["risk"] == "low" and "security" not in result["surfaces"]):
                            job.update(profile="quick", stage="build")
                        else:
                            job["stage"] = {"ACCEPT": "spec", "QUICK": "spec", "NEEDS-INFO": "needs-info",
                                            "REJECT": "rejected"}[verdict]
                    elif stage == "build" and job.get("profile") == "quick" and (
                        result["risk"] != "low" or "security" in result["surfaces"]
                    ):
                        self.full_pipeline(job)
                    else:
                        job["stage"] = ("gate" if stage == "build" else "build") if verdict == "READY" else "needs-human"
                elif stage == "gate":
                    try:
                        passed = self.gate(job)
                    except ProcessFailed as error:
                        self.retry(job, str(error))
                        continue
                    except Refused as error:
                        job["stage"] = "needs-human"
                        job["results"]["feedback"] = str(error)
                        self.state.save(job, "revision-required")
                        continue
                    if passed:
                        job["stage"] = "review"
                    else:
                        self.full_pipeline(job)
                elif stage == "review":
                    require(self.fingerprint(job) == job["fingerprint"], "candidate changed since gate")
                    results = [self.stage(job, role) for role in review_roles(job)]
                    if job.get("profile") == "quick" and results[0]["risk"] != "low":
                        self.full_pipeline(job)
                    elif any(result["verdict"] == "CHANGES" for result in results):
                        self.retry(job, "Address every review marked CHANGES.")
                        continue
                    else:
                        job["stage"] = "handoff"
                elif stage == "handoff":
                    require(self.fingerprint(job) == job["fingerprint"], "candidate changed since review")
                    if job.get("profile") == "quick":
                        job["results"]["handoff"] = {"verdict": "READY", "report":
                            "Configured checks and independent code and security reviews passed. "
                            "Review the local diff and verify the issue’s acceptance checks before merging."}
                        self.state.save(job, "stage-handoff")
                        job["stage"] = "ready"
                    else:
                        job["stage"] = "ready" if self.stage(job, stage)["verdict"] == "READY" else "needs-human"
                else:
                    raise Refused("unknown persisted stage")
                self.state.save(job, "transition")
        except (Refused, OSError, ValueError):
            # Keep the stage: a restart redoes it, without inferring success from partial logs.
            self.state.save(job, "interrupted")
            raise
        return job

    def publish(self, number, route=None):
        self.verify_checkout()
        job = self.state.get(-number if route else number)
        require(job is not None, "unknown job")
        if job["stage"] == "published":
            return job
        require(job["stage"] == "ready", "only a ready candidate can be published")
        current = route.current_issue() if route else self.provider.issue(number)
        require(current == job["issue"], "issue changed since review")
        require(self.fingerprint(job) == job["fingerprint"], "candidate changed since review")
        self.safe_paths(job)
        # Refuse stale bases: re-running tests on a different tree is a new candidate.
        self.git(self.checkout, "fetch", "--no-tags", self.config["git_url"],
                 self.config["base_branch"], authenticate=True)
        latest = self.git(self.checkout, "rev-parse", "FETCH_HEAD").stdout.decode().strip()
        require(latest == job["base"], "base branch advanced; create and review a fresh candidate")
        path = self.path(job)
        if not job.get("commit"):
            paths = self.changed(job)
            with tempfile.TemporaryDirectory(prefix="index-", dir=self.state.root) as temp:
                index = Path(temp) / "index"
                self.git(path, "read-tree", job["base"], index_file=index)
                self.git(path, "add", "--", *paths, index_file=index)
                tree = self.git(path, "write-tree", index_file=index).stdout.decode().strip()
            # commit-tree avoids hooks and leaves HEAD untouched until the durable checkpoint.
            commit = self.git(path, "commit-tree", tree, "-p", job["base"], "-m",
                              "Factory: issue #" + str(number)).stdout.decode().strip()
            job["commit"] = commit
            self.state.save(job, "commit-prepared")
        # Recover an interrupted publication without creating a second commit.
        require(self.fingerprint(job) == job["fingerprint"], "candidate changed during publication")
        require(self.commit_fingerprint(job) == job["fingerprint"],
                "Git commit bytes differ from the reviewed candidate (for example, a clean filter)")
        remote = self.git(path, "ls-remote", "--exit-code", self.config["git_url"],
                          "refs/heads/" + job["branch"], authenticate=True, check=False)
        require(remote.returncode == 2 or (remote.returncode == 0 and
                remote.stdout.decode().split()[0] == job["commit"]),
                "remote candidate branch exists at another commit; inspect manually")
        # Only create this branch or repeat an identical push. Never force-update a remote branch.
        self.git(path, "push", self.config["git_url"], job["commit"] + ":refs/heads/" + job["branch"],
                 authenticate=True)
        request = self.provider.create_request(job["issue"], job["branch"], self.config["base_branch"])
        job["request_url"] = request.get("html_url") or request["web_url"]
        job["release_intent"] = {"environment": "staging", "commit": job["commit"],
                                 "base": job["base"], "repository": self.config["scm"]["repository"],
                                 "provider": self.config["scm"]["provider"],
                                 "production_authorization": "human-manual-merge"}
        job["stage"] = "published"
        self.state.save(job, "published")
        return job
