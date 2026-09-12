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
from .storage import session_dir, session_path, new_session_id

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


#: The three local-header signatures a zip file can legitimately open with:
#: a normal archive, an EMPTY archive (end-of-central-directory record only),
#: and a spanned/split archive marker. The latter two are accepted only as far
#: as `zipfile` itself can open the file — a bare empty-archive record has no
#: members, so it fails the manifest check on its own merits.
_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

#: The member spellings `zipfile.extractall` places at `<unpack>/manifest.json`.
#: `./manifest.json` is here because some zip writers emit it and extractall
#: drops the `.` component, so it lands at the root exactly like the bare name.
_ROOT_MANIFEST_NAMES = ("manifest.json", "./manifest.json")


def _names_root_manifest(info: zipfile.ZipInfo) -> bool:
    """True iff `info` is a FILE member landing at the archive root as
    `manifest.json`.

    JUDGED LITERALLY, NOT BY RESOLVING AGAINST A FAKE BASE
    -----------------------------------------------------
    The first version of this ran the raw name through `_inside` against a
    never-touched `/vae-probe` base, so that "is this the manifest?" was
    word-for-word the loader's own containment rule. It read well and was
    wrong: `Path.resolve()` NORMALISES `..`, while `extractall` DROPS it, so
    three member shapes passed the probe and then failed the loader — each
    turning the required 415 ("not a project") into a 422 plus an orphan
    session in WORKDIR that GET /api/sessions listed as an empty project:

      * `manifest.json/` — a DIRECTORY entry. It resolves to the same path a
        file member would, so the probe said yes; extractall made a directory
        and `read_text` raised `IsADirectoryError`, whose message carried the
        absolute WORKDIR path straight into a user-facing error message.
      * `../vae-probe/manifest.json` and `vae-probe/../manifest.json` — both
        leave the base and come back, so `.resolve()` landed on
        `<base>/manifest.json` and the probe said yes; extractall discards the
        `..` component instead of normalising it, so the member really lands at
        `<unpack>/vae-probe/manifest.json` and the loader finds no manifest.

    Nested (`backup/manifest.json`), escaping (`../manifest.json`) and
    absolute (`/manifest.json`) names are all still refused — the brief's rule
    is that a traversal name is never treated as a project. Being stricter than
    extractall's own sanitiser is the safe direction: the only archives it can
    turn away are ones no zip writer we care about produces, and they get an
    honest 415 rather than a session nobody asked for.
    """
    return info.filename in _ROOT_MANIFEST_NAMES and not info.is_dir()


