"""Read-only views of recorded factory runs in the current project or workspace."""

import fcntl
import json
import sqlite3
import webbrowser
from contextlib import closing
from http.server import HTTPServer
from itertools import groupby
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import Refused, load
from .engine import job_id, review_roles
from .local_ui import LocalHandler, private_url


ASSETS = Path(__file__).resolve().with_name("dashboard_ui")
ROLES = ("intake", "spec", "build", "gate", "code", "security", "ux", "ui", "handoff")


def database(settings):
    if settings is None or not settings.is_file():
        return None
    path = Path(load(settings)["state_dir"]) / "state.sqlite3"
    return path if path.is_file() else None


def summarize(job, when=None, last_event=None, started=None, active=False):
    issue = job["issue"]
    role = last_event.removeprefix("started-") if last_event else None
    return {"id": job_id(job), "number": issue["number"], "title": issue["title"], "issue_url": issue.get("url"),
            "workspace_task": bool(issue.get("workspace_parent")),
            "profile": job.get("profile"),
            "request_url": job.get("request_url"), "stage": job["stage"], "round": job["round"],
            "risk": job["results"].get("intake", {}).get("risk"), "updated": when, "last_event": last_event,
            "started": started, "active_role": role if active and role in ROLES else None,
            "review_roles": review_roles(job)}


def open_reader(path):
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)


def worker_busy(path):
    try:
        with path.with_name("worker.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except FileNotFoundError:
        pass
    return False


def list_runs(settings):
    path = database(settings)
    if not path:
        return []
    with closing(open_reader(path)) as reader:
        reader.execute("BEGIN")
        busy = worker_busy(path)
        rows = reader.execute("""
            WITH bounds AS (SELECT job, MIN(sequence) AS first, MAX(sequence) AS last FROM events GROUP BY job)
            SELECT jobs.id, jobs.value, latest.at, latest.event, first.at,
                   latest.sequence = (SELECT MAX(sequence) FROM events)
            FROM jobs LEFT JOIN bounds ON bounds.job = jobs.id
            LEFT JOIN events AS latest ON latest.sequence = bounds.last
            LEFT JOIN events AS first ON first.sequence = bounds.first
            ORDER BY bounds.last DESC, jobs.id DESC LIMIT 200
        """).fetchall()
        jobs = {number: summarize(json.loads(raw), updated, event, started, busy and latest)
                for number, raw, updated, event, started, latest in rows}
        if jobs:
            placeholders = ", ".join("?" for _ in jobs)
            snapshots = reader.execute(f"""
                SELECT job, at, event, value FROM events WHERE job IN ({placeholders})
                AND (event LIKE 'stage-%' OR event IN ('started-gate', 'gate-pass', 'revision-required', 'transition'))
                ORDER BY job, sequence
            """, tuple(jobs))
            for number, entries in groupby(snapshots, key=lambda row: row[0]):
                rounds, _ = recorded_results((row[1:] for row in entries), include_reports=False)
                jobs[number]["rounds"] = rounds
        return list(jobs.values())


def run_detail(settings, number):
    path = database(settings)
    if not path:
        return None
    with closing(open_reader(path)) as reader:
        reader.execute("BEGIN")
        row = reader.execute("SELECT value FROM jobs WHERE id=?", (number,)).fetchone()
        if row is None:
            return None
        job = json.loads(row[0])
        created = reader.execute("SELECT at FROM events WHERE job=? ORDER BY sequence LIMIT 1", (number,)).fetchone()
        last_worker_job = reader.execute("SELECT job FROM events ORDER BY sequence DESC LIMIT 1").fetchone()
        active = last_worker_job == (number,) and worker_busy(path)
        events = reader.execute("""
            SELECT at, event, value FROM (
                SELECT sequence, at, event, value FROM events
                WHERE job=? ORDER BY sequence DESC LIMIT 300
            ) ORDER BY sequence
        """, (number,))
        attempts, timeline = recorded_results(events)
    latest = timeline[-1] if timeline else {}
    started = created[0] if created else None
    return {"job": summarize(job, latest.get("at"), latest.get("event"), started, active),
            "started": started, "rounds": attempts, "events": timeline,
            "agents": job.get("agents", {}), "planner_id": job.get("planner_id")}


def recorded_results(events, include_reports=True):
    attempts = {}
    timeline = []
    previous = None
    for at, event, raw in events:
        snapshot = json.loads(raw)
        stage, round_number = snapshot["stage"], snapshot["round"]
        role = event.removeprefix("stage-") if event.startswith("stage-") else None
        if event == "gate-pass":
            role = "gate"
        result = snapshot["results"].get(role) if role in ROLES else None
        if result:
            attempts.setdefault(round_number, {})[role] = {"verdict": result.get("verdict")}
            if include_reports:
                attempts[round_number][role]["report"] = (result.get("report") or "")[:12000]
        if event == "revision-required" and previous and previous[0] == "gate":
            attempts.setdefault(previous[1], {})["gate"] = {"verdict": "FAIL"}
            if include_reports:
                attempts[previous[1]]["gate"]["report"] = (snapshot["results"].get("feedback") or "")[:12000]
        if include_reports:
            timeline.append({"at": at, "event": event, "stage": stage, "round": round_number})
        previous = (stage, round_number)
    return attempts, timeline


def server_for(project, repositories):
    class Handler(LocalHandler):
        def do_GET(self):
            if not self.valid_host():
                return self.reply(403, {"error": "Invalid dashboard host."})
            url = urlsplit(self.path)
            if not url.path.startswith("/api/"):
                return self.asset(url.path, ASSETS)
            if not self.authorized():
                return self.reply(403, {"error": "Open the private dashboard URL printed in your terminal."})
            if url.path == "/api/runs":
                payload = []
                for name, settings in repositories.items():
                    status = "configured" if settings and settings.is_file() else "setup-required"
                    try:
                        runs = list_runs(settings)
                    except (sqlite3.Error, OSError, ValueError, KeyError, Refused):
                        status, runs = "unavailable", []
                    payload.append({"name": name, "status": status, "runs": runs})
                return self.reply(200, {"project": project, "repositories": payload})
            if url.path == "/api/run":
                fields = parse_qs(url.query)
                try:
                    name, = fields["repository"]
                    value, = fields["issue"]
                    number = int(value)
                    if number == 0 or abs(number) > 2**63 - 1:
                        raise ValueError
                    detail = run_detail(repositories[name], number)
                except (KeyError, ValueError):
                    return self.reply(404, {"error": "Run not found in this workspace."})
                except (sqlite3.Error, OSError, Refused):
                    return self.reply(503, {"error": "The run could not be read. Try again shortly."})
                if detail is None:
                    return self.reply(404, {"error": "Run not found in this workspace."})
                return self.reply(200, detail)
            return self.reply(404, {"error": "Page unavailable."})

        def do_POST(self):
            return self.reply(405, {"error": "The dashboard is read-only."})

    server = HTTPServer(("127.0.0.1", 0), Handler)
    return server, private_url(server)


def serve_dashboard(project, repositories, open_browser=True):
    server, url = server_for(project, repositories)
    with server:
        print(f"Open factory dashboard: {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
