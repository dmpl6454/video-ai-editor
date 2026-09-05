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
import logging
import shutil
import zipfile
from pathlib import Path
from .config import WORKDIR
from .edl import EDL, EDLStore
from .edl.schema import Clip, Sticker
from .storage import session_dir, new_session_id

_log = logging.getLogger("video_ai_editor")


def _media_srcs(edl: EDL) -> set[str]:
    """Every on-disk file the EDL references, for bundling + path remapping.

    `Sticker` counts. It used to be `isinstance(c, Clip)` only, so save_project
    bundled no sticker PNG and recorded none in the manifest — which meant
    load_project's src_remap never rewrote them either, and the restored EDL
    still pointed at the ORIGINAL session's `uploads/stickers/…`. On the
    authoring machine those paths usually still resolve, which is exactly why
    this went unnoticed; move the .vae to another machine (or delete the source
    session) and every sticker is gone. The renderer then skips a sticker whose
    src is missing SILENTLY, so the failure mode was "my stickers just aren't
    there any more" with no error.

    Emoji stickers matter here too: their src lives in a per-machine
    `user_cache_dir/emoji/<codepoints>.png`, so an emoji-heavy project was not
    portable at all. They are ~2KB each — bundling is strictly better than
    re-deriving them on load, which would need network access.
    """
    out: set[str] = set()
    for t in edl.tracks:
        for c in t.clips:
            if isinstance(c, (Clip, Sticker)):
                out.add(c.src)
    if edl.brand_kit and edl.brand_kit.end_card:
        out.add(edl.brand_kit.end_card)
    return out


def save_project(session_id: str, dst: Path) -> Path:
    sd = session_dir(session_id)
    store = EDLStore(sd)
    edl = store.edl

    # An unreadable edl.json with no usable snapshot presents to every caller
    # as an empty timeline, indistinguishable from a brand-new session. Writing
    # a .vae in that state produced a valid archive with `manifest.media == []`
    # and no error (QA round 5, VAI-02) — a second data-loss path stacked on
    # the first, since the user's natural response to "my project looks empty"
    # is to save a backup, which then overwrites their only good copy.
    if store.is_data_loss_state:
        raise ValueError(
            f"session {session_id} could not be loaded (its edl.json is "
            f"unreadable and no snapshot recovered), so there is nothing to "
            f"save — writing a project file now would produce an empty one. "
            f"The unreadable original is kept at edl.corrupt.json.")
    if store.load_state == "recovered":
        _log.warning("saving %s from a snapshot-recovered timeline — the most "
                     "recent edit(s) may be missing from %s", session_id, dst.name)

    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.suffix != ".vae":
        dst = dst.with_suffix(".vae")

    media_paths = sorted(_media_srcs(edl))
    manifest = {"media": [], "session_id": session_id,
                "load_state": store.load_state}
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
        # edl.json comes from the LIVE store, not the file on disk: after a
        # snapshot recovery the on-disk copy is still the unreadable one, and
        # bundling that would make the archive unloadable too.
        zf.writestr("edl.json", edl.to_json())
        for name in ("ops.json", "meta.json", "chat.json"):
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


def _inside(base: Path, relative: object) -> Path | None:
    """Resolve `relative` against `base`, or None if it escapes.

    WHY THIS EXISTS, AND WHY IT IS NOT PARANOIA ABOUT `..`
    -----------------------------------------------------
    `manifest.json` comes out of an attacker-supplied zip, and every string in
    it was, until 0.6.0, joined onto the unpack directory and handed straight
    to `shutil.move`. `Path.__truediv__` DISCARDS the left-hand side entirely
    when the right-hand side is absolute:

        Path("/a/b") / "/Users/me/tax-return.pdf"  ->  Path("/Users/me/tax-return.pdf")

    so a hostile `.vae` did not even need a `..` to turn `POST /api/load_project`
    into "move any file on this Mac into a session directory, then download it
    through /api/sessions/{sid}/files/uploads/imported/…". That is a MOVE, not a
    copy, so it is destructive as well as an exfiltration primitive, and it sat
    entirely outside the dispatch path guards — `assert_path_allowed` is never
    consulted here, so arming LAN mode did nothing to close it.

    `zipfile.extractall` already refuses to write outside its destination, so a
    member that really came from the archive is by construction inside `unpack`.
    Anything that resolves outside is therefore not a stale project written by
    an older version of this app — it is hostile input, and the honest response
    is to skip it rather than to try to repair it.
    """
    if not isinstance(relative, str) or not relative:
        return None
    try:
        candidate = (base / relative).resolve()
        root = base.resolve()
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_relative_to(root) else None


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
    manifest_path = _inside(unpack, "manifest.json")
    if manifest_path is None or not manifest_path.exists():
        # `_unpack` is inside the session dir we just created, so this can only
        # fail if the archive carried no manifest at all.
        raise ValueError("that .vae has no manifest.json — it is not a project file")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # Move media into imported/, build src remap
    src_remap: dict[str, str] = {}
    for entry in manifest.get("media", []):
        if not isinstance(entry, dict):
            continue
        bundled = _inside(unpack, entry.get("bundled"))
        if bundled is None:
            _log.warning("load_project: refusing manifest entry that escapes the "
                         "archive: %r", entry.get("bundled"))
            continue
        if not bundled.exists():
            continue
        orig = entry.get("orig")
        if not isinstance(orig, str) or not orig:
            continue
        # `.name` on the RESOLVED path, not on the raw manifest string: the raw
        # string is what we just refused to trust.
        target = imported / bundled.name
        shutil.move(str(bundled), str(target))
        src_remap[orig] = str(target)

    # Move state files into the session dir, rewriting src paths in edl.json
    for name in ("edl.json", "ops.json", "meta.json", "chat.json"):
        # Fixed names, so this cannot escape — routed through `_inside` anyway
        # so there is exactly one rule in this function for "is this path mine?"
        sp = _inside(unpack, name)
        if sp is None or not sp.exists():
            continue
        text = sp.read_text(encoding="utf-8")
        for old, new in src_remap.items():
            text = text.replace(json.dumps(old)[1:-1], json.dumps(new)[1:-1])
        (sd / name).write_text(text, encoding="utf-8")

    shutil.rmtree(unpack, ignore_errors=True)
    return sid
