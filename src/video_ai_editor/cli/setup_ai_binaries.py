"""One-shot installer for the ncnn-vulkan binaries `upscale` and
`smooth_slow_motion` need: Real-ESRGAN and RIFE.

These are the two features `check_features` reports missing on a fresh
checkout — everything else (captions, stems, bg_remove, diarize, tracking,
beats, tts, translate, stabilize) is a pip package or ships with ffmpeg. These
two are standalone ncnn-vulkan executables the project has never bundled or
auto-fetched, because they are GPU binaries with per-OS builds and, for RIFE,
a genuinely large download (~430MB — it bundles every model generation, not
just the one this app calls by default).

Both modules already know WHERE to look (`ai/upscale.py::_esrgan_dir`,
`ai/rife.py::_rife_dir`, both preferring `<user data dir>/models/<name>`) —
this script is the other half: put the right binary there for THIS platform.

Run with:  uv run python -m video_ai_editor.cli.setup_ai_binaries
       or: uv run python -m video_ai_editor.cli.setup_ai_binaries --which upscale
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .. import platformutil as _pu

# Same convention as ai/emoji.py's `_download_ex` — GitHub does not require a
# UA, but some CDNs in front of release assets rate-limit or reject the
# default urllib one.
_UA = {"User-Agent": "video-ai-editor/0.1"}

_OS_KEY = "windows" if _pu.IS_WINDOWS else ("macos" if _pu.IS_MAC else "ubuntu")


@dataclass(frozen=True)
class BinaryTool:
    key: str                 # feature key in ai/features.py
    exe: str                 # binary name WITHOUT platform extension
    urls: dict[str, str]     # os key -> release asset URL
    dest_name: str           # subdirectory under models/ ("realesrgan" / "rife")
    verify: str              # dotted module path with an `available()` fn


TOOLS: dict[str, BinaryTool] = {
    "upscale": BinaryTool(
        key="upscale",
        exe="realesrgan-ncnn-vulkan",
        dest_name="realesrgan",
        verify="video_ai_editor.ai.upscale",
        urls={
            "windows": "https://github.com/xinntao/Real-ESRGAN/releases/download/"
                       "v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip",
            "macos": "https://github.com/xinntao/Real-ESRGAN/releases/download/"
                     "v0.2.5.0/realesrgan-ncnn-vulkan-20220424-macos.zip",
            "ubuntu": "https://github.com/xinntao/Real-ESRGAN/releases/download/"
                      "v0.2.5.0/realesrgan-ncnn-vulkan-20220424-ubuntu.zip",
        },
    ),
    "interpolate": BinaryTool(
        key="interpolate",
        exe="rife-ncnn-vulkan",
        dest_name="rife",
        verify="video_ai_editor.ai.rife",
        urls={
            "windows": "https://github.com/nihui/rife-ncnn-vulkan/releases/download/"
                       "20221029/rife-ncnn-vulkan-20221029-windows.zip",
            "macos": "https://github.com/nihui/rife-ncnn-vulkan/releases/download/"
                     "20221029/rife-ncnn-vulkan-20221029-macos.zip",
            "ubuntu": "https://github.com/nihui/rife-ncnn-vulkan/releases/download/"
                      "20221029/rife-ncnn-vulkan-20221029-ubuntu.zip",
        },
    ),
}


def _dest_dir(tool: BinaryTool) -> Path:
    """The SAME "new, per-OS" path `_esrgan_dir()`/`_rife_dir()` check first —
    this must stay in lockstep with those or the installed binary would sit
    somewhere `available()` never looks."""
    return _pu.user_data_dir("Video AI Editor") / "models" / tool.dest_name


def _fmt_mb(n: int) -> str:
    return f"{n / 1e6:.0f}MB"


def _download(url: str, dst: Path, *, label: str) -> None:
    """Stream to disk with a progress line, and verify the byte count against
    Content-Length so a truncated connection is caught HERE — as a plain
    error — rather than surfacing later as an unreadable zip.
    """
    # `\r`-overwrite only when stdout is a real terminal — piped into a log or
    # this project's own test/CI harness, `\r` does not erase anything and 45MB
    # at 1MB granularity becomes 45 concatenated progress lines.
    interactive = sys.stdout.isatty()
    req = urllib.request.Request(url, headers=_UA)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            last_pct = -1
            with open(dst, "wb") as f:
                while True:
                    chunk = resp.read(1 << 20)   # 1MB
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        pct = int(got * 100 / total)
                        # Every 10% when not interactive, so a log still shows
                        # progress without 45+ lines for a 45MB file.
                        if pct != last_pct and (interactive or pct % 10 == 0):
                            end = "" if interactive else "\n"
                            print(f"\r  {label}: {pct:3d}%  "
                                  f"({_fmt_mb(got)}/{_fmt_mb(total)})",
                                  end=end, flush=True)
                            last_pct = pct
            if interactive:
                print()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"download failed: {e}") from e
    if total and got != total:
        raise RuntimeError(
            f"download incomplete: got {got} of {total} bytes — "
            "network interrupted, try again")


def _common_prefix(names: list[str]) -> str:
    """If every entry in the archive sits under one shared top-level directory,
    return that directory name (with trailing '/'); else "".

    RIFE's release zips nest everything under `rife-ncnn-vulkan-<date>-<os>/`;
    Real-ESRGAN's are flat at the zip root. Detecting this generically — rather
    than hardcoding "RIFE nests, ESRGAN doesn't" — means a future release that
    changes either layout doesn't silently install one level too deep or too
    shallow.
    """
    tops = {n.split("/", 1)[0] for n in names if n.strip("/")}
    if len(tops) == 1:
        only = next(iter(tops))
        # Require at least one entry NESTED under `only` (not merely equal to
        # it) — a zip whose sole content is one top-level FILE must not have
        # that entry mistaken for a shared directory, which would strip it down
        # to an empty name and (per `_extract`'s defensive skip) install
        # nothing at all.
        if any(n.startswith(only + "/") for n in names):
            return only + "/"
    return ""


def _extract(zip_path: Path, dest: Path) -> None:
    """Unzip, stripping one shared top-level directory if the whole archive has
    one, so both layouts land the binary and its model/ dirs directly in
    `dest` — where `ESRGAN_DIR`/`RIFE_DIR` expect them.
    """
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        prefix = _common_prefix(names)
        for info in zf.infolist():
            name = info.filename
            if prefix:
                if not (name == prefix or name.startswith(prefix)):
                    continue          # shouldn't happen; be defensive
                name = name[len(prefix):]
            if not name:
                continue
            target = dest / name
            if info.is_dir() or name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            # zipfile does not reliably restore the executable bit on POSIX —
            # it only round-trips unix permissions when BOTH ends are POSIX and
            # the writer stored them (usually true here, but not guaranteed
            # across zip tools), and Windows has no bit to restore at all. The
            # binary itself gets an explicit chmod below regardless of this.
            if not _pu.IS_WINDOWS:
                mode = (info.external_attr >> 16) & 0o777
                if mode:
                    try:
                        os.chmod(target, mode)
                    except OSError:
                        pass


def install_one(which: str, *, force: bool = False) -> tuple[bool, str]:
    """Install one tool. Returns (ok, message)."""
    tool = TOOLS[which]
    url = tool.urls.get(_OS_KEY)
    if not url:
        return False, f"no {which} build published for this OS ({_OS_KEY})"

    dest = _dest_dir(tool)
    bin_path = dest / _pu.exe_name(tool.exe)
    if bin_path.exists() and not force:
        return True, f"already installed at {bin_path} (pass --force to redo)"

    print(f"Installing {which} for {_OS_KEY} -> {dest}")
    with tempfile.TemporaryDirectory(prefix="vai_binsetup_") as td:
        zip_path = Path(td) / "download.zip"
        try:
            _download(url, zip_path, label=which)
        except RuntimeError as e:
            return False, str(e)
        try:
            _extract(zip_path, dest)
        except zipfile.BadZipFile as e:
            return False, f"downloaded file is not a valid zip: {e}"

    if not _pu.IS_WINDOWS and bin_path.exists():
        try:
            os.chmod(bin_path, 0o755)   # belt-and-braces on top of _extract's
        except OSError:
            pass

    if not bin_path.exists():
        return False, (
            f"extracted, but the expected binary was not found at {bin_path} — "
            "the release layout may have changed; please file an issue")

    import importlib
    mod = importlib.import_module(tool.verify)
    importlib.reload(mod)   # the module cached ESRGAN_DIR/RIFE_DIR at import time
    if not mod.available():
        return False, f"installed but {tool.verify}.available() still says no"
    return True, f"installed and verified at {bin_path}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--which", choices=[*TOOLS.keys(), "all"], default="all",
                   help="which binary to install (default: all)")
    p.add_argument("--force", action="store_true",
                   help="reinstall even if already present")
    args = p.parse_args(argv)

    targets = list(TOOLS.keys()) if args.which == "all" else [args.which]

    print("=== AI binary setup (Real-ESRGAN / RIFE) ===")
    print(f"Platform: {_OS_KEY}\n")
    print("Sizes: upscale ~45-52MB, interpolate ~430MB (RIFE ships every model "
          "generation in one archive — there is no smaller official build).\n")

    ok_all = True
    for which in targets:
        ok, msg = install_one(which, force=args.force)
        print(f"  {'OK ' if ok else 'FAIL'} {which}: {msg}\n")
        ok_all = ok_all and ok

    if ok_all:
        print("Done. Re-run `check_features` (in-app or via the API) to confirm.")
        return 0
    print("One or more installs failed — see messages above. Nothing else on "
          "this machine was changed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
