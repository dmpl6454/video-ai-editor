"""Why couldn't this file be imported? — QA-112.

Every ingest failure used to produce the same sentence: "it may not be a valid
video, or it uses a codec/container we can't read. Try exporting it as a
standard H.264 .mp4". That is the right advice for exactly one of the cases a
user actually hits, and actively wrong for the others:

* a 0-byte file (a copy or download that never finished) — re-exporting does
  nothing, waiting for the copy does;
* a truncated video (the first 20 KB of an mp4: the index at the end is
  missing, ffprobe says "moov atom not found") — the file is damaged, not
  exotic;
* a document that is not media at all (notes.txt, a PDF, a .vae dropped on
  the video importer) — there is nothing to re-export.

`diagnose_unreadable` looks at the file ITSELF — its size, its first bytes
(content, not the extension: a text file renamed .mp4 still starts with text)
and what ffprobe says about it — and returns a `(code, message)` the upload
route answers with. It only runs on the failure path, after an import has
already failed, so its extra ffprobe costs nothing on a good import.
"""
from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

from .. import platformutil as _pu

_HEAD_BYTES = 4096

#: ffprobe/ffmpeg phrases that mean "this container is cut short or corrupt"
#: — as opposed to "I don't know what this is".
_DAMAGE_MARKERS = (
    "moov atom not found", "ended prematurely", "partial file", "truncat",
    "invalid data found", "error reading header", "end of file",
    "invalid nal", "no frame!", "corrupt",
)

#: ISO-BMFF box types that can open an mp4/mov file.
_BMFF_BOXES = {b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot", b"uuid"}


def _sniff(head: bytes) -> tuple[str, str] | None:
    """(family, label) from a file's first bytes; None when unrecognised.
    family: "video" | "audio" | "image" | "document"."""
    if len(head) >= 12 and head[4:8] in _BMFF_BOXES:
        brand = head[8:12] if head[4:8] == b"ftyp" else b""
        if brand in (b"heic", b"heix", b"mif1", b"msf1", b"hevc", b"heim", b"heis"):
            return "image", "HEIC photo"
        if brand == b"avif":
            return "image", "AVIF image"
        if brand in (b"M4A ", b"M4B "):
            return "audio", "M4A audio"
        return "video", "QuickTime (.mov)" if brand == b"qt  " else "MP4"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "video", "Matroska/WebM"
    if head.startswith(b"RIFF") and head[8:12] == b"AVI ":
        return "video", "AVI"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "audio", "WAV audio"
    if len(head) > 376 and head[0] == 0x47 and head[188] == 0x47 and head[376] == 0x47:
        return "video", "MPEG transport stream (.ts/.mts)"
    if len(head) > 388 and head[4] == 0x47 and head[196] == 0x47 and head[388] == 0x47:
        return "video", "AVCHD (.mts/.m2ts)"
    if head.startswith(b"\x00\x00\x01\xba"):
        return "video", "MPEG program stream"
    if head.startswith(b"FLV"):
        return "video", "Flash video (.flv)"
    if head.startswith(b"OggS"):
        return "audio", "Ogg"
    if head.startswith(b"fLaC"):
        return "audio", "FLAC audio"
    if head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "audio", "MP3 audio"
    if head.startswith(b"\xff\xd8\xff"):
        return "image", "JPEG photo"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "PNG image"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image", "GIF image"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "image", "TIFF image"
    if head.startswith(b"%PDF"):
        return "document", "a PDF document"
    if head.startswith(b"PK\x03\x04"):
        return "document", "a ZIP archive"
    if head.startswith(b"{\\rtf"):
        return "document", "a text document"
    if _looks_like_text(head):
        return "document", "a text document"
    return None


def _looks_like_text(head: bytes) -> bool:
    if not head:
        return False
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):          # UTF-16 BOM
        return True
    if b"\x00" in head:
        return False
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        # A multi-byte character cut at the 4 KB boundary is still text.
        try:
            text = head[:-3].decode("utf-8")
        except UnicodeDecodeError:
            return False
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\r\n\t")
    return printable >= 0.95 * max(1, len(text))


