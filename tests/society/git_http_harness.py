"""A tiny local git smart-HTTP server with HTTP Basic authentication.

Used by ``tests/society/test_github_credentials_and_push.py`` to prove that
the promotion controller pushes through ``GIT_ASKPASS`` (username
``x-access-token`` + token) against a REAL ``git push`` over HTTP without
the token ever entering the URL, argv or the repository configuration.

It fronts ``git http-backend`` (CGI, shipped with git) for bare repositories
under ``root``; ``REMOTE_USER`` is set only after a correct ``Authorization``
header, which is what allows ``git-receive-pack`` (push) at all.
"""

from __future__ import annotations

import base64
import os
import pathlib
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List


class GitHTTPServer:
    def __init__(self, root: pathlib.Path, *, username: str, token: str):
        self.root = pathlib.Path(root)
        self._expected = "Basic " + base64.b64encode(f"{username}:{token}".encode()).decode()
        self.username = username
        self.auth_attempts: List[str] = []   # "none" | "ok" | "bad", in order
        self.paths: List[str] = []           # request paths as seen on the wire
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a, **k):  # quiet
                return None

            def do_GET(self):
                self._handle()

            def do_POST(self):
                self._handle()

            def _handle(self):
                server.paths.append(self.path)
                auth = self.headers.get("Authorization")
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                if not auth or auth != server._expected:
                    server.auth_attempts.append("none" if not auth else "bad")
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="agentnet-test"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                server.auth_attempts.append("ok")
                path, _, query = self.path.partition("?")
                env = {
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "GIT_PROJECT_ROOT": str(server.root),
                    "GIT_HTTP_EXPORT_ALL": "1",
                    "REQUEST_METHOD": self.command,
                    "PATH_INFO": path,
                    "QUERY_STRING": query,
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(length),
                    "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
                    "REMOTE_USER": server.username,
                    "REMOTE_ADDR": "127.0.0.1",
                    "SERVER_PROTOCOL": "HTTP/1.1",
                }
                proc = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True, check=False)
                out = proc.stdout
                head, sep, payload = out.partition(b"\r\n\r\n")
                if not sep:
                    head, sep, payload = out.partition(b"\n\n")
                status = 200
                headers = []
                for line in head.decode("latin-1").splitlines():
                    if not line.strip():
                        continue
                    name, _, value = line.partition(":")
                    if name.strip().lower() == "status":
                        status = int(value.strip().split()[0])
                    else:
                        headers.append((name.strip(), value.strip()))
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> "GitHTTPServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> "GitHTTPServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
