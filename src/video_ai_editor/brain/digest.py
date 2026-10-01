"""Path-free, id-anchored digests for the model Gateway (spec §9.2).

This wave carries ONE task, `rank_moments`: ≤ 24 sentences per call as
`{windows: [{start, end, text}], k}` — the shape the built `fm-planner`
helper's `rank_windows` command already decodes, so no Swift rebuild is
needed. `assert_path_free` is the rule every payload builder is held to: no
rooted path, `..` hop or media-file name reaches a model (the `BrainRequest`
rule of the prompt machinery). It reads the payload's VALUES, not its JSON
text: a quotation mark in a spoken sentence is `\\"` in JSON and used to be
mistaken for a path."""
from __future__ import annotations

import re
from typing import Any

MAX_SENTENCES_PER_CALL = 24
MAX_TEXT_CHARS = 300

_MEDIA_SUFFIX = r"\.(?:mp4|mov|wav|m4a|mkv|mp3|aac|flac|png|jpe?g|json|srt|vtt)\b"
_TOKEN_LEAK = re.compile(r"[/\\~]|" + _MEDIA_SUFFIX, re.IGNORECASE)
#: What a REAL path looks like inside prose: rooted (`/…`, `~/…`, `C:\…`, `\\server\…`), a parent-directory hop, or a
#: name that ends in a media / data suffix. A quote, an apostrophe or `and/or` is not one.
_ROOTED = r"(?:~[\\/]|[A-Za-z]:[\\/]|\\\\|/(?=[^\s/]))"
_PATH_SHAPED = re.compile(r"(?<![\w.])" + _ROOTED + r"|(?<![\w])\.\.[\\/]|" + _MEDIA_SUFFIX, re.IGNORECASE)
#: A rooted path in an error message. A directory segment may hold spaces (the app's own folders do), the
#: file name may too when it ends in an extension; it stops at a colon, a quote, a bracket or a comma.
_STOP = r"\\/\n:'\"<>|,;()\[\]"
_ABS_IN_TEXT = re.compile(
    r"(?<![\w.:/\\-])(?:~|[A-Za-z]:|\\\\[^\\/\s]+)?(?:[\\/](?:(?!\.[A-Za-z0-9]{1,5}\s)[^" + _STOP + r"])+)*[\\/]"
    r"(?:(?:[^\s" + _STOP + r"]+ )*?[^\s" + _STOP + r"]*\.[A-Za-z0-9]{1,5}\b|[^\s" + _STOP + r"]+)")


#: `file:///Users/x/y.mp4` and ffmpeg's `file:/Users/x/y.mp4`: the scheme goes, the path that follows is scrubbed like any other.
_FILE_URL = re.compile(r"\bfile:(?://[^/\s]*)?(?=/)", re.IGNORECASE)
#: An unrooted path the rooted rules do not see: a directory-like first word (`Users/me/x`, `Library/Caches/x`), four or
#: more words between separators (`a/b/c/d`), or a backslash between word characters (`back\\slash`). `and/or`,
#: `24/7`, `left/right/centre` and `1/2/2024` are prose.
_DIR_WORDS = r"(?:users|home|library|volumes|private|var|tmp|etc|usr|opt|documents|movies|downloads|desktop|appdata|workdir|uploads)"
_RELATIVE = re.compile(r"(?<![\w.])" + _DIR_WORDS + r"[\\/]\w|(?<![\w.])[A-Za-z]\w*(?:[\\/]\w*[A-Za-z]\w*){3,}|\w\\[\w.]",
                       re.IGNORECASE)


class PathLeak(ValueError):
    """A path-like token was about to reach a model."""


def scrub(text: str) -> str:
    """The text with every path-like token removed (kept words untouched)."""
    return " ".join(t for t in str(text).split() if not _TOKEN_LEAK.search(t)).strip()


def _strings(obj: Any):
    """Every string VALUE and dict KEY of a payload, as Python sees it (never the JSON encoding: a `"`
    is `\\"` there, and the backslash used to read as a path)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            yield from _strings(v)


def assert_path_free(obj: Any) -> None:
    """Raises `PathLeak` when any string of `obj` holds a path-shaped token (a rooted path, `..`, a media
    file name). Quotes, apostrophes and ordinary slashes in prose are not paths."""
    for text in _strings(obj):
        m = _PATH_SHAPED.search(text) or _RELATIVE.search(text)
        if m:
            raise PathLeak(f"path-like token {text[max(0, m.start() - 12): m.end() + 12]!r} in a model payload")


def scrub_paths_in_text(text: Any) -> str:
    """`text` with every rooted path reduced to its leaf name — for error text that reaches the person
    (a failed read, the job record). Words around a path are kept; directories are not."""
    def leaf(m: re.Match) -> str:
        parts = [p for p in re.split(r"[\\/]", m.group(0)) if p]
        return parts[-1].strip() if parts else ""
    return _ABS_IN_TEXT.sub(leaf, _FILE_URL.sub("", str(text)))


def rank_moments_payload(sentences: list[dict], *, k: int | None = None) -> dict:
    """`{windows: [{start, end, text}], k}` for ≤ MAX_SENTENCES_PER_CALL
    sentences (`{id, t0, t1, text}`), path-free by construction and asserted."""
    chosen = list(sentences)[:MAX_SENTENCES_PER_CALL]
    windows = [{"start": round(float(s["t0"]), 2), "end": round(float(s["t1"]), 2),
                "text": scrub(s["text"])[:MAX_TEXT_CHARS]} for s in chosen]
    payload = {"windows": windows, "k": int(k) if k else len(windows)}
    assert_path_free(payload)
    return payload


__all__ = ["MAX_SENTENCES_PER_CALL", "PathLeak", "scrub", "assert_path_free", "scrub_paths_in_text",
           "rank_moments_payload"]
