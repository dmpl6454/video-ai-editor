import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Auto-load .env at the project root. Last assignment wins (POSIX-style) so
# stale duplicates higher up in the file are overridden by newer entries
# appended at the bottom.
def _load_dotenv() -> None:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    parsed: dict[str, str] = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v:
            parsed[k] = v  # later entries overwrite earlier ones
    for k, v in parsed.items():
        # The shell env always wins over .env (so an explicit `export` or an
        # inline launch still beats stale .env entries).
        if k not in os.environ:
            os.environ[k] = v


_load_dotenv()

WORKDIR = PROJECT_ROOT / os.environ.get("WORKDIR", "workdir")
PRESETS_DIR = PROJECT_ROOT / "presets"
FONTS_DIR = PROJECT_ROOT / "fonts"

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "auto")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

DEFAULT_CANVAS = {"w": 1080, "h": 1920, "fps": 30}

WORKDIR.mkdir(parents=True, exist_ok=True)


def session_dir(session_id: str) -> Path:
    d = WORKDIR / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d
