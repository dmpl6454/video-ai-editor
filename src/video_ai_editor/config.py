import os
from pathlib import Path

from . import platformutil as _pu


def _augment_path_for_gui_launch() -> None:
    """Make CLIs (ffmpeg, ffprobe, whisper-cli, …) resolvable no matter how the
    app was started.

    macOS: a double-clicked .app inherits launchd's minimal PATH and can't see
    /opt/homebrew/bin. Windows: GUI processes inherit the user PATH, but a
    winget-installed ffmpeg (Gyan.FFmpeg) is famously NOT put on PATH — so we
    also probe its package dir. Append (don't prepend) so we never override a
    deliberately-chosen binary."""
    if _pu.IS_WINDOWS:
        localappdata = os.environ.get("LOCALAPPDATA", "")
        extra = []
        if localappdata:
            # Gyan.FFmpeg / BtbN unzip locations; glob the winget packages dir.
            wg = Path(localappdata) / "Microsoft" / "WinGet" / "Packages"
            if wg.is_dir():
                extra += [str(p) for p in wg.glob("Gyan.FFmpeg*/**/bin") if p.is_dir()]
            extra.append(str(Path(localappdata) / "Programs" / "ffmpeg" / "bin"))
    else:
        extra = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin",
                 "/usr/local/sbin", str(Path.home() / ".local" / "bin")]
    current = os.environ.get("PATH", "").split(os.pathsep)
    additions = [d for d in extra if d and d not in current and os.path.isdir(d)]
    if additions:
        os.environ["PATH"] = os.pathsep.join([*current, *additions])


def _avoid_hf_symlink_failures() -> None:
    """Stop a model download from dying on Windows for lack of a privilege.

    huggingface_hub populates its cache with symlinks into a blob store. Creating
    one on Windows needs Developer Mode or admin, and without either
    `snapshot_download` raises

        OSError: [WinError 1314] A required privilege is not held by the client

    partway through — measured on a real first-run download of a whisper model.
    The hub *warns* that this machine does not support symlinks and then attempts
    them anyway, so its own detection cannot be relied on. `HF_HUB_DISABLE_SYMLINKS`
    makes it place real files instead.

    Only ever a default: a user or CI that set the variable keeps their value.
    Windows-only, since the failure is. The cost is disk when two cache entries
    share a blob, which for model weights is rare — and a duplicated blob is
    plainly better than a caption feature that cannot fetch its model.
    """
    if _pu.IS_WINDOWS:
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")


# Run at import time — config is imported before any subprocess fires, and by
# every entrypoint (desktop .app, `uvicorn …:app`, tests), so this is the one
# universal chokepoint.
_augment_path_for_gui_launch()
_avoid_hf_symlink_failures()

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _read_version() -> str:
    """App version — single source of truth is the VERSION file at the repo
    root (also bundled under sys._MEIPASS in the .app)."""
    import sys as _sys
    for base in (getattr(_sys, "_MEIPASS", None), PROJECT_ROOT):
        if base:
            vf = Path(base) / "VERSION"
            if vf.exists():
                try:
                    return _pu.read_text_config(vf).strip() or "0.0.0"
                except Exception:
                    pass
    return "0.0.0"


APP_VERSION = _read_version()

_BUILD_ID: str | None = None


