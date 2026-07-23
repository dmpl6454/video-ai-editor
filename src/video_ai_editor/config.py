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


# Run at import time — config is imported before any subprocess fires, and by
# every entrypoint (desktop .app, `uvicorn …:app`, tests), so this is the one
# universal chokepoint.
_augment_path_for_gui_launch()

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
                    return vf.read_text(encoding="utf-8").strip() or "0.0.0"
                except Exception:
                    pass
    return "0.0.0"


APP_VERSION = _read_version()

# Auto-load .env at the project root. Last assignment wins (POSIX-style) so
# stale duplicates higher up in the file are overridden by newer entries
# appended at the bottom.
def _apply_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    parsed: dict[str, str] = {}
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
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

# Path-restriction allowlist. When VAI_RESTRICT_PATHS=1 (multi-user / hosted
# deployment posture), tool args that point at filesystem paths must resolve
# beneath one of these roots. Default off preserves the local-desktop
# experience where Claude can `apply_lut("/Users/me/luts/teal.cube")`.
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


def assert_path_allowed(p: str | Path) -> Path:
    """Resolve `p` and reject if RESTRICT_PATHS is on and the path escapes
    every ALLOWED_PATH_ROOTS prefix. Symlinks are followed during resolution
    so an attacker can't symlink-escape into /etc.

    Returns the resolved Path so callers can use it directly.
    Raises ValueError when the path is outside the allowlist.
    """
    resolved = Path(p).expanduser().resolve()
    if not RESTRICT_PATHS:
        return resolved
    for root in ALLOWED_PATH_ROOTS:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise ValueError(
        f"path {resolved} is outside the allowed roots "
        f"({[str(r) for r in ALLOWED_PATH_ROOTS]}); "
        f"set VAI_ALLOWED_ROOTS to permit it"
    )


WORKDIR.mkdir(parents=True, exist_ok=True)


def session_dir(session_id: str) -> Path:
    d = WORKDIR / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d
