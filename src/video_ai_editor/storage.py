"""Per-session paths and atomic writes."""
from __future__ import annotations
import json
import re
from pathlib import Path
from uuid import uuid4
from . import platformutil as _pu
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
    # Windows enforces mandatory file locking: a still-open handle elsewhere
    # in the process (a FileResponse mid-stream of a previews/exports mp4, an
    # in-flight render's *.part.mp4, or a lingering AV/indexer scan) makes a
    # bare rmtree raise PermissionError/OSError partway through, leaving the
    # session half-deleted and this route 500ing instead of returning 200/404.
    # rmtree_with_retry backs off and retries, same pattern as
    # replace_with_retry/unlink_with_retry.
    _pu.rmtree_with_retry(d)
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
        if not isinstance(meta, dict):
            # Final QA r2: a meta.json that parses to a list/null/number (a
            # crafted or damaged .vae) made `meta.get` raise here, and every
            # project-list, New project and Open call 500'd from then on.
            meta = {}
        mtime = d.stat().st_mtime
        sessions.append({
            "id": d.name,
            "name": meta.get("name", d.name),
            "source": meta.get("source"),
            "created_at": mtime,
            # QA-099: the project picker shows WHEN a project was last worked
            # on instead of its raw id, so two same-named copies stay apart.
            # `created_at` above has always been this mtime; kept for callers.
            "modified_at": mtime,
            # QA-099-THUMBS: the picker row's poster frame, or None when the
            # project is known to have nothing to show. A stat and a tiny
            # sidecar read — never the EDL (see poster_url).
            "poster": poster_url(d),
        })
    return sessions


# --- QA-099-THUMBS: a cached per-project poster frame -------------------------
#
# The project picker shows a frame of each project. Deriving it means reading
# the project's edl.json, and GET /api/sessions must not read every EDL on each
# call. So the answer is cached in `poster.json` beside the EDL, stamped with
# edl.json's (mtime_ns, size): the listing only STATS edl.json and compares
# stamps; the EDL is read again only by the poster route, and only for a project
# whose edl.json changed since the last derivation. The stamp covers EVERY
# writer of edl.json (commit, undo, redo, .vae load, a restore) with no hook in
# any of them — a "refresh on commit" hook would miss the ones that bypass
# commit(). The image URL carries the stamp, so the browser caches a poster
# until its project actually changes.

POSTER_FILE = "poster.json"
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic"}


def _edl_stamp(d: Path) -> str | None:
    try:
        st = (d / "edl.json").stat()
    except OSError:
        return None
    return f"{st.st_mtime_ns:x}-{st.st_size:x}"


