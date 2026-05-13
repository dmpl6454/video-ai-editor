"""Desktop launcher — boots uvicorn in a thread and opens a PyWebView window.

Usage:
    uv run python -m video_ai_editor.desktop
"""
from __future__ import annotations
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path


def _wait_for_server(url: str, timeout: float = 15.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.1)
    return False


def _ensure_frontend_built() -> None:
    """Make sure frontend/dist exists. If not, run `npm run build` for the user."""
    repo = Path(__file__).resolve().parents[2]
    dist = repo / "frontend" / "dist"
    if dist.exists() and (dist / "index.html").exists():
        return
    print("[desktop] frontend/dist missing — running `npm run build`…", flush=True)
    import subprocess
    proc = subprocess.run(
        ["npm", "run", "build"],
        cwd=str(repo / "frontend"),
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print("[desktop] npm build failed:\n", proc.stderr[-1500:], file=sys.stderr)
        sys.exit(1)


def _serve(host: str, port: int) -> None:
    """Run uvicorn in this thread."""
    import uvicorn
    # Disable reload in desktop mode; users get fresh bits next launch.
    uvicorn.run("video_ai_editor.main:app", host=host, port=port,
                reload=False, log_level="warning", access_log=False)


def main() -> None:
    _ensure_frontend_built()
    host = os.environ.get("VAE_HOST", "127.0.0.1")
    port = int(os.environ.get("VAE_PORT", "8765"))
    url = f"http://{host}:{port}"

    server_thread = threading.Thread(target=_serve, args=(host, port), daemon=True)
    server_thread.start()
    if not _wait_for_server(f"{url}/api/health"):
        print(f"[desktop] backend didn't start on {url}", file=sys.stderr)
        sys.exit(1)

    import webview
    webview.create_window(
        title="Video AI Editor",
        url=url,
        width=1480, height=920,
        min_size=(1100, 700),
        easy_drag=False,
    )
    webview.start()


if __name__ == "__main__":
    main()
