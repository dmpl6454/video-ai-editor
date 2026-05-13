"""Project save/load.

A `.vae` file is a zip containing:
  - edl.json + ops.json + chat.json + meta.json (the session state)
  - manifest.json — list of media srcs and their relative bundled paths
  - media/  — original uploaded files referenced by V1/music/vo clips

Loading restores the EDL into a NEW session and rewrites src paths to point at
the new session's `uploads/imported/`. Caches (previews, transcripts, vision)
are not bundled — they regenerate on demand.
"""
from __future__ import annotations
import json
import shutil
import zipfile
from pathlib import Path
from .config import WORKDIR
from .edl import EDL, EDLStore
from .edl.schema import Clip
from .storage import session_dir, new_session_id


def _media_srcs(edl: EDL) -> set[str]:
    out: set[str] = set()
    for t in edl.tracks:
        for c in t.clips:
            if isinstance(c, Clip):
                out.add(c.src)
    if edl.brand_kit and edl.brand_kit.end_card:
        out.add(edl.brand_kit.end_card)
    return out


def save_project(session_id: str, dst: Path) -> Path:
    sd = session_dir(session_id)
    store = EDLStore(sd)
    edl = store.edl

    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.suffix != ".vae":
        dst = dst.with_suffix(".vae")

    media_paths = sorted(_media_srcs(edl))
    manifest = {"media": [], "session_id": session_id}
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
        # State files
        for name in ("edl.json", "ops.json", "meta.json", "chat.json"):
            p = sd / name
            if p.exists():
                zf.write(p, arcname=name)

        # Media: bundle by basename to keep arcnames simple. If duplicate
        # basenames, suffix with index.
        seen_names: dict[str, int] = {}
        for src in media_paths:
            sp = Path(src)
            if not sp.exists():
                continue
            base = sp.name
            n = seen_names.get(base, 0)
            arc_name = base if n == 0 else f"{sp.stem}__{n}{sp.suffix}"
            seen_names[base] = n + 1
            zf.write(sp, arcname=f"media/{arc_name}")
            manifest["media"].append({"orig": str(sp), "bundled": f"media/{arc_name}"})

        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
    return dst


def load_project(src: Path) -> str:
    """Load a .vae into a fresh session. Returns the new session_id."""
    if not src.exists():
        raise FileNotFoundError(src)
    sid = new_session_id()
    sd = session_dir(sid)
    imported = sd / "uploads" / "imported"
    imported.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(src, "r") as zf:
        zf.extractall(sd / "_unpack")

    unpack = sd / "_unpack"
    manifest = json.loads((unpack / "manifest.json").read_text())

    # Move media into imported/, build src remap
    src_remap: dict[str, str] = {}
    for entry in manifest.get("media", []):
        bundled = unpack / entry["bundled"]
        if not bundled.exists():
            continue
        target = imported / Path(entry["bundled"]).name
        shutil.move(str(bundled), str(target))
        src_remap[entry["orig"]] = str(target)

    # Move state files into the session dir, rewriting src paths in edl.json
    for name in ("edl.json", "ops.json", "meta.json", "chat.json"):
        sp = unpack / name
        if not sp.exists():
            continue
        text = sp.read_text()
        for old, new in src_remap.items():
            text = text.replace(json.dumps(old)[1:-1], json.dumps(new)[1:-1])
        (sd / name).write_text(text)

    shutil.rmtree(unpack, ignore_errors=True)
    return sid
