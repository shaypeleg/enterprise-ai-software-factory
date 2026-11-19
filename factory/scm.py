"""Small GitHub/GitLab adapters. Intentionally no merge/approve/deploy API."""

import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import Refused, require
from .credentials import repository_credential


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward repository credentials to a redirected URL.


class SCMHTTPError(Refused):
    def __init__(self, status):
        self.status = status
        super().__init__(f"SCM HTTP {status}; no response body logged")


class Provider:
    def __init__(self, config, *, checkout=None, git_url=None):
        self.config = config
        self.checkout, self.git_url = checkout, git_url
        self.github = config["provider"] == "github"
        self.root = config["api_url"].rstrip("/")
        repository = config["repository"]
        self.project = ("/repos/" + repository if self.github
                        else "/projects/" + quote(repository, safe=""))
        self.opener = build_opener(NoRedirect())

    def request(self, method, path, data=None, *, attempts=3, timeout=20):
        _, token = repository_credential(self.config, self.checkout, self.git_url)
        headers = {"Accept": "application/json", "User-Agent": "codex-factory-pilot/1"}
        headers.update({"Authorization": "Bearer " + token} if self.github else {"PRIVATE-TOKEN": token})
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = Request(self.root + path, method=method, headers=headers,
                      data=None if data is None else json.dumps(data).encode())
        for attempt in range(attempts):
            try:
                with self.opener.open(req, timeout=timeout) as response:
                    body = response.read(2 * 1024 * 1024 + 1)
                    require(len(body) <= 2 * 1024 * 1024, "SCM response is too large")
                    return json.loads(body)
            except HTTPError as error:
                if method == "GET" and (error.code == 429 or error.code >= 500) and attempt + 1 < attempts:
                    time.sleep(2 ** attempt)
                    continue
                raise SCMHTTPError(error.code) from None
            except (URLError, TimeoutError):
                if method == "GET" and attempt + 1 < attempts:
                    time.sleep(2 ** attempt)
                    continue
                raise Refused("SCM request failed; check connectivity and credentials") from None

    def test_issue_access(self):
        # Exercise the same repository issue route as queue(), with a single read.
        rows = self.request("GET", self.project + "/issues?per_page=1", attempts=1, timeout=8)
        require(isinstance(rows, list), "The endpoint returned an unexpected response for repository issues.")

    def pages(self, path, params):
        for page in range(1, 101):
            rows = self.request("GET", self.project + path + "?" + urlencode(
                dict(params, per_page=100, page=page)))
            require(isinstance(rows, list), "unexpected SCM list response")
            yield from rows
            if len(rows) < 100:
                return
        raise Refused("SCM pagination limit exceeded; narrow the queue")

    def normalize(self, row):
        return {"number": row["number"] if self.github else row["iid"],
                "title": row["title"], "body": (row.get("body") if self.github
                else row.get("description")) or "", "url": row["html_url"] if self.github
                else row["web_url"], "state": row["state"],
                "labels": [x["name"] for x in row["labels"]] if self.github else row["labels"]}

    def queue(self):
        params = {"state": "open" if self.github else "opened",
                  "labels": self.config["queue_label"]}
        return [self.normalize(row) for row in self.pages("/issues", params)
                if not row.get("pull_request")]

    def issue(self, number):
        require(type(number) is int and number > 0, "issue must be a positive integer")
        row = self.request("GET", self.project + "/issues/" + str(number))
        require(not row.get("pull_request"), "pull requests cannot enter the issue queue")
        issue = self.normalize(row)
        require(issue["state"] in ("open", "opened"), "issue is closed")
        require(self.config["queue_label"] in issue["labels"], "issue lacks the queue label")
        return issue

    def find_request(self, branch, base):
        path = "/pulls" if self.github else "/merge_requests"
        params = ({"state": "all", "head": self.config["repository"].split("/")[0] + ":" + branch,
                   "base": base} if self.github else
                  {"state": "all", "source_branch": branch, "target_branch": base})
        rows = list(self.pages(path, params))
        require(len(rows) <= 1, "multiple PRs/MRs for this branch; inspect manually")
        return rows[0] if rows else None

    def create_request(self, issue, branch, base):
        existing = self.find_request(branch, base)
        if existing:
            require(existing["state"] in ("open", "opened"), "existing PR/MR is closed; inspect manually")
            return existing
        reference = f"Part of {issue['url']}" if issue.get("workspace_parent") else f"Closes #{issue['number']}"
        body = (f"{reference}\n\nFactory checks and reviews passed. Trusted CI may deploy this "
                "candidate to staging automatically. Production requires human review and a "
                "manual merge. This publisher performed no deployment.")
        title = issue["title"][:200]
        data = ({"title": title, "body": body, "head": branch, "base": base, "draft": True}
                if self.github else {"title": "Draft: " + title, "description": body,
                "source_branch": branch, "target_branch": base, "remove_source_branch": False})
        # No POST retry: after an ambiguous failure, the next invocation reconciles by branch.
        return self.request("POST", self.project + ("/pulls" if self.github else "/merge_requests"), data)

    def update_workspace_note(self, number, message):
        marker = "<!-- enterprise-ai-software-factory:workspace-cascade -->"
        body = marker + "\n" + message
        path = f"/issues/{number}/" + ("comments" if self.github else "notes")
        identity = self.request("GET", "/user")["id"]
        require(type(identity) is int and identity > 0, "Could not verify the factory’s Git identity.")
        author_field = "user" if self.github else "author"
        notes = [note for note in self.pages(path, {})
                 if (note.get(author_field) or {}).get("id") == identity
                 and (note.get("body") or "").startswith(marker + "\n")]
        require(len(notes) <= 1, "Multiple factory workspace notes exist. Inspect the parent issue.")
        if not notes:
            self.request("POST", self.project + path, {"body": body})
        elif notes[0]["body"] != body:
            note_id = notes[0]["id"]
            require(type(note_id) is int and note_id > 0, "Invalid workspace note ID.")
            edit_path = f"/issues/comments/{note_id}" if self.github else f"{path}/{note_id}"
            self.request("PATCH" if self.github else "PUT", self.project + edit_path, {"body": body})