def is_project_archive(path: Path) -> bool:
    """Decide from CONTENT whether `path` is a Video AI Editor project file.

    WHY CONTENT AND NOT THE NAME
    ----------------------------
    `POST /api/load_project` used to gate on the upload's filename ending in
    `.vae`/`.zip`. The shipped desktop app then hit a 415 on its own saved
    projects: the "Saved" link was served as `text/plain` with an unknown
    extension, which is precisely when WebKit/macOS appends `.txt` on download —
    so the user picked `<sid>.vae.txt`, a byte-for-byte valid project, and was
    told it was unsupported. `P.VAE` and an extensionless pick failed the same
    way. The bytes were never wrong; only the name was, and the name is the one
    thing the OS, the browser and the user all feel free to rewrite.

    So: zip magic first (cheap, and turns "an .mp4 named p.vae" into an honest
    answer without opening it as an archive), then the member list must carry a
    root `manifest.json` file — see `_names_root_manifest` for why that is
    judged on the literal member name and never by resolving it.
    Anything else is not a project, whatever it is called.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(4)
    except OSError:
        return False
    if head not in _ZIP_MAGICS:
        return False
    try:
        with zipfile.ZipFile(path, "r") as zf:
            # infolist(), not namelist(): the directory/file distinction only
            # exists on the ZipInfo, and `manifest.json/` is not a manifest.
            infos = zf.infolist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError):
        return False
    return any(_names_root_manifest(i) for i in infos)


def _read_manifest(unpack: Path) -> dict:
    """The archive's root `manifest.json`, or ValueError saying why not.

    `is_file()`, not `exists()`: a `manifest.json/` DIRECTORY member used to
    satisfy `exists()` and then blow up in `read_text` with
    `IsADirectoryError: /…/workdir/s_xxx/_unpack/manifest.json` — an absolute
    WORKDIR path in a message the endpoint hands to the user. The probe refuses
    that member now too (`_names_root_manifest`), so this is the second lock on
    the same door: `load_project` is public and called directly by tests and by
    any future caller that has not run the probe.
    """
    path = _inside(unpack, "manifest.json")
    if path is None or not path.is_file():
        raise ValueError("that .vae has no manifest.json — it is not a project file")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"that .vae's manifest.json is unreadable ({e})") from e
    if not isinstance(manifest, dict):
        # `manifest.get(...)` on a list/str would raise AttributeError, which
        # reads like a crash rather than "this file is not a project".
        raise ValueError("that .vae's manifest.json is not a project manifest")
    return manifest


def _assert_timeline_importable(unpack: Path) -> None:
    """Refuse an archive whose `edl.json` will not load, BEFORE a session exists.

    Without this the import returned 200 and the user got a blank default
    timeline with no error at all: `EDLStore` deliberately falls back to an
    empty EDL (then to a snapshot, of which an imported session has none), so
    an unreadable `edl.json` presents as "my project opened empty". That is the
    precise failure `save_project`'s `is_data_loss_state` guard exists to stop
    on the way OUT, and it had no counterpart on the way IN — so a damaged .vae
    opened blank, and saving that blank session would then overwrite the user's
    last good copy.

    A missing `edl.json` is refused for the same reason, not a different one:
    `save_project` ALWAYS writes one (from the live store, precisely so a
    recovered session bundles readable bytes), so an archive without it cannot
    have come from Save and has no timeline to open.

    Validated with `EDL.model_validate_json` rather than by constructing an
    `EDLStore`: the store has side effects (it writes `edl.corrupt.json` and
    scans for snapshots) and would need a session directory to live in — which
    is the thing this check exists to avoid creating.
    """
    path = _inside(unpack, "edl.json")
    if path is None or not path.is_file():
        raise ValueError("that .vae has no edl.json — there is no timeline in it to open")
    try:
        EDL.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError(
            f"that .vae's edl.json is unreadable ({type(e).__name__}) — the "
            f"project file is damaged, so opening it would show an empty "
            f"timeline rather than your project") from e


def _import_media(manifest: dict, unpack: Path, imported: Path) -> dict[str, str]:
    """Move bundled media into the session, returning the old->new src map."""
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
    return src_remap


def _write_state_files(sd: Path, unpack: Path, src_remap: dict[str, str]) -> None:
    """Copy the state files in, rewriting bundled media paths to the new ones."""
    for name in ("edl.json", "ops.json", "meta.json", "chat.json"):
        # Fixed names, so this cannot escape — routed through `_inside` anyway
        # so there is exactly one rule in this module for "is this path mine?"
        sp = _inside(unpack, name)
        if sp is None or not sp.is_file():
            continue
        text = sp.read_text(encoding="utf-8")
        for old, new in src_remap.items():
            text = text.replace(json.dumps(old)[1:-1], json.dumps(new)[1:-1])
        (sd / name).write_text(text, encoding="utf-8")


def load_project(src: Path) -> str:
    """Load a .vae into a fresh session. Returns the new session_id.

    THE ORDER IS THE POINT: extract to a private directory, validate there,
    and only then materialise the session.

    This used to call `new_session_id()` + `session_dir()` first and unpack
    INTO the session, so every archive that got past the zip open and then
    failed validation left a directory behind in WORKDIR. `GET /api/sessions`
    globs `s_*`, so each one showed up in the project picker as an empty
    project (its `/edl` even answered 200 with a default timeline) — open four
    broken files and the picker gains four ghosts the user cannot tell from
    real work. Nothing about a refusal should leave a trace.

    The unpack directory is a sibling of the session dirs, not a system temp
    dir, so the media moves stay on one filesystem — `shutil.move` across
    devices degrades to a full copy, and these are video files. It is named
    `_import_unpack_<sid>` so it lands under the same `_import_*` glob the
    endpoint's own temp file uses, and it is removed on EVERY exit.
    """
    if not src.exists():
        raise FileNotFoundError(src)
    sid = new_session_id()
    # `session_path` is pure (no mkdir) — asking it where the session WOULD go
    # is how we find the sessions' parent without creating anything yet.
    unpack = session_path(sid).parent / f"_import_unpack_{sid}"
    try:
        unpack.mkdir(parents=True)
        with zipfile.ZipFile(src, "r") as zf:
            zf.extractall(unpack)
        manifest = _read_manifest(unpack)
        _assert_timeline_importable(unpack)
        return _materialise_session(sid, manifest, unpack)
    finally:
        shutil.rmtree(unpack, ignore_errors=True)


def _materialise_session(sid: str, manifest: dict, unpack: Path) -> str:
    """Create the session and fill it. Removes it again if filling fails, so a
    half-imported session never reaches the project picker."""
    sd = session_dir(sid)
    try:
        imported = sd / "uploads" / "imported"
        imported.mkdir(parents=True, exist_ok=True)
        _write_state_files(sd, unpack, _import_media(manifest, unpack, imported))
    except Exception:
        # `sid` was generated a moment ago, so `sd` holds nothing but what this
        # call just put there — removing it cannot take anything else with it.
        shutil.rmtree(sd, ignore_errors=True)
        raise
    return sid
