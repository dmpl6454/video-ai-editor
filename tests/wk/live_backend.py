"""A REAL backend for WK pages that talk to the app (wave D verify suites).

``LiveBackend`` runs the FastAPI app under uvicorn in a thread on a free
loopback port, with a scratch WORKDIR, and serves ``/wk/*`` from a static
directory on the SAME origin — so a page's ``fetch('/api/...')`` is a
same-origin request exactly as in the desktop app (the Host allowlist and
cross-site refusal run unchanged). The harness's own ``PageServer`` keeps
the result mailbox; ``GO_PAGE`` is a one-line redirect from it onto the
backend's origin, passing the token and the mailbox origin along, and pages
post their result there with a no-cors ``fetch``.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from pathlib import Path

GO_PAGE = """<!doctype html><meta charset="utf-8"><script>
const q = new URLSearchParams(location.search)
const to = new URL(q.get('to'))
to.searchParams.set('token', q.get('token'))
to.searchParams.set('mailbox', location.origin)
location.replace(to.href)
</script>"""


def module_page(title: str, bundle: str, fn: str) -> str:
    """A page that runs ``fn(config)`` from ``/wk/bundle/<bundle>`` with
    ``/wk/config.json`` and posts the result (or the fatal error)."""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title></head>
<body style="margin:0"><script type="module">
import {{ {fn} }} from '/wk/bundle/{bundle}'
const q = new URLSearchParams(location.search)
const post = (body) => fetch(q.get('mailbox') + '/__result/' + q.get('token'),
  {{ method: 'POST', mode: 'no-cors', body: JSON.stringify(body) }})
try {{
  const cfg = await (await fetch('/wk/config.json')).json()
  await post(await {fn}(cfg))
}} catch (e) {{ await post({{ fatal: String((e && e.stack) || e) }}) }}
</script></body></html>
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_json(base: str, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read() or b"{}")


class LiveBackend:
    """The app under uvicorn in a thread, plus /wk/* static files."""

    def __init__(self, workdir: Path, static_root: Path):
        import uvicorn
        from starlette.staticfiles import StaticFiles

        from video_ai_editor import main as _main, storage as _storage
        workdir.mkdir(parents=True, exist_ok=True)
        self._saved = (_storage.WORKDIR, _main.WORKDIR)
        _storage.WORKDIR = workdir
        _main.WORKDIR = workdir
        _main._STORES.clear()
        static = StaticFiles(directory=str(static_root))
        app = _main.app

        async def combined(scope, receive, send):
            if scope["type"] == "http" and scope["path"].startswith("/wk/"):
                return await static({**scope, "root_path": "/wk"}, receive, send)
            return await app(scope, receive, send)

        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(uvicorn.Config(combined, host="127.0.0.1", port=self.port,
                                                    log_level="warning", lifespan="on"))
        self.thread = threading.Thread(target=self.server.run, name="wk-backend", daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                self.close()
                raise RuntimeError("the live backend did not start")
            time.sleep(0.05)

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        return http_json(self.base, method, path, body)

    def close(self) -> None:
        from video_ai_editor import main as _main, storage as _storage
        self.server.should_exit = True
        self.thread.join(15)
        _storage.WORKDIR, _main.WORKDIR = self._saved
        _main._STORES.clear()


__all__ = ["LiveBackend", "GO_PAGE", "module_page", "free_port", "http_json"]