def _read_poster(d: Path) -> dict | None:
    try:
        data = json.loads((d / POSTER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def poster_url(d: Path) -> str | None:
    """The picker's image URL for session dir `d` (versioned by the EDL
    stamp), or None when the cache says this project has no frame to show."""
    stamp = _edl_stamp(d)
    if stamp is None:
        return None
    cached = _read_poster(d)
    if cached and cached.get("stamp") == stamp and not cached.get("src"):
        return None
    return f"/api/sessions/{d.name}/poster?v={stamp}"


def _first_frame(edl: dict) -> tuple[str | None, float]:
    """(source, time) of the frame that stands for the project: the earliest
    media clip on the first video track that has one."""
    for track in edl.get("tracks") or []:
        if not isinstance(track, dict) or track.get("type") != "video":
            continue
        clips = [c for c in track.get("clips") or []
                 if isinstance(c, dict) and isinstance(c.get("src"), str) and "in" in c]
        if not clips:
            continue
        first = min(clips, key=lambda c: float(c.get("start") or 0.0))
        src = first["src"]
        if Path(src).suffix.lower() in _IMAGE_EXTS:
            return src, 0.0
        t_in = float(first.get("in") or 0.0)
        span = max(0.0, float(first.get("out") or 0.0) - t_in)
        # A quarter-second in (capped by the clip): frame 0 is often a fade from black.
        return src, t_in + min(1.0, span * 0.25)
    return None, 0.0


def poster_source(session_id: str) -> tuple[Path, float] | None:
    """The (source file, time) of a project's poster frame, re-derived from
    the EDL only when edl.json changed since the cached answer."""
    d = session_path(session_id)
    stamp = _edl_stamp(d)
    if stamp is None:
        return None
    cached = _read_poster(d)
    if not cached or cached.get("stamp") != stamp:
        try:
            edl = json.loads((d / "edl.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        src, t = _first_frame(edl if isinstance(edl, dict) else {})
        cached = {"stamp": stamp, "src": src, "t": round(t, 3)}
        tmp = d / f".{POSTER_FILE}.{uuid4().hex[:8]}.tmp"   # two picker requests may race
        try:
            tmp.write_text(json.dumps(cached), encoding="utf-8")
            _pu.replace_with_retry(tmp, d / POSTER_FILE)
        except OSError:
            pass    # a read-only dir still gets its poster, just uncached
    src = cached.get("src")
    if not src:
        return None
    p = Path(src)
    return (p, float(cached.get("t") or 0.0)) if p.is_file() else None


#: QA-099: a project with no name of its own is "Untitled project N", never
#: its `s_xxxxxxxxxx` id (the chip, picker and delete dialog all showed it).
UNTITLED_PREFIX = "Untitled project"
_UNTITLED_RE = re.compile(r"^Untitled project (\d+)$")
#: Long enough for any real title, short enough for the top-bar chip.
PROJECT_NAME_MAX = 120


def default_project_name() -> str:
    """The next free "Untitled project N" among the sessions on disk."""
    taken = [int(m.group(1)) for s in list_sessions()
             if (m := _UNTITLED_RE.match(str(s.get("name") or "")))]
    return f"{UNTITLED_PREFIX} {max(taken, default=0) + 1}"


def clean_project_name(raw: object) -> str:
    """A user-typed project name, whitespace-collapsed. ValueError when it is
    empty or longer than PROJECT_NAME_MAX (the route answers 400)."""
    import unicodedata
    text = str(raw if raw is not None else "")
    # Control/format/unassigned characters (NUL, C1, bidi overrides…) never
    # belong in a name the picker, the top-bar chip and meta.json show.
    # ZWJ/ZWNJ stay: Indic names need them. Whitespace controls (\n, \t)
    # collapse to a space below rather than vanishing.
    text = "".join(ch for ch in text
                   if ch.isspace() or ch in "\u200c\u200d"
                   or not unicodedata.category(ch).startswith("C"))
    name = " ".join(text.split())
    if not name:
        raise ValueError("A project name can't be empty.")
    if len(name) > PROJECT_NAME_MAX:
        raise ValueError(f"A project name can be at most {PROJECT_NAME_MAX} characters.")
    return name


def rename_session(session_id: str, name: str) -> str:
    """Set a session's display name, keeping every other meta key."""
    clean = clean_project_name(name)
    write_meta(session_id, {**read_meta(session_id), "name": clean})
    return clean


def name_reopened_copy(session_id: str, when: str) -> str:
    """A just-opened .vae carries the saved project's name. When another
    project already has that name the copy becomes "<name> (opened <when>)",
    so the picker never lists two rows that only their ids tell apart."""
    meta = read_meta(session_id)
    name = str(meta.get("name") or "").strip() or default_project_name()
    others = {str(s.get("name")) for s in list_sessions() if s["id"] != session_id}
    if name in others:
        base = f"{name} (opened {when})"
        name, n = base, 2
        while name in others:
            name, n = f"{base} {n}", n + 1
        name = name[:PROJECT_NAME_MAX]
    write_meta(session_id, {**meta, "name": name})
    return name


def write_meta(session_id: str, meta: dict) -> None:
    p = session_dir(session_id) / "meta.json"
    p.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def read_meta(session_id: str) -> dict:
    """The session's meta.json as a dict — {} when it is absent or is not a
    JSON object (Final QA r2: rename and save used to 500 on `[1,2,3]`).
    Unreadable JSON still raises, as it always has."""
    p = session_dir(session_id) / "meta.json"
    meta = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return meta if isinstance(meta, dict) else {}
