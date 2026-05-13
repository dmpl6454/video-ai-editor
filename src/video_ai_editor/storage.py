"""Per-session paths and atomic writes."""
from __future__ import annotations
import json
import shutil
from pathlib import Path
from uuid import uuid4
from .config import WORKDIR


def new_session_id() -> str:
    return f"s_{uuid4().hex[:10]}"


def session_dir(session_id: str) -> Path:
    """Compute + create the session directory tree. Has the side-effect of
    creating directories — use `session_path(sid)` if you only want to know
    where a session WOULD live without materialising it."""
    d = WORKDIR / session_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "uploads").mkdir(exist_ok=True)
    (d / "previews").mkdir(exist_ok=True)
    (d / "exports").mkdir(exist_ok=True)
    (d / "cache").mkdir(exist_ok=True)
    (d / "snapshots").mkdir(exist_ok=True)
    return d


def session_path(session_id: str) -> Path:
    """Pure path computation, no side effects. Use this for existence checks
    (otherwise `session_dir` creates the dir + makes every check trivially true)."""
    return WORKDIR / session_id


def session_exists(session_id: str) -> bool:
    return session_path(session_id).exists()


def list_sessions() -> list[dict]:
    sessions = []
    for d in sorted(WORKDIR.glob("s_*"), key=lambda p: p.stat().st_mtime, reverse=True):
        meta_path = d / "meta.json"
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
            except Exception:
                pass
        sessions.append({
            "id": d.name,
            "name": meta.get("name", d.name),
            "source": meta.get("source"),
            "created_at": d.stat().st_mtime,
        })
    return sessions


def write_meta(session_id: str, meta: dict) -> None:
    p = session_dir(session_id) / "meta.json"
    p.write_text(json.dumps(meta, indent=2))


def read_meta(session_id: str) -> dict:
    p = session_dir(session_id) / "meta.json"
    return json.loads(p.read_text()) if p.exists() else {}
