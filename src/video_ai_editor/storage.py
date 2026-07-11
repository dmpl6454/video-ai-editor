"""Per-session paths and atomic writes."""
from __future__ import annotations
import json
import re
import shutil
from pathlib import Path
from uuid import uuid4
from .config import WORKDIR

# Every session id this code ever generates matches this shape (see
# new_session_id below). Reject anything else before it touches the
# filesystem — a sid coming straight from a URL path param (e.g.
# DELETE /api/sessions/{sid}) is untrusted input, and WORKDIR / session_id
# with an unvalidated session_id like "../../etc" is a path-traversal /
# arbitrary-directory-deletion primitive.
_SID_RE = re.compile(r"^s_[a-zA-Z0-9]{6,64}$")


def new_session_id() -> str:
    return f"s_{uuid4().hex[:10]}"


def is_valid_session_id(session_id: str) -> bool:
    return bool(_SID_RE.fullmatch(session_id))


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


def delete_session(session_id: str) -> bool:
    """Remove a session directory and all its media/state. Returns True if it
    existed. Idempotent — deleting a missing session is a no-op returning False.

    Refuses anything that isn't a well-formed session id AND doesn't resolve
    to a direct child of WORKDIR — belt-and-suspenders against a path-traversal
    sid (e.g. "../../Documents") reaching shutil.rmtree, even if a caller
    forgets the is_valid_session_id() check at the route layer."""
    if not is_valid_session_id(session_id):
        return False
    d = session_path(session_id).resolve()
    if d.parent != WORKDIR.resolve():
        return False
    if not d.exists():
        return False
    shutil.rmtree(d, ignore_errors=False)
    return True


def list_sessions() -> list[dict]:
    sessions = []
    for d in sorted(WORKDIR.glob("s_*"), key=lambda p: p.stat().st_mtime, reverse=True):
        meta_path = d / "meta.json"
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
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
    p.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def read_meta(session_id: str) -> dict:
    p = session_dir(session_id) / "meta.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
