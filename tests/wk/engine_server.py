"""Loopback server for the ENGINE acceptance pages (tests/wk/test_wk_phase1_video.py).

A drop-in for :class:`harness.PageServer` (same ``box`` mailbox and ``url``,
so :class:`harness.WKHarness` drives it unchanged) that also serves proxy
directories at the product's route shape, ``/api/proxies/<key>/index.json``,
``init.mp4`` and ``v/NNNN.bin`` (spec §5.2), with what the engine relies on:

* HTTP ``Range`` (206) on span packs — the ProxyStore reads a cold span's
  header and one sample first;
* scripted DELAYS per path (P1-F5 "the span route is delayed by 1 s"),
  scripted ``202 Accepted`` + ``Retry-After`` answers (on-demand encodes)
  and scripted error answers (``fail``: a transient 5xx, a 410);
* a request log with timestamps, so a test can see what was fetched when.
"""
from __future__ import annotations

import mimetypes
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlencode

from .harness import _TOKEN, _Mailbox, window_command

_PROXY = re.compile(r"^/api/proxies/([A-Za-z0-9_-]{1,64})/(index\.json|init\.mp4|v/\d{4}\.bin)$")
_RANGE = re.compile(r"^bytes=(\d+)-(\d*)$")


class EngineServer:
    def __init__(self, mounts: dict[str, Path], proxies: dict[str, Path] | None = None, port: int = 0):
        self.mounts = {k.strip("/"): Path(v).resolve() for k, v in mounts.items()}
        self.mount_order = sorted(self.mounts, key=len, reverse=True)
        self.proxies = {k: Path(v).resolve() for k, v in (proxies or {}).items()}
        self.box = _Mailbox()
        self.request_log: list[dict] = []
        self.delays: dict[str, float] = {}
        self.pending: dict[str, int] = {}
        #: path -> [status, answers left]
        self.failing: dict[str, list[int]] = {}
        self._lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                return

            def _send(self, status: int, body: bytes = b"", ctype: str = "text/plain",
                      headers: dict[str, str] | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if body:
                    try:
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("content-length", 0) or 0)
                body = self.rfile.read(n)
                if window_command(server.box, self.path):
                    return self._send(204)
                m = re.match(r"^/__(result|error)/([^/?]+)", self.path)
                if not m or not _TOKEN.match(m.group(2)):
                    return self._send(404)
                kind, token = m.groups()
                with server.box.cond:
                    if kind == "result":
                        server.box.results[token] = body
                        done = server.box.done_files.get(token)
                        if done is not None:
                            done.write_text("done")
                    else:
                        server.box.errors.setdefault(token, []).append(body.decode("utf-8", "replace"))
                    server.box.cond.notify_all()
                return self._send(204)

            def do_GET(self):  # noqa: N802
                path = unquote(self.path.split("?", 1)[0])
                rng = self.headers.get("Range")
                entry = {"path": path, "range": rng, "t": time.monotonic(), "status": 0}
                with server._lock:
                    server.request_log.append(entry)
                m = _PROXY.match(path)
                if m:
                    return self._proxy(m.group(1), m.group(2), rng, entry)
                rel = path.lstrip("/")
                prefix = next((p for p in server.mount_order if rel.startswith(p + "/")), None)
                if prefix is None:
                    entry["status"] = 404
                    return self._send(404)
                root = server.mounts[prefix]
                target = (root / rel[len(prefix) + 1:]).resolve()
                if not target.is_relative_to(root) or not target.is_file():
                    entry["status"] = 404
                    return self._send(404)
                ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                if target.suffix in (".js", ".mjs"):
                    ctype = "text/javascript"
                entry["status"] = 200
                return self._send(200, target.read_bytes(), ctype)

            def _proxy(self, key: str, rel: str, rng: str | None, entry: dict) -> None:
                root = server.proxies.get(key)
                if root is None:
                    entry["status"] = 404
                    return self._send(404)
                full = f"/api/proxies/{key}/{rel}"
                with server._lock:
                    delay = server.delays.get(full, 0.0)
                    left = server.pending.get(full, 0)
                    if left:
                        server.pending[full] = left - 1
                    fail = server.failing.get(full)
                    fail_status = 0
                    if fail and fail[1] > 0:
                        fail[1] -= 1
                        fail_status = fail[0]
                if fail_status:
                    entry["status"] = fail_status
                    return self._send(fail_status, b"injected", "text/plain")
                if left:
                    entry["status"] = 202
                    return self._send(202, b'{"status":"pending"}', "application/json",
                                      {"Retry-After": "0.05"})
                if delay:
                    time.sleep(delay)
                target = (root / rel).resolve()
                if not target.is_relative_to(root) or not target.is_file():
                    entry["status"] = 404
                    return self._send(404)
                data = target.read_bytes()
                ctype = {"index.json": "application/json", "init.mp4": "video/mp4"}.get(rel, "application/octet-stream")
                r = _RANGE.match(rng or "")
                if r and rel.startswith("v/"):
                    a = int(r.group(1))
                    b = min(len(data) - 1, int(r.group(2)) if r.group(2) else len(data) - 1)
                    if a >= len(data):
                        entry["status"] = 416
                        return self._send(416, headers={"Content-Range": f"bytes */{len(data)}"})
                    entry["status"] = 206
                    return self._send(206, data[a:b + 1], ctype, {"Content-Range": f"bytes {a}-{b}/{len(data)}",
                                                                  "Accept-Ranges": "bytes"})
                entry["status"] = 200
                return self._send(200, data, ctype, {"Accept-Ranges": "bytes"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="wk-engine-server", daemon=True)
        self._thread.start()

    def url(self, path: str, query: dict[str, object] | None = None) -> str:
        q = f"?{urlencode(query)}" if query else ""
        return f"http://127.0.0.1:{self.port}/{path.lstrip('/')}{q}"

    def set_delay(self, path: str, seconds: float) -> None:
        with self._lock:
            self.delays[path] = seconds

    def fail(self, path: str, status: int, times: int = 1) -> None:
        """Answer the next ``times`` GETs of ``path`` with ``status``."""
        with self._lock:
            self.failing[path] = [status, times]

    def clear(self) -> None:
        with self._lock:
            self.delays.clear()
            self.pending.clear()
            self.failing.clear()
            self.request_log.clear()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
