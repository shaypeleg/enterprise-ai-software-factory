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
from .runtime import clean_env, execute


PROTECTED = (".git*", ".github/**", ".gitlab/**", ".codex/**", ".agents/**",
             "AGENTS.md", "**/AGENTS.md", ".factory-settings.json", ".factory-workspace.json", "factory/**")
STOP = {"ready", "needs-human", "needs-info", "rejected", "published"}


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
            self.db.execute("INSERT OR REPLACE INTO jobs VALUES (?, ?)", (job["issue"]["number"], value))
            self.db.execute("INSERT INTO events(job, at, event, value) VALUES (?, ?, ?, ?)",
                            (job["issue"]["number"], datetime.now(timezone.utc).isoformat(), event, value))

    def all(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT value FROM jobs ORDER BY id")]


class Engine:
    def __init__(self, config, state, provider, runtime):
        self.config, self.state, self.provider, self.runtime = config, state, provider, runtime
        self.checkout = Path(config["checkout"])

    def git(self, cwd, *args, authenticate=False, check=True, index_file=None):
        # Ignore global/system Git credentials and hooks. Only publisher/fetch receives SCM auth.
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

    def path(self, job):
        return self.state.root / "worktrees" / str(job["issue"]["number"])

    def prepare(self, job):
        worktree = self.path(job)
        if not job.get("base"):
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

    def stage(self, job, role):
        before = self.fingerprint(job)
        context = {"issue": job["issue"], "base_commit": job["base"], "round": job["round"],
                   "results": job["results"], "policy": {"protected_paths": list(PROTECTED)
                   + self.config["protected_paths"], "merge": "human only", "deploy": "CI only"}}
        directory = self.state.root / "artifacts" / str(job["issue"]["number"]) / str(job["round"])
        try:
            result = self.runtime.run(role, self.path(job), context, directory)
        finally:
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
        before = self.fingerprint(job)
        changed = self.safe_paths(job)
        require(changed, "builder produced no changes")
        directory = self.state.root / "artifacts" / str(job["issue"]["number"]) / str(job["round"])
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="checks-", dir=directory) as home:
            for index, command in enumerate(self.config["checks"]):
                execute(command, self.path(job), clean_env(home), self.config["check_timeout_seconds"],
                        directory / ("check-%s.log" % index))
        require(before == self.fingerprint(job), "checks modified candidate files")
        job["fingerprint"] = before
        job["results"]["gate"] = {"verdict": "PASS", "fingerprint": before}
        self.state.save(job, "gate-pass")

    def retry(self, job, why):
        if job["round"] >= self.config["max_rounds"]:
            job["stage"] = "needs-human"
        else:
            job["round"] += 1
            job["stage"] = "build"
        job["results"]["feedback"] = why
        self.state.save(job, "revision-required")

    def run(self, number):
        self.verify_checkout()
        current = self.provider.issue(number)  # Recheck open/label before any resumed work.
        job = self.state.get(number)
        if not job:
            job = {"issue": current, "branch": "factory/issue-" + str(number), "stage": "intake",
                   "round": 1, "results": {}}
            self.state.save(job, "created")
        if job["stage"] in STOP:
            return job
        require(current == job["issue"], "issue changed; inspect it and use a fresh state_dir")
        self.prepare(job)
        try:
            while job["stage"] not in STOP:
                stage = job["stage"]
                if stage in ("intake", "spec", "build"):
                    result = self.stage(job, stage)
                    verdict = result["verdict"]
                    if stage == "intake":
                        job["stage"] = {"ACCEPT": "spec", "NEEDS-INFO": "needs-info",
                                        "REJECT": "rejected"}[verdict]
                    else:
                        job["stage"] = ("gate" if stage == "build" else "build") if verdict == "READY" else "needs-human"
                elif stage == "gate":
                    try:
                        self.gate(job)
                    except Refused as error:
                        self.retry(job, str(error))
                        continue
                    job["stage"] = "review"
                elif stage == "review":
                    require(self.fingerprint(job) == job["fingerprint"], "candidate changed since gate")
                    roles = ["code", "security"]
                    if "ui" in job["results"]["intake"]["surfaces"]:
                        roles += ["ux", "ui"]
                    results = [self.stage(job, role) for role in roles]
                    if any(result["verdict"] == "CHANGES" for result in results):
                        self.retry(job, "Address every review marked CHANGES.")
                        continue
                    job["stage"] = "handoff"
                elif stage == "handoff":
                    require(self.fingerprint(job) == job["fingerprint"], "candidate changed since review")
                    job["stage"] = "ready" if self.stage(job, stage)["verdict"] == "READY" else "needs-human"
                else:
                    raise Refused("unknown persisted stage")
                self.state.save(job, "transition")
        except (Refused, OSError, ValueError):
            # Keep the stage: a restart redoes it, without inferring success from partial logs.
            self.state.save(job, "interrupted")
            raise
        return job

    def publish(self, number):
        self.verify_checkout()
        job = self.state.get(number)
        require(job is not None, "unknown job")
        if job["stage"] == "published":
            return job
        require(job["stage"] == "ready", "only a ready candidate can be published")
        require(self.provider.issue(number) == job["issue"], "issue changed since review")
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