def _is_project_archive(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
    except (OSError, zipfile.BadZipFile):
        return False
    return "manifest.json" in names and "edl.json" in names


def _ffprobe(path: Path) -> tuple[bool, list[str], str]:
    """(ok, video codec names, stderr) — never raises."""
    try:
        out = subprocess.run(
            [_pu.FFPROBE, "-v", "error", "-show_entries", "stream=codec_type,codec_name",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, **_pu.SUBPROCESS_FLAGS)
    except (OSError, subprocess.SubprocessError):
        return False, [], ""
    codecs = [ln.split(",")[0] for ln in out.stdout.splitlines()
              if ln.strip().endswith(",video") or ln.strip().endswith("video")]
    return out.returncode == 0 and bool(out.stdout.strip()), codecs, out.stderr or ""


def diagnose_unreadable(path: Path, failure: str = "") -> tuple[str, str] | None:
    """`(code, message)` for a file whose import failed, or None when the file
    gives no better explanation than the generic one.

    `failure` is the import's own error text (ffmpeg's stderr tail); it is
    searched for damage markers alongside a fresh ffprobe of the file. The
    file's name is not in the sentence: every surface already shows it next
    to the message ("notes.txt: This is a text document, …"), and the route's
    answer carries it in `file`.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size == 0:
        return ("empty_file",
                "This file is empty (0 bytes). If it is still copying or "
                "downloading, wait for that to finish, then import it again.")
    try:
        with path.open("rb") as fh:
            head = fh.read(_HEAD_BYTES)
    except OSError:
        return None
    sniffed = _sniff(head)
    family, label = sniffed if sniffed else (None, None)

    if family == "document":
        if label == "a ZIP archive" and _is_project_archive(path):
            return ("not_media",
                    "This is a Video AI Editor project, not a clip. Open it with "
                    "Open project instead.")
        return ("not_media",
                f"This is {label}, not a video, audio or image file.")

    ok, video_codecs, stderr = _ffprobe(path)
    damaged = any(m in (stderr + "\n" + failure).lower() for m in _DAMAGE_MARKERS)

    if family in ("video", "audio", "image"):
        if damaged or not ok:
            return ("damaged_file",
                    f"This file is incomplete or damaged: it is "
                    f"{_article(label)} file, but part of it is missing or "
                    f"unreadable. If it is still copying or downloading, wait for "
                    f"that to finish; otherwise export it again from the camera or "
                    f"app it came from.")
        if video_codecs:
            return ("unsupported_codec",
                    f"This file uses a video format ({video_codecs[0]}) this "
                    f"editor can't convert. Export it as a "
                    f"standard H.264 .mp4 and import that instead.")
        return None

    if not ok:
        return ("not_media",
                "This isn't a video, audio or image file this editor recognises.")
    return None


#: ffmpeg demuxers that will "decode" a file that is not media at all:
#: `tty` renders any text of ~600+ bytes as ANSI art, `bin`/`xbin`/`adf`/`idf`
#: render raw bytes as a picture, and the subtitle demuxers read captions. A
#: successful probe by one of these is NOT evidence of a video (QA-112).
_NON_MEDIA_DEMUXERS = frozenset({"tty", "bin", "xbin", "adf", "idf"})
_SUBTITLE_DEMUXERS = frozenset({
    "srt", "ass", "ssa", "webvtt", "subviewer", "subviewer1", "microdvd", "mpl2",
    "jacosub", "sami", "realtext", "pjs", "vplayer", "stl", "aqtitle", "lrc", "scc",
    "mcc", "tedcaptions",
})
#: The text-art "video" codecs those demuxers hand out.
_NON_MEDIA_CODECS = frozenset({"ansi", "bintext", "xbin", "idf"})


def refuse_before_ingest(path: Path, probe) -> tuple[str, str] | None:
    """`(code, message)` for a file that must not be imported even though
    ffprobe read it, or None. Runs on the SUCCESS path, before normalising.

    `probe` is the upload's pre-probe (`ingest.probe.ProbeResult`, or None
    when it failed — then only the content sniff runs)."""
    try:
        with path.open("rb") as fh:
            head = fh.read(_HEAD_BYTES)
    except OSError:
        return None
    demuxers = set((getattr(probe, "format_name", "") or "").lower().split(","))
    if demuxers & _SUBTITLE_DEMUXERS:
        return ("not_media",
                "This is a subtitle file, not a video, audio or image file. Add it "
                "with Import subtitles in the AI tab instead.")
    sniffed = _sniff(head)
    if sniffed and sniffed[0] == "document" and b"<svg" in head[:2048].lower():
        # A drawing, not a document — and this ffmpeg has no SVG decoder.
        return ("not_media",
                "This is an SVG drawing, which the editor can't import. Export it as a PNG and "
                "import that instead.")
    if sniffed and sniffed[0] == "document":
        return diagnose_unreadable(path) or (
            "not_media", f"This is {sniffed[1]}, not a video, audio or image file.")
    video = getattr(probe, "video", None)
    codec = (getattr(video, "codec_name", "") or "").lower()
    if demuxers & _NON_MEDIA_DEMUXERS or codec in _NON_MEDIA_CODECS:
        return ("not_media",
                "This is not a video, audio or image file this editor recognises.")
    return None


#: A decoded result shorter than this share of what the container declares,
#: AND more than `_SHORTFALL_MIN_S` short, is a cut-off file, not a quirk.
_READABLE_SHARE_MIN = 0.5
_SHORTFALL_MIN_S = 1.0


def refuse_short_read(declared_s: float | None, readable_s: float | None) -> tuple[str, str] | None:
    """damaged_file when a container that survived truncation (its index is at
    the front, so ffprobe still reports the full length) decodes to a sliver:
    a 200 KB prefix of an 85 s mp4 normalised to 0.04 s and was placed as a
    1-frame clip (QA-112). Both conditions must hold, so a clip whose audio
    simply runs a little past its picture is never refused."""
    if not declared_s or declared_s <= 0 or readable_s is None:
        return None
    if readable_s >= _READABLE_SHARE_MIN * declared_s or declared_s - readable_s <= _SHORTFALL_MIN_S:
        return None
    return ("damaged_file",
            f"This file is incomplete or damaged — only {_clock(readable_s)} of "
            f"{_clock(declared_s)} could be read. If it is still copying or "
            f"downloading, wait for that to finish; otherwise export it again "
            f"from the camera or app it came from.")


def _clock(seconds: float) -> str:
    """0.0 s under a minute, else m:ss — how the Media rows state a length."""
    if seconds < 60:
        return f"{seconds:.1f} s"
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02d}"


def _article(label: str) -> str:
    """"an MP4", "an AVI", "a QuickTime (.mov)" — by sound, for the labels above."""
    vowel_sound = label[:1].upper() in "AEIOU" or label.startswith(("MP", "M4"))
    return f"an {label}" if vowel_sound else f"a {label}"
