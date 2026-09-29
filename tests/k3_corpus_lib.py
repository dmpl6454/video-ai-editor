"""Helpers for the K3 prompt safety-net corpus (tests/test_k3_prompt_corpus.py).

Not a test module: the corpus imports these so the phrase table stays a
table. Every predicate reads the EDL the real service left behind and
raises AssertionError when the edit is not what the phrase asked for.

The judge has two halves, and a result is CORRECT only when both hold:

  * the entry's own predicate — the edit the phrase asked for, on the clips
    it named, in the direction it said;
  * `collateral` — nothing OUTSIDE the aspects the entry declares may change
    (`touch`: v = main-lane clips, x = transitions, m = music, t = titles /
    text, c = captions, k = canvas, o = overlay lane, u = voice-over lane).
    A title that silently vanished, a music bed that moved, a canvas that
    flipped — each is a wrong edit even when the asked-for part is right.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from video_ai_editor.edl.schema import Clip, TextClip

TOL = 0.05
ASK = "ask"

Check = Callable[[Any, dict[str, str]], None]


# --------------------------------------------------------------------------- reads

def v1(e) -> list[Clip]:
    return sorted((c for c in e.get_track("v1").clips if isinstance(c, Clip)), key=lambda c: c.start)


def media_v1(e) -> list[Clip]:
    return [c for c in v1(e) if c.freeze is None]


def music(e) -> list[Clip]:
    t = e.get_track("music")
    return [c for c in t.clips if isinstance(c, Clip)] if t else []


def music_track(e):
    return e.get_track("music")


def texts(e) -> list[TextClip]:
    return sorted((c for t in e.tracks if t.id != "captions" for c in t.clips if isinstance(c, TextClip)),
                  key=lambda c: c.start)


def captions(e) -> list[TextClip]:
    t = e.get_track("captions")
    return [c for c in t.clips if isinstance(c, TextClip)] if t else []


def transitions(e) -> list:
    return sorted(e.get_track("v1").transitions, key=lambda t: t.at)


def dur(e) -> float:
    return round(e.video_extent(), 3)


def spd(c: Clip) -> float:
    return float(c.speed_factor)


def lut_names(c: Clip) -> list[str]:
    return [Path(str(x.params.get("src", ""))).name for x in c.effects if x.type == "lut"]


def color_fx(c: Clip) -> dict[str, float]:
    out: dict[str, float] = {}
    for x in c.effects:
        if x.type == "color":
            out.update({k: float(v) for k, v in x.params.items() if isinstance(v, (int, float))})
    return out


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) else None


def scale_end(c: Clip) -> float:
    """The scale the clip ends on: a static value, or the last key."""
    s = c.transform.scale
    if hasattr(s, "keyframes"):
        return float(s.keyframes[-1][1])
    return float(s)


def zoomed_in(c: Clip) -> bool:
    return scale_end(c) > 1.0 + 1e-6


def zoomed_out(c: Clip) -> bool:
    s = c.transform.scale
    if hasattr(s, "keyframes"):
        return float(s.keyframes[-1][1]) < float(s.keyframes[0][1]) - 1e-6
    return float(s) < 1.0 - 1e-6


def rotation(c: Clip) -> float:
    r = c.transform.rotation
    return float(r.keyframes[-1][1]) if hasattr(r, "keyframes") else float(r)


def coverage(e) -> list[tuple[float, float]]:
    """Merged SOURCE ranges the main lane plays (one source file)."""
    spans = sorted((float(c.in_), float(c.out)) for c in media_v1(e))
    out: list[list[float]] = []
    for a, b in spans:
        if out and a <= out[-1][1] + TOL:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(round(a, 2), round(b, 2)) for a, b in out]


def eq(a: Any, b: Any) -> None:
    assert a == b, f"{a!r} != {b!r}"


def near(a: float, b: float, tol: float = TOL) -> None:
    assert abs(float(a) - float(b)) <= tol, f"{a} != {b} ± {tol}"


def cov_is(*spans: tuple[float, float]) -> Check:
    def chk(e, ids):
        got = coverage(e)
        assert len(got) == len(spans), f"coverage {got} != {list(spans)}"
        for (a, b), (x, y) in zip(got, spans):
            near(a, x, 0.07)
            near(b, y, 0.07)
    return chk


# --------------------------------------------------------------------------- per-clip judges

def pristine(c: Clip) -> bool:
    s, r = c.transform.scale, c.transform.rotation
    return (c.speed in (None, 1.0) and not c.reverse and not c.audio.mute and abs(c.audio.gain_db) < 1e-9
            and not c.effects and not hasattr(s, "keyframes") and float(s) == 1.0
            and not hasattr(r, "keyframes") and float(r) == 0.0
            and c.video_fade_in == 0 and c.video_fade_out == 0
            and not c.audio.fade_in and not c.audio.fade_out
            and not getattr(c.transform, "flip_h", False) and not getattr(c.transform, "flip_v", False)
            and not (c.anim_in or c.anim_out or c.anim_combo) and not c.audio.voice_effect
            and c.canvas_bg is None and c.freeze is None)


def only(labels: str, pred: Callable[[Clip], bool]) -> Check:
    """The main lane still holds A, B, C (same ids); the clips `labels` names
    satisfy `pred`; every other one is untouched."""
    def chk(e, ids):
        by = {c.id: c for c in v1(e)}
        assert set(by) == set(ids.values()), f"clips changed: {[c.id for c in v1(e)]}"
        for lab, cid in ids.items():
            c = by[cid]
            dump = c.model_dump(exclude_defaults=True, exclude={"src", "id"})
            if lab in labels:
                assert pred(c), f"{lab} did not get the edit: {dump}"
            else:
                assert pristine(c), f"{lab} was edited too: {dump}"
    return chk


def every(pred: Callable[[Clip], bool], n: int = 3) -> Check:
    def chk(e, ids):
        cl = media_v1(e)
        assert len(cl) == n, f"{len(cl)} clips"
        bad = [c.model_dump(exclude_defaults=True, exclude={"src"}) for c in cl if not pred(c)]
        assert not bad, bad
    return chk


def either(*checks: Check) -> Check:
    def chk(e, ids):
        errs = []
        for c in checks:
            try:
                c(e, ids)
                return
            except AssertionError as err:
                errs.append(str(err))
        raise AssertionError(" | ".join(errs))
    return chk


def both(*checks: Check) -> Check:
    def chk(e, ids):
        for c in checks:
            c(e, ids)
    return chk


def ins_are(*ins: float) -> Check:
    def chk(e, ids):
        got = [round(c.in_, 2) for c in media_v1(e)]
        assert len(got) == len(ins) and all(abs(a - b) <= 0.07 for a, b in zip(got, ins)), f"{got} != {list(ins)}"
    return chk


def split_at(t: float) -> Check:
    def chk(e, ids):
        cl = v1(e)
        assert len(cl) == 4, [c.start for c in cl]
        assert any(abs(c.start - t) <= 0.04 for c in cl), [round(c.start, 3) for c in cl]
        near(dur(e), 12.0)
        assert all(c.speed in (None, 1.0) and not c.effects and not c.audio.mute for c in cl)
    return chk


def music_db(pred: Callable[[float], bool]) -> Check:
    def chk(e, ids):
        g = [round(c.audio.gain_db, 2) for c in music(e)]
        assert g and all(pred(x) for x in g), g
        assert not music_track(e).muted
    return chk


def music_is(db: float) -> Check:
    return music_db(lambda g: abs(g - db) <= 0.06)


def music_silenced(e, ids) -> None:
    mt = music_track(e)
    assert mt is None or mt.muted or not music(e) or all(c.audio.mute for c in music(e)), "music still plays"


def gains(e) -> list[float]:
    return [round(c.audio.gain_db, 2) for c in media_v1(e)]


def text_state(e) -> list[tuple[str, float, float]]:
    return [(t.text, round(t.start, 2), round(t.end, 2)) for t in texts(e)]


def the_text(e, word: str) -> TextClip:
    got = [t for t in texts(e) if word.lower() in t.text.lower()]
    assert len(got) == 1, text_state(e)
    return got[0]


def text_look(word: str, *, color: str | None = None, bigger: bool | None = None, size: float | None = None,
              font: str | None = None, y: str | None = None, keep: tuple[float, float] | None = (0.0, 3.0),
              base_size: float = 96.0, base_y: float = 810.0) -> Check:
    """The text containing `word` keeps its WORDS and (by default) its time,
    and carries the look asked for."""
    def chk(e, ids):
        t = the_text(e, word)
        if keep is not None:
            eq((round(t.start, 2), round(t.end, 2)), keep)
        if color is not None:
            eq(t.style.color.upper()[:7], color.upper())
        if bigger is True:
            assert t.style.size > base_size, t.style.size
        if bigger is False:
            assert t.style.size < base_size, t.style.size
        if size is not None:
            near(t.style.size, size, 0.5)
        if font is not None:
            assert (t.style.font or "").lower().startswith(font.lower()), t.style.font
        if y == "top":
            assert float(t.transform.y) < base_y - 1, t.transform.y
        if y == "bottom":
            assert float(t.transform.y) > base_y + 1, t.transform.y
    return chk


def text_time(word: str, start: float, end: float) -> Check:
    def chk(e, ids):
        t = the_text(e, word)
        near(t.start, start)
        near(t.end, end)
    return chk


def cap_look(pred: Callable[[Any, Any], bool]) -> Check:
    def chk(e, ids):
        cap = e.get_track("captions")
        assert cap is not None and captions(e), "no captions"
        assert pred(cap.config, cap.config.look), cap.config.model_dump()
    return chk


def anims(e) -> list[tuple[str | None, str | None, str | None]]:
    return [(c.anim_in, c.anim_out, c.anim_combo) for c in media_v1(e)]


def canvas_is(w: int, h: int) -> Check:
    def chk(e, ids):
        eq((e.canvas.w, e.canvas.h), (w, h))
    return chk


def bgs(e) -> list:
    out = []
    for c in media_v1(e):
        bg = c.canvas_bg
        out.append(None if bg is None else (bg.type, bg.color if bg.type == "color" else bg.blur))
    return out


def blends(e) -> list[str]:
    t = e.get_track("v2")
    return [c.blend for c in t.clips if isinstance(c, Clip)] if t else []


# --------------------------------------------------------------------------- collateral

ASPECT_CODES = {"v": "main-lane clips", "x": "transitions", "m": "music", "t": "text", "c": "captions",
                "k": "canvas", "o": "overlay lane", "u": "voice-over lane"}


def aspects(e) -> dict[str, str]:
    """A JSON fingerprint per aspect of the timeline."""
    def dump(obj) -> str:
        return json.dumps(obj, sort_keys=True, default=str)
    out: dict[str, str] = {}
    v1t = e.get_track("v1")
    out["v"] = dump([c.model_dump(exclude={"id"}) for c in v1t.clips] + [v1t.muted, getattr(v1t, "solo", False)])
    out["x"] = dump([t.model_dump() for t in v1t.transitions])
    mt = e.get_track("music")
    out["m"] = dump(mt.model_dump(exclude={"id"}) if mt else None)
    out["t"] = dump([t.model_dump(exclude={"id"}) for tr in e.tracks if tr.id not in ("captions",)
                     for t in tr.clips if isinstance(t, TextClip)])
    cap = e.get_track("captions")
    out["c"] = dump(cap.model_dump(exclude={"id"}) if cap else None)
    out["k"] = dump(e.canvas.model_dump())
    ov = e.get_track("v2")
    out["o"] = dump(ov.model_dump(exclude={"id"}) if ov else None)
    vo = e.get_track("vo")
    out["u"] = dump(vo.model_dump(exclude={"id"}) if vo else None)
    return out


def collateral(before: dict[str, str], after: dict[str, str], touch: str) -> list[str]:
    """The aspects that changed although the entry did not allow them."""
    return [ASPECT_CODES[k] for k in before if k not in touch and before[k] != after.get(k)]
