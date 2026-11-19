"""Shared response and session protections for local browser tools."""

import hmac
import json
import secrets
from http.server import BaseHTTPRequestHandler


ASSETS = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
          "/style.css": ("style.css", "text/css")}


def private_url(server):
    server.session_token = secrets.token_urlsafe(32)
    return f"http://127.0.0.1:{server.server_port}/#{server.session_token}"


class LocalHandler(BaseHTTPRequestHandler):
    timeout = 5

    def log_message(self, *args):
        pass  # Do not log private browser requests.

    def reply(self, status, data, kind="application/json; charset=utf-8"):
        if not isinstance(data, bytes):
            data = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; "
                         "style-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
                         "base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        return hmac.compare_digest(self.headers.get("X-Factory-Session", "").encode(),
                                   self.server.session_token.encode())

    def valid_host(self):
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def asset(self, path, directory):
        if path not in ASSETS:
            return self.reply(404, {"error": "Page unavailable."})
        name, mime = ASSETS[path]
        return self.reply(200, (directory / name).read_bytes(), mime + "; charset=utf-8")