def build_id() -> str:
    """Short, unambiguous identifier for the exact bits that are running.

    `APP_VERSION` alone is NOT a build identity: VERSION sat at 0.3.7 for 99
    commits and three fix rounds, so a bug report saying "v0.3.7" could not be
    dated, and testers repeatedly re-reported bugs that had already been fixed.
    This returns something that changes with the code:

      frozen app -> the BUILD_ID file baked in at package time
      dev tree   -> `git rev-parse --short HEAD` plus `-dirty` for local edits
      neither    -> "" (callers must treat it as optional)

    Deliberately LAZY and cached, not computed at import: `config` is the
    import-time chokepoint every entry point hits, and shelling out to git there
    would add latency to every launch, test and CLI invocation.
    """
    global _BUILD_ID
    if _BUILD_ID is not None:
        return _BUILD_ID
    _BUILD_ID = ""
    import sys as _sys
    for base in (getattr(_sys, "_MEIPASS", None), PROJECT_ROOT):
        if not base:
            continue
        bf = Path(base) / "BUILD_ID"
        if bf.exists():
            try:
                # BOM-tolerant: build_win.ps1 under PowerShell 5.1 writes this
                # file with a UTF-8 BOM, which `.strip()` does not remove — the
                # reported sha then differs from every real git object.
                _BUILD_ID = _pu.read_text_config(bf).strip()
                if _BUILD_ID:
                    return _BUILD_ID
            except OSError:
                pass
    if getattr(_sys, "frozen", False):
        # No git in a packaged app, and no BUILD_ID baked in — nothing to add.
        return _BUILD_ID
    import subprocess
    try:
        rev = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, **_pu.SUBPROCESS_FLAGS,
        )
        if rev.returncode != 0:
            return _BUILD_ID
        sha = rev.stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "status", "--porcelain"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, **_pu.SUBPROCESS_FLAGS,
        )
        if dirty.returncode == 0 and dirty.stdout.strip():
            sha += "-dirty"
        _BUILD_ID = sha
    except (OSError, subprocess.SubprocessError):
        pass
    return _BUILD_ID

# Auto-load .env at the project root. Last assignment wins (POSIX-style) so
# stale duplicates higher up in the file are overridden by newer entries
# appended at the bottom.
def _apply_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    parsed: dict[str, str] = {}
    try:
        # BOM-tolerant: a `.env` written by Notepad or PowerShell 5.1 starts
        # with EF BB BF, which would make the FIRST key parse as
        # `﻿ANTHROPIC_API_KEY` — so the key never loads and the app reports
        # it missing while the file plainly contains it.
        lines = _pu.read_text_config(env_path).splitlines()
    except Exception:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        # Strip unquoted inline comments (`KEY=auto  # note` → `auto`) —
        # .env.example itself shipped one on WHISPER_DEVICE and the comment
        # became part of the value, crashing whisper with "unsupported
        # device". Quoted values keep their # (passwords etc.).
        if v and not v.startswith(('"', "'")):
            v = v.split(" #", 1)[0].rstrip()
        v = v.strip('"').strip("'")
        if k and v:
            parsed[k] = v  # later entries overwrite earlier ones
    for k, v in parsed.items():
        # The shell env always wins over .env, and an earlier-loaded file wins
        # over a later one (so the dev repo .env beats the user-level one).
        if k not in os.environ:
            os.environ[k] = v


def _user_config_dir() -> Path:
    """Stable, user-writable config dir that both dev and the shipped app can
    reach. Windows: %APPDATA%\\Video AI Editor; macOS: ~/Library/Application
    Support/Video AI Editor."""
    return _pu.user_data_dir("Video AI Editor")


def _load_dotenv() -> None:
    # Order = precedence (first wins): dev repo .env, then the user-level config
    # dir used by the shipped app.
    for env_path in (PROJECT_ROOT / ".env", _user_config_dir() / ".env"):
        _apply_env_file(env_path)


_load_dotenv()


def _default_workdir() -> Path:
    """Where per-session uploads/previews/exports/caches live.

    Dev: `<repo>/workdir`. But a shipped .app runs from a READ-ONLY location
    (the DMG, then /Applications), so writing next to the bundle fails — the
    first session-create would blow up. When frozen, store under the user's
    Application Support dir instead.
    """
    env = os.environ.get("WORKDIR")
    if env:
        # Relative env → under repo (dev convenience); absolute → as-is.
        p = Path(env)
        return p if p.is_absolute() else PROJECT_ROOT / p
    import sys as _sys
    if getattr(_sys, "frozen", False) or getattr(_sys, "_MEIPASS", None):
        return _pu.user_data_dir("Video AI Editor") / "workdir"
    return PROJECT_ROOT / "workdir"


