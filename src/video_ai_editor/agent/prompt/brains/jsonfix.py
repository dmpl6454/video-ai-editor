"""Repair almost-JSON from a small local model into a dict (spec §3.3).

`json.loads` first; only on failure apply repairs ONE AT A TIME, re-parsing
after each, in this order:

  1. fence strip          ```json … ``` and any prose before the first `{`
  2. first balanced object a string-aware scan for the outermost `{ … }`
  3. trailing commas      `,}` / `,]`
  4. Python literals      True / False / None
  5. unquoted keys        `{recipe: …}` → `{"recipe": …}`
  6. single quotes        `'…'` → `"…"` — LAST, because it is the only
                          repair that can change the meaning of a string:
                          `"don't scroll"` must survive untouched, and an
                          apostrophe inside a single-quoted string
                          (`'don't scroll'`) is only a closer when what
                          follows is a delimiter.
  7. close truncation     a 500-token cap can cut the object mid-value; drop
                          the dangling tail and close the open brackets.

WHY re-parse after every step instead of applying them all: each repair is a
guess about what the model meant, and every guess that was not needed is a
chance to corrupt a value that was fine. Stopping at the first parse that
succeeds keeps the repaired text as close to the original as possible.

Every repair is string-aware: nothing inside a quoted string is ever
rewritten by steps 3–5, so a Devanagari reply, a colon in a hook line or a
`True` in a caption keeps its bytes.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterator

__all__ = ["JsonRepairFailed", "repair", "REPAIR_STEPS"]


class JsonRepairFailed(ValueError):
    """No repair produced a JSON object. `.attempts` lists the steps tried."""

    def __init__(self, attempts: list[str], sample: str):
        super().__init__(f"could not repair model output after {attempts}: {sample[:120]!r}")
        self.attempts = attempts
        self.sample = sample


# --- string-aware segmentation ---------------------------------------------

_DELIMS_AFTER_STRING = set(",:}] \t\r\n")


def _segments(text: str) -> Iterator[tuple[str, bool]]:
    """Yield `(chunk, is_string)` so a repair can rewrite only the code
    between strings. Double-quoted strings honour backslash escapes; a
    single-quoted string ends at a `'` that is followed by a delimiter or
    the end of text (so `'don't scroll'` is one string)."""
    i, n = 0, len(text)
    code_start = 0
    while i < n:
        ch = text[i]
        if ch not in "\"'":
            i += 1
            continue
        if code_start < i:
            yield text[code_start:i], False
        j = i + 1
        while j < n:
            c = text[j]
            if c == "\\" and ch == '"':
                j += 2
                continue
            if c == ch:
                if ch == '"':
                    break
                nxt = text[j + 1:j + 2]
                if nxt == "" or nxt in _DELIMS_AFTER_STRING:
                    break
            j += 1
        end = min(j + 1, n)
        yield text[i:end], True
        i = end
        code_start = i
    if code_start < n:
        yield text[code_start:], False


def _map_code(text: str, fn: Callable[[str], str]) -> str:
    return "".join(chunk if is_str else fn(chunk) for chunk, is_str in _segments(text))


# --- the repairs, in ladder order --------------------------------------------

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)


def strip_fences(text: str) -> str:
    m = _FENCE.search(text)
    if m:
        return m.group(1).strip()
    first = text.find("{")
    return text[first:] if first > 0 else text


def first_balanced_object(text: str) -> str:
    start = text.find("{")
    if start < 0:
        return text
    depth = 0
    pos = start
    for chunk, is_str in _segments(text[start:]):
        if is_str:
            pos += len(chunk)
            continue
        for k, ch in enumerate(chunk):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:pos + k + 1]
        pos += len(chunk)
    return text[start:]


_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def drop_trailing_commas(text: str) -> str:
    return _map_code(text, lambda code: _TRAILING_COMMA.sub(r"\1", code))


_PY_LITERALS = {"True": "true", "False": "false", "None": "null"}
_PY_LITERAL_RE = re.compile(r"\b(True|False|None)\b")


def python_literals(text: str) -> str:
    return _map_code(text, lambda code: _PY_LITERAL_RE.sub(lambda m: _PY_LITERALS[m.group(1)], code))


_UNQUOTED_KEY = re.compile(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)")


def quote_keys(text: str) -> str:
    return _map_code(text, lambda code: _UNQUOTED_KEY.sub(r'\1"\2"\3', code))


def single_to_double_quotes(text: str) -> str:
    out: list[str] = []
    for chunk, is_str in _segments(text):
        if not is_str or chunk[0] != "'":
            out.append(chunk)
            continue
        inner = chunk[1:-1] if chunk.endswith("'") and len(chunk) >= 2 else chunk[1:]
        inner = inner.replace("\\'", "'").replace('"', '\\"')
        out.append(f'"{inner}"')
    return "".join(out)


def _open_closers(code_and_strings: str) -> list[str]:
    """Closers for every bracket still open in `text`, in opening order."""
    stack: list[str] = []
    for chunk, is_str in _segments(code_and_strings):
        if is_str:
            continue
        for ch in chunk:
            if ch in "{[":
                stack.append("}" if ch == "{" else "]")
            elif ch in "}]" and stack and stack[-1] == ch:
                stack.pop()
    return stack


def close_truncation(text: str) -> str:
    """Drop whatever follows the last complete value and close the brackets
    that are still open. Only reached when every gentler repair failed."""
    if not _open_closers(text):
        return text
    last_complete = -1
    pos = 0
    for chunk, is_str in _segments(text):
        if is_str:
            pos += len(chunk)
            if len(chunk) >= 2 and chunk[-1] == chunk[0]:
                last_complete = pos
            continue
        for k, ch in enumerate(chunk):
            if ch in "}]" or ch.isalnum() or ch == ".":
                last_complete = pos + k + 1
        pos += len(chunk)
    head = (text[:last_complete] if last_complete > 0 else text).rstrip().rstrip(",")
    # A dangling key — a string preceded by `{` or `,` (colon or not, the
    # cut may fall between them) — has no value and cannot be closed.
    head = re.sub(r'(?:,\s*|(?<=\{)\s*)"(?:[^"\\]|\\.)*"\s*(?::\s*)?$', "", head).rstrip().rstrip(",")
    # An object opened right before the cut (`, {` / `[{`) is now empty:
    # drop it rather than hand the caller a `{}` intent.
    while re.search(r"[,\[]\s*\{\s*$", head):
        head = re.sub(r",?\s*\{\s*$", "", head).rstrip()
    # WHY recompute from `head`: brackets opened AFTER the last complete
    # value were cut away with it, so the closers must come from what stays.
    return head + "".join(reversed(_open_closers(head)))


REPAIR_STEPS: tuple[tuple[str, Callable[[str], str]], ...] = (
    ("fence_strip", strip_fences),
    ("first_balanced_object", first_balanced_object),
    ("trailing_commas", drop_trailing_commas),
    ("python_literals", python_literals),
    ("unquoted_keys", quote_keys),
    ("single_quotes", single_to_double_quotes),
    ("close_truncation", close_truncation),
)


def _try_load(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def repair(text: str) -> dict[str, Any]:
    """Parse `text` into a JSON object, repairing it step by step if needed.
    Raises `JsonRepairFailed` when no step yields an object."""
    if not isinstance(text, str):
        raise JsonRepairFailed([], repr(text))
    current = text.strip()
    parsed = _try_load(current)
    if parsed is not None:
        return parsed
    attempts: list[str] = []
    for name, step in REPAIR_STEPS:
        candidate = step(current)
        attempts.append(name)
        if candidate == current:
            continue
        current = candidate
        parsed = _try_load(current)
        if parsed is not None:
            return parsed
    raise JsonRepairFailed(attempts, text)
