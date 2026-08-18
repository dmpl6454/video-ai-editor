"""Regenerate `frontend/src/lib/emojiCatalog.ts` — the sticker picker's contents.

    uv run python scripts/gen_emoji_catalog.py

WHY THIS IS GENERATED AND NOT HAND-WRITTEN
------------------------------------------
The picker used to be a hand-curated list of 113 "viral" emoji. That is a
maintenance trap with one visible symptom: an emoji you want is simply absent,
with no way to reach it and no indication the app supports it. Reported as
"I couldn't find 😁" — which the app renders perfectly; it just was not on the
list. A curated list also silently rots as Unicode adds emoji.

So the catalogue is DERIVED from two sources and regenerated, never edited:

  1. Unicode's `emoji-test.txt` — the authoritative RGI set, already in CLDR
     keyboard order, already carrying group/subgroup and a name per emoji. It is
     the same file every real emoji keyboard is built from.
  2. The actual file inventories of the four artwork sources `ai/emoji.py`
     fetches from (Apple / Noto / Fluent 3D / Twemoji), matched with the EXACT
     filename spelling rules that module uses.

Intersecting the two is the point: **every emoji offered in the picker is one
we can actually render.** A picker that shows an emoji it cannot draw is worse
than one that omits it — you click, and get a blank sticker or an OS glyph that
disagrees with the export. At the pinned versions the intersection is total
(3781/3781 resolve), but that is a measured fact, not an assumption, and this
script re-measures it every run and refuses to emit a catalogue with holes.

SKIN TONES are kept out of the grid and hung off their base emoji instead.
1875 of the 3781 entries are tone variants, so inlining them would nearly
triple the grid and bury the base emoji among five near-identical neighbours —
directly harming the findability this whole change exists to fix. They are
matched to their base by stripping the tone codepoints, so the mapping comes
from Unicode's own data rather than from string surgery at runtime.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
import urllib.request
from collections import OrderedDict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "frontend" / "src" / "lib" / "emojiCatalog.ts"

EMOJI_TEST = "https://unicode.org/Public/emoji/16.0/emoji-test.txt"

# Kept in step with ai/emoji.py. Deliberately duplicated rather than imported:
# this script pins the artwork INVENTORY urls (a repo tree / a package listing),
# which are different endpoints from the per-file bases that module fetches.
APPLE_TREE = "https://api.github.com/repos/iamcal/emoji-data/git/trees/master"
NOTO_TREE = "https://api.github.com/repos/googlefonts/noto-emoji/git/trees/v2.047"
FLUENT_PKG = "https://data.jsdelivr.com/v1/packages/npm/@lobehub/fluent-emoji-3d@1.1.0?structure=flat"
TWEMOJI_TREE = "https://api.github.com/repos/jdecked/twemoji/git/trees/main"

TONES = [0x1F3FB, 0x1F3FC, 0x1F3FD, 0x1F3FE, 0x1F3FF]


def _get(url: str, *, as_json: bool = True):
    req = urllib.request.Request(url, headers={"User-Agent": "video-ai-editor-gen"})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    return json.loads(raw) if as_json else raw.decode("utf-8")


def _subtree(tree_url: str, *path: str) -> list[dict]:
    """Walk a GitHub tree one directory at a time.

    Not `?recursive=1`: noto-emoji is large enough that a recursive tree comes
    back truncated, and a truncated listing would look like "these emoji have no
    artwork" — silently shrinking the picker instead of failing.
    """
    node = _get(tree_url)
    for part in path:
        entry = next(e for e in node["tree"] if e["path"] == part)
        node = _get(entry["url"])
    return node["tree"]


def _cps(ch: str, *, keep_vs16: bool, pad: bool, sep: str) -> str:
    out = []
    for c in ch:
        o = ord(c)
        if o == 0xFE0F and not keep_vs16:
            continue
        out.append(f"{o:04x}" if pad else f"{o:x}")
    return sep.join(out)


def fetch_inventories() -> dict[str, set[str]]:
    print("fetching artwork inventories…", file=sys.stderr)
    apple = {e["path"][:-4] for e in _subtree(APPLE_TREE, "img-apple-160")
             if e["path"].endswith(".png")}
    noto = {e["path"][:-4] for e in _subtree(NOTO_TREE, "png", "512")
            if e["path"].endswith(".png")}
    fluent = {f["name"].rsplit("/", 1)[-1][:-5] for f in _get(FLUENT_PKG)["files"]
              if f["name"].endswith(".webp")}
    twemoji = {e["path"][:-4] for e in _subtree(TWEMOJI_TREE, "assets", "72x72")
               if e["path"].endswith(".png")}
    print(f"  apple={len(apple)} noto={len(noto)} fluent={len(fluent)} "
          f"twemoji={len(twemoji)}", file=sys.stderr)
    return {"apple": apple, "noto": noto, "fluent": fluent, "twemoji": twemoji}


def resolves(ch: str, inv: dict[str, set[str]]) -> str | None:
    """Which source would `ai.emoji.fetch_emoji_png` land on — or None.

    Mirrors that function's order and spellings exactly. If the two drift, the
    catalogue starts promising artwork the app fetches differently, which is
    the one thing generating it is supposed to prevent.
    """
    for keep in (True, False):
        if _cps(ch, keep_vs16=keep, pad=True, sep="-") in inv["apple"]:
            return "apple"
    if "emoji_u" + _cps(ch, keep_vs16=False, pad=True, sep="_") in inv["noto"]:
        return "noto"
    for keep in (True, False):
        if _cps(ch, keep_vs16=keep, pad=False, sep="-") in inv["fluent"]:
            return "fluent"
    if _cps(ch, keep_vs16=False, pad=False, sep="-") in inv["twemoji"]:
        return "twemoji"
    return None


LINE = re.compile(r"^([0-9A-Fa-f ]+);\s*(\S+)\s*#\s*(\S+)\s+E[\d.]+\s+(.*)$")


def parse_emoji_test() -> list[dict]:
    print("fetching emoji-test.txt…", file=sys.stderr)
    group = sub = ""
    rows: list[dict] = []
    for ln in _get(EMOJI_TEST, as_json=False).splitlines():
        if ln.startswith("# group:"):
            group = ln.split(":", 1)[1].strip()
        elif ln.startswith("# subgroup:"):
            sub = ln.split(":", 1)[1].strip()
        elif ln and not ln.startswith("#"):
            m = LINE.match(ln)
            # Only fully-qualified: the minimally-qualified spellings are the
            # SAME emoji missing a VS16, so including them would put visual
            # duplicates in the grid.
            if m and m.group(2) == "fully-qualified":
                rows.append({"group": group, "sub": sub, "ch": m.group(3),
                             "name": m.group(4)})
    return rows


def base_of(ch: str) -> str:
    """The tone-less spelling of `ch`, or `ch` itself."""
    return "".join(c for c in ch if ord(c) not in TONES)


def keywords(name: str, sub: str) -> str:
    """Extra search text beyond the name.

    The subgroup carries words a user actually types that the name lacks —
    "face-smiling" for 😁 ("beaming face with smiling eyes"), "food-fruit" for
    🍎. Cheap to include and it measurably widens what a query matches.
    """
    return sub.replace("-", " ")


def main() -> int:
    inv = fetch_inventories()
    rows = parse_emoji_test()

    # Report the split, not just the total: a chain that quietly stops answering
    # from its primary source is a style regression nothing else would surface.
    from collections import Counter
    split = Counter(resolves(r["ch"], inv) or "NONE" for r in rows)
    print("  resolves via: " + ", ".join(f"{k}={v}" for k, v in split.most_common()),
          file=sys.stderr)

    unresolved = [r for r in rows if resolves(r["ch"], inv) is None]
    if unresolved:
        # Refuse rather than quietly ship a picker with dead swatches.
        print(f"ERROR: {len(unresolved)} emoji have no artwork in any source:",
              file=sys.stderr)
        for r in unresolved[:20]:
            print(f"  {r['ch']!r} {r['name']}", file=sys.stderr)
        return 1

    # Bases in grid order; tone variants attached to their base.
    bases: OrderedDict[str, dict] = OrderedDict()
    tone_variants: dict[str, dict[int, str]] = {}
    for r in rows:
        ch = r["ch"]
        tones_in = [ord(c) for c in ch if ord(c) in TONES]
        if tones_in:
            tone_variants.setdefault(base_of(ch), {})[tones_in[0]] = ch
        elif ch not in bases:
            bases[ch] = r

    by_group: OrderedDict[str, list[dict]] = OrderedDict()
    for ch, r in bases.items():
        entry: dict = {"c": ch, "n": r["name"], "k": keywords(r["name"], r["sub"])}
        variants = tone_variants.get(ch)
        if variants and all(t in variants for t in TONES):
            # Only when all five exist — a partial set would make the tone
            # selector silently fall back for some emoji and not others.
            entry["t"] = [variants[t] for t in TONES]
        by_group.setdefault(r["group"], []).append(entry)

    total_bases = sum(len(v) for v in by_group.values())
    toned = sum(1 for g in by_group.values() for e in g if "t" in e)
    print(f"  {len(rows)} emoji -> {total_bases} bases ({toned} with skin tones)",
          file=sys.stderr)

    lines = [
        "// GENERATED by scripts/gen_emoji_catalog.py — do not edit by hand.",
        "//",
        "// Every entry here is an emoji the app can actually RENDER: the list is",
        "// Unicode's RGI set (emoji-test.txt, CLDR keyboard order) intersected with",
        "// the real file inventories of the three artwork sources ai/emoji.py",
        "// fetches from. The generator fails rather than emit an entry with no",
        "// artwork, because a swatch you can click but not draw is worse than an",
        "// absent one — you get a blank sticker instead of a missing option.",
        "//",
        "// `t` is the five skin-tone variants, present only when all five exist.",
        "// They are hung off the base rather than inlined so the grid stays",
        "// scannable: they are half of all emoji, and burying every base among",
        "// five near-identical neighbours is the opposite of findable.",
        "",
        "export interface EmojiEntry {",
        "  /** The emoji itself. */",
        "  c: string",
        "  /** Unicode CLDR name, shown as the tooltip and searched. */",
        "  n: string",
        "  /** Extra search words (the Unicode subgroup, de-hyphenated). */",
        "  k: string",
        "  /** Skin-tone variants, light→dark. Absent when the emoji has none. */",
        "  t?: string[]",
        "}",
        "",
        "export const EMOJI_CATALOG: { name: string; emojis: EmojiEntry[] }[] = [",
    ]
    for group, entries in by_group.items():
        lines.append(f"  {{ name: {json.dumps(group)}, emojis: [")
        for e in entries:
            lines.append("    " + json.dumps(e, ensure_ascii=False) + ",")
        lines.append("  ] },")
    lines.append("]")
    lines.append("")
    lines.append(f"/** Total pickable emoji, tone variants included. */")
    lines.append(f"export const EMOJI_COUNT = {len(rows)}")
    lines.append("")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    kb = OUT.stat().st_size / 1024
    print(f"wrote {OUT.relative_to(REPO)} ({kb:.0f} KB)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