def _asset_root() -> Path:
    """Root of the read-only bundled assets (presets, fonts).

    Frozen: they're unpacked under sys._MEIPASS (via --add-data). Dev: repo."""
    import sys as _sys
    meipass = getattr(_sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return PROJECT_ROOT


WORKDIR = _default_workdir()
_ASSETS = _asset_root()
PRESETS_DIR = _ASSETS / "presets"
FONTS_DIR = _ASSETS / "fonts"

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
# Captions get the heavy, best-quality model by default — uploads stay fast on
# `small`, then auto_caption re-transcribes with large-v3 for broadcast-quality
# Hindi/English. large-v3 is the only model that handles Hindi cleanly without
# the repetition-loop hallucination weaker models fall into (measured); turbo
# mangles Hindi into English, so it is NOT the caption default.
WHISPER_CAPTION_MODEL = os.environ.get("WHISPER_CAPTION_MODEL", "large-v3")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "auto")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

DEFAULT_CANVAS = {"w": 1080, "h": 1920, "fps": 30}

# --- filesystem path restriction --------------------------------------------
#
# Two postures, one mechanism.
#
#   * Loopback desktop (the default, and every release through 0.5.0):
#     restriction is OFF and a tool arg may name any file the user can read.
#     That is the point of a local-first editor — Claude gets to
#     `apply_lut("/Users/me/luts/teal.cube")` without ceremony, and the only
#     principal on the socket is the person sitting at the machine.
#
#   * LAN mode (0.6.0's phone companion): the socket is reachable from other
#     machines on the network, so the very same tool args become a remote file
#     READ primitive (`add_clip.src`, `import_srt.path`, exfiltrated back out
#     through /transcript) and a remote file WRITE primitive
#     (`export_srt.path` → `~/.zshrc`, `~/Library/LaunchAgents/…`). Nothing in
#     0.5.0 stopped either. `enable_path_restriction(True)` forces restriction
#     on for the life of the process, independently of the env var, and
#     api/pairing.py calls it whenever LAN mode is armed.
#
# VAI_RESTRICT_PATHS keeps its old meaning: force restriction on for a hosted /
# multi-user deployment, LAN mode or not.
RESTRICT_PATHS = os.environ.get("VAI_RESTRICT_PATHS", "").strip().lower() in {"1", "true", "yes", "on"}
ALLOWED_PATH_ROOTS: list[Path] = []
if RESTRICT_PATHS:
    _extra = os.environ.get("VAI_ALLOWED_ROOTS", "")
    ALLOWED_PATH_ROOTS = [WORKDIR.resolve()]
    for r in _extra.split(os.pathsep) if _extra else []:
        r = r.strip()
        if r:
            try:
                ALLOWED_PATH_ROOTS.append(Path(r).expanduser().resolve())
            except Exception:
                pass

#: Set by enable_path_restriction(), NOT by the environment. A double-clicked
#: .app inherits launchd's environment, which has no VAI_RESTRICT_PATHS in it
#: and no way for the user to add one — so the LAN switch cannot be an env var
#: and this flag is how the running process learns about it.
_FORCED_RESTRICT = False


def enable_path_restriction(on: bool = True) -> None:
    """Turn the allowlist on (or back off) at runtime.

    Called from api/pairing.py when LAN mode is armed or disarmed. Deliberately
    a function and not a module constant: the LAN toggle is a live setting the
    user can flip from the desktop's Phone panel, and re-importing config from
    a request handler would not re-run the import-time computation anyway.
    """
    global _FORCED_RESTRICT
    _FORCED_RESTRICT = bool(on)


def restrict_paths_active() -> bool:
    """Is the allowlist being enforced right now?

    `RESTRICT_PATHS` (env, fixed at import) OR the runtime LAN flag. Every
    guard reads THIS, never the bare constant.
    """
    return RESTRICT_PATHS or _FORCED_RESTRICT


def _env_extra_roots() -> list[Path]:
    """VAI_ALLOWED_ROOTS, re-read on every call.

    Re-read rather than snapshotted because `ALLOWED_PATH_ROOTS` above is only
    populated when the env var was set AT IMPORT with restriction already on —
    which is never true for the LAN path, where restriction is armed later.
    """
    out: list[Path] = []
    raw = os.environ.get("VAI_ALLOWED_ROOTS", "")
    for r in raw.split(os.pathsep) if raw else []:
        r = r.strip()
        if not r:
            continue
        try:
            out.append(Path(r).expanduser().resolve())
        except OSError:
            continue
    return out


def allowed_write_roots() -> list[Path]:
    """Where a tool may CREATE or OVERWRITE a file under restriction.

    Deliberately narrower than the read list: a stray read of ~/Pictures is a
    privacy problem, but a stray write to a dotfile or a LaunchAgent is remote
    code execution on the Mac. Only the app's own workdir plus the two folders
    a person actually asks an editor to write into.

    WORKDIR is read from the module global on EVERY call, never captured at
    import: the pytest fixtures (and `desktop.py` under a custom WORKDIR env)
    monkeypatch `config.WORKDIR` after this module is imported, and a snapshot
    would silently reject every legitimate session path in the test suite.
    """
    roots = [WORKDIR.resolve()]
    home = Path.home()
    for name in ("Movies", "Videos", "Downloads"):
        candidate = home / name
        if candidate.is_dir():
            roots.append(candidate.resolve())
    roots.extend(_env_extra_roots())
    roots.extend(ALLOWED_PATH_ROOTS)
    return roots


def allowed_read_roots() -> list[Path]:
    """Where a tool may READ from under restriction.

    Everything writable, plus ~/Pictures (stills for `apply_brand_kit.end_card`
    and the sticker tools), plus the app's own read-only bundled assets
    (LUT/template presets and fonts — `apply_lut` and the brand kit resolve
    names into them).

    Deliberately NOT ~/Documents or ~/Desktop. A LAN peer that can name a path
    can read it back out through `/transcript`, and those two are where people
    keep the things they would mind losing. A user who genuinely stores footage
    there adds the folder with VAI_ALLOWED_ROOTS — the desktop's Phone panel
    says so in as many words.
    """
    roots = allowed_write_roots()
    pictures = Path.home() / "Pictures"
    if pictures.is_dir():
        roots.append(pictures.resolve())
    for extra in (PRESETS_DIR, FONTS_DIR):
        try:
            roots.append(extra.resolve())
        except OSError:
            continue
    return roots


def _check_roots(p: str | Path, roots: list[Path], what: str) -> Path:
    resolved = Path(p).expanduser().resolve()
    if not restrict_paths_active():
        return resolved
    for root in roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise ValueError(
        f"{what} path {resolved} is outside the allowed roots "
        f"({[str(r) for r in roots]}); "
        f"set VAI_ALLOWED_ROOTS to permit it"
    )


def assert_path_allowed(p: str | Path) -> Path:
    """Resolve `p` for READING and reject it if restriction is active and the
    path escapes every read root. Symlinks are followed during resolution so an
    attacker can't symlink-escape into /etc.

    Returns the resolved Path so callers can use it directly.
    Raises ValueError when the path is outside the allowlist.
    """
    return _check_roots(p, allowed_read_roots(), "read")


def assert_write_path_allowed(p: str | Path) -> Path:
    """Resolve `p` for WRITING and reject it if restriction is active and the
    path escapes every write root.

    Separate from `assert_path_allowed` because the two threat models are not
    the same size: reading the wrong file leaks data, writing the wrong file
    (a shell rc, a LaunchAgent plist) executes code the next time the user logs
    in. Callers must run this BEFORE any `mkdir(parents=True)` — creating the
    parent directory of a rejected path is itself a filesystem side effect an
    unauthenticated caller should not get.
    """
    return _check_roots(p, allowed_write_roots(), "write")


WORKDIR.mkdir(parents=True, exist_ok=True)


def session_dir(session_id: str) -> Path:
    d = WORKDIR / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d
