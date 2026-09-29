"""What a prompt run CHANGED, as categories an editor would name (K3 net).

`diff(before, after)` compares two EDLs and returns a `Diff`: per main-lane
clip (matched by id, or by being a piece of it after a split) the attributes
that moved, what happened to the lane's SOURCE coverage (removed = a cut or
a delete, added = a duplicate, a changed order = a move), and every other
lane — music, voice-over, overlay, titles, captions, transitions, canvas.

`contract.py` judges this against what the prompt asked (semantics.py);
nothing here knows about prompts. Kept separate so the diff is testable on
two hand-built EDLs and the judge on a hand-built Diff.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...edl.schema import EDL, Clip, Sticker, TextClip

#: Seconds below which two times are the same (a frame at 30 fps is 0.033).
EPS = 0.02
#: Source seconds a cut must remove before it counts as removed coverage.
COVER_EPS = 0.05


def _kf_last(v: Any) -> float:
    return float(v.keyframes[-1][1]) if hasattr(v, "keyframes") else float(v)


def _kf_first(v: Any) -> float:
    return float(v.keyframes[0][1]) if hasattr(v, "keyframes") else float(v)


def _is_kf(v: Any) -> bool:
    return hasattr(v, "keyframes")


def _dump(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def lut_names(c: Clip) -> list[str]:
    return [Path(str(x.params.get("src", ""))).name for x in c.effects if x.type == "lut"]


def lut_intensity(c: Clip, name: str) -> float:
    """The strength of the look `name` on `c` (1.0 when the LUT carries none;
    0.0 when the clip has no such look) — run 4, "make the warm look stronger"."""
    for x in c.effects:
        if x.type == "lut" and Path(str(x.params.get("src", ""))).name == name:
            v = x.params.get("intensity", 1.0)
            return float(v) if isinstance(v, (int, float)) else 1.0
    return 0.0


def color_params(c: Clip) -> dict[str, float]:
    out: dict[str, float] = {}
    for x in c.effects:
        if x.type == "color":
            out.update({k: float(v) for k, v in x.params.items() if isinstance(v, (int, float))})
    return out


def media_v1(edl: EDL) -> list[Clip]:
    t = edl.get_track("v1")
    if t is None:
        return []
    return sorted((c for c in t.clips if isinstance(c, Clip)), key=lambda c: c.start)


# --------------------------------------------------------------------------
# per-clip attributes
# --------------------------------------------------------------------------

#: attribute → reader. The category names are what contract.py licenses.
def clip_attrs(c: Clip) -> dict[str, Any]:
    tr = c.transform
    return {
        "speed": round(float(c.speed_factor), 4),
        "speed_shape": _dump(c.speed) if isinstance(c.speed, dict) else None,
        "reverse": bool(c.reverse),
        "mute": bool(c.audio.mute),
        "gain": round(float(c.audio.gain_db), 3),
        "gain_env": _dump(c.audio.gain_env) if c.audio.gain_env is not None else None,
        "look": _dump([(e.type, Path(str(e.params.get("src", ""))).name if e.type == "lut" else e.params)
                       for e in c.effects if e.type in ("lut", "color", "color_grade")]),
        "effects": _dump([e.type for e in c.effects if e.type not in ("lut", "color", "color_grade")]),
        "zoom": _dump(tr.scale.model_dump() if _is_kf(tr.scale) else round(float(tr.scale), 4)),
        "rotate": _dump(tr.rotation.model_dump() if _is_kf(tr.rotation) else round(float(tr.rotation), 3)),
        "position": _dump([tr.x.model_dump() if _is_kf(tr.x) else round(float(tr.x), 2),
                           tr.y.model_dump() if _is_kf(tr.y) else round(float(tr.y), 2)]),
        "opacity": _dump(tr.opacity.model_dump() if _is_kf(tr.opacity) else round(float(tr.opacity), 3)),
        "flip": (bool(tr.flip_h), bool(tr.flip_v)),
        "fade": (round(c.video_fade_in, 3), round(c.video_fade_out, 3)),
        "audio_fade": (round(float(c.audio.fade_in or 0), 3), round(float(c.audio.fade_out or 0), 3)),
        "anim": (c.anim_in, c.anim_out, c.anim_combo),
        "voice_fx": c.audio.voice_effect,
        "canvas_bg": _dump(c.canvas_bg.model_dump()) if c.canvas_bg is not None else None,
        "fit": c.fit,
        "blend": c.blend,
        "src": str(c.src),
        "framing": _dump(c.framing.model_dump()) if c.framing is not None else None,
        "mask": _dump(c.mask.model_dump()) if c.mask is not None else None,
        "chroma": _dump(c.chromakey.model_dump()) if c.chromakey is not None else None,
    }


#: Attributes a split / trim / cut legitimately rewrites on the pieces of a
#: clip (fades move to the outer pieces, a curve is re-sliced) — compared
#: only when the clip kept its whole span.
_PIECE_VOLATILE = frozenset({"fade", "audio_fade", "speed_shape", "gain_env"})


@dataclass
class ClipDelta:
    """One BEFORE main-lane clip and what became of it."""
    id: str
    index: int                                   # 1-based position before the run
    before: Clip
    pieces: list[Clip] = field(default_factory=list)   # after clips that are it (same id / split pieces)
    changed: set[str] = field(default_factory=set)     # attribute names that moved on any piece
    removed_src: float = 0.0                    # source seconds of it no longer played

    @property
    def gone(self) -> bool:
        return not self.pieces

    def after_values(self, attr: str) -> list[Any]:
        return [clip_attrs(p)[attr] for p in self.pieces]


@dataclass
class Diff:
    clips: list[ClipDelta] = field(default_factory=list)
    categories: set[str] = field(default_factory=set)
    removed_src: float = 0.0                    # source seconds cut from the main lane
    added_src: float = 0.0                      # source seconds played more than once more (duplicates)
    order_changed: bool = False
    splits: int = 0                             # more pieces than before, same coverage
    freezes_added: int = 0
    new_clips: list[Clip] = field(default_factory=list)   # after clips that are no piece of a before clip
    music: dict[str, Any] = field(default_factory=dict)
    texts: dict[str, Any] = field(default_factory=dict)
    captions: dict[str, Any] = field(default_factory=dict)
    transitions: dict[str, Any] = field(default_factory=dict)
    canvas: dict[str, Any] = field(default_factory=dict)
    before: EDL | None = None
    after: EDL | None = None

    def clip(self, cid: str) -> ClipDelta | None:
        return next((d for d in self.clips if d.id == cid), None)

    def changed_clips(self, attr: str) -> list[ClipDelta]:
        return [d for d in self.clips if attr in d.changed]


# --------------------------------------------------------------------------
# coverage
# --------------------------------------------------------------------------

def _spans(clips: list[Clip]) -> dict[str, list[tuple[float, float]]]:
    out: dict[str, list[tuple[float, float]]] = {}
    for c in clips:
        if c.freeze is not None:
            continue
        out.setdefault(str(c.src), []).append((float(c.in_), float(c.out)))
    return out


def _merge(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1] + 1e-6:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _length(spans: list[tuple[float, float]]) -> float:
    return sum(b - a for a, b in _merge(spans))


def _minus(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> float:
    """Seconds of `a` not covered by `b`."""
    a, b = _merge(a), _merge(b)
    total = 0.0
    for x, y in a:
        cur = x
        for p, q in b:
            if q <= cur or p >= y:
                continue
            if p > cur:
                total += p - cur
            cur = max(cur, q)
        if cur < y:
            total += y - cur
    return total


def coverage(edl: EDL) -> dict[str, list[tuple[float, float]]]:
    return {k: _merge(v) for k, v in _spans(media_v1(edl)).items()}


# --------------------------------------------------------------------------
# the diff
# --------------------------------------------------------------------------

def _curve_resliced(before: Clip, pieces: list[Clip]) -> bool:
    """`before` played a speed curve and `pieces` are that curve cut into
    parts: every piece still plays a curve, together they cover exactly the
    clip's source span, and together they last exactly as long. A piece given
    a constant speed, or a curve played faster or slower, fails one of these."""
    if not isinstance(before.speed, dict) or not pieces:
        return False
    if any(p.freeze is not None or not isinstance(p.speed, dict) for p in pieces):
        return False
    own = [(float(before.in_), float(before.out))]
    spans = [(float(p.in_), float(p.out)) for p in pieces]
    if _minus(own, spans) > EPS or _minus(spans, own) > EPS:
        return False
    return abs(sum(p.effective_duration for p in pieces) - before.effective_duration) <= EPS


def _piece_of(new: Clip, befores: list[Clip]) -> Clip | None:
    """The BEFORE clip a new after-clip is a piece of: the longest id prefix
    (split_at / cut_range pieces carry `<id>_<suffix>`)."""
    best: Clip | None = None
    for b in befores:
        if new.id.startswith(b.id + "_") and (best is None or len(b.id) > len(best.id)):
            best = b
    return best


def _text_map(edl: EDL) -> dict[str, TextClip]:
    return {c.id: c for t in edl.tracks if t.id != "captions" for c in t.clips if isinstance(c, TextClip)}


def _text_state(t: TextClip) -> dict[str, Any]:
    return {"text": t.text, "start": round(t.start, 3), "end": round(t.end, 3),
            "style": _dump(t.style.model_dump()), "transform": _dump(t.transform.model_dump()),
            "anim": (t.anim_in, t.anim_out), "role": t.role}


def _music_clips(edl: EDL) -> list[Clip]:
    t = edl.get_track("music")
    return [c for c in t.clips if isinstance(c, Clip)] if t else []


def diff(before: EDL, after: EDL) -> Diff:
    d = Diff(before=before, after=after)
    cats = d.categories
    b_clips, a_clips = media_v1(before), media_v1(after)
    b_by_id = {c.id: c for c in b_clips}
    deltas = {c.id: ClipDelta(id=c.id, index=i + 1, before=c) for i, c in enumerate(b_clips)}
    for a in a_clips:
        if a.id in deltas:
            deltas[a.id].pieces.append(a)
            continue
        parent = _piece_of(a, b_clips)
        if parent is not None:
            deltas[parent.id].pieces.append(a)
        else:
            d.new_clips.append(a)
            if a.freeze is not None:
                d.freezes_added += 1
    # per-clip attributes
    for dl in deltas.values():
        battrs = clip_attrs(dl.before)
        whole = len(dl.pieces) == 1 and abs(dl.pieces[0].in_ - dl.before.in_) < EPS \
            and abs(dl.pieces[0].out - dl.before.out) < EPS
        resliced = not whole and _curve_resliced(dl.before, dl.pieces)
        for p in dl.pieces:
            if p.freeze is not None and dl.before.freeze is None:
                dl.changed.add("freeze")
                continue
            pattrs = clip_attrs(p)
            for k, v in pattrs.items():
                if k in _PIECE_VOLATILE and not whole:
                    # a fade on a split clip stays on the outer piece; count it
                    # only when the value APPEARS where the whole clip had none
                    if k in ("fade", "audio_fade") and battrs[k] == (0.0, 0.0) and v != (0.0, 0.0):
                        dl.changed.add(k)
                    continue
                if k == "speed" and not whole and resliced:
                    # a re-sliced speed curve: each piece plays its own part of
                    # it, so its MEAN speed differs from the whole clip's while
                    # nothing about the playback changed (gate, 0.8.0)
                    continue
                if v != battrs[k]:
                    dl.changed.add(k)
        bs = _spans([dl.before])
        as_ = {str(dl.before.src): [(float(p.in_), float(p.out)) for p in dl.pieces if p.freeze is None]}
        dl.removed_src = sum(_minus(v, as_.get(k, [])) for k, v in bs.items())
        if dl.removed_src > COVER_EPS:
            dl.changed.add("cut")
        cats.update(f"clip:{k}" for k in dl.changed)
    d.clips = sorted(deltas.values(), key=lambda x: x.index)
    # coverage and structure — a piece is keyed by its ORIGIN clip's source,
    # so a clip whose file was re-rendered (reframe, noise reduction,
    # stabilise) is the same footage, not a cut plus a new clip
    b_cov = _spans(b_clips)
    a_cov: dict[str, list[tuple[float, float]]] = {}
    for dl in deltas.values():
        for p in dl.pieces:
            if p.freeze is None:
                a_cov.setdefault(str(dl.before.src), []).append((float(p.in_), float(p.out)))
    for c in d.new_clips:
        if c.freeze is None:
            a_cov.setdefault(str(c.src), []).append((float(c.in_), float(c.out)))
    d.removed_src = sum(_minus(v, a_cov.get(k, [])) for k, v in b_cov.items())
    # a clip's own pieces reaching past its old span = an extended trim;
    # a NEW clip (not a piece of one) replaying footage = a duplicate
    extended = 0.0
    for dl in deltas.values():
        own = [(float(dl.before.in_), float(dl.before.out))]
        extended += sum(_minus([(float(p.in_), float(p.out))], own) for p in dl.pieces if p.freeze is None)
    d.added_src = sum(float(c.out - c.in_) for c in d.new_clips if c.freeze is None and str(c.src) in b_cov)
    if extended > COVER_EPS:
        cats.add("v1:extend")
    if any(c.freeze is None and str(c.src) not in b_cov for c in d.new_clips):
        cats.add("v1:new_source")
    if d.removed_src > COVER_EPS:
        cats.add("v1:cut")
    if d.added_src > COVER_EPS:
        cats.add("v1:duplicate")
    if d.freezes_added:
        cats.add("v1:freeze")
    b_order = [c.id for c in b_clips]
    a_order = [c.id for c in a_clips if c.id in b_by_id]
    if [x for x in b_order if x in a_order] != a_order:
        d.order_changed = True
        cats.add("v1:move")
    pieces = sum(len(x.pieces) for x in d.clips)
    if pieces > len([x for x in d.clips if x.pieces]):
        d.splits = pieces - len([x for x in d.clips if x.pieces])
        cats.add("v1:split")
    if any(dl.gone for dl in d.clips):
        cats.add("v1:delete")
    b1, a1 = before.get_track("v1"), after.get_track("v1")
    if (b1 and a1) and (b1.muted != a1.muted or b1.solo != a1.solo):
        cats.add("v1:track")
    _diff_transitions(d, before, after)
    _diff_music(d, before, after)
    _diff_texts(d, before, after)
    _diff_captions(d, before, after)
    _diff_lanes(d, before, after)
    _diff_canvas(d, before, after)
    return d


def _diff_transitions(d: Diff, before: EDL, after: EDL) -> None:
    def rows(e: EDL) -> list[tuple[float, str, float]]:
        t = e.get_track("v1")
        return sorted((round(x.at, 2), x.type, round(float(x.duration or 0), 3)) for x in (t.transitions if t else []))
    b, a = rows(before), rows(after)
    if b == a:
        return
    info = {"before": b, "after": a}
    d.transitions = info
    if len(a) > len(b):
        d.categories.add("transitions:add")
    if len(a) < len(b):
        d.categories.add("transitions:remove")
    bt = [x[1] for x in b]
    at = [x[1] for x in a]
    if len(a) == len(b) and bt != at:
        d.categories.add("transitions:type")
    if len(a) == len(b) and [x[2] for x in b] != [x[2] for x in a]:
        d.categories.add("transitions:duration")
    if len(a) == len(b) and [x[0] for x in b] != [x[0] for x in a] and bt == at:
        d.categories.add("transitions:moved")


def _diff_music(d: Diff, before: EDL, after: EDL) -> None:
    bt, at = before.get_track("music"), after.get_track("music")
    bc, ac = _music_clips(before), _music_clips(after)
    info: dict[str, Any] = {"before_gain": [round(c.audio.gain_db, 2) for c in bc],
                            "after_gain": [round(c.audio.gain_db, 2) for c in ac]}
    if (bt.muted if bt else False) != (at.muted if at else False) or \
            [c.audio.mute for c in bc] != [c.audio.mute for c in ac]:
        d.categories.add("music:mute")
    bmap = {c.id: c for c in bc}
    for c in ac:
        b = bmap.get(c.id)
        if b is None:
            continue
        if abs(b.audio.gain_db - c.audio.gain_db) > 1e-6:
            d.categories.add("music:gain")
        if (b.audio.fade_in, b.audio.fade_out) != (c.audio.fade_in, c.audio.fade_out):
            d.categories.add("music:fade")
        if b.audio.voice_effect != c.audio.voice_effect:
            d.categories.add("music:voice_fx")
        if abs(b.speed_factor - c.speed_factor) > 1e-6:
            d.categories.add("music:speed")
        if (abs(b.in_ - c.in_) > EPS or abs(b.out - c.out) > EPS or abs(b.start - c.start) > EPS):
            d.categories.add("music:trim")
    if set(bmap) - {c.id for c in ac}:
        # a bed trimmed to the video can come back as a new piece
        d.categories.add("music:remove" if not ac else "music:trim")
    if {c.id for c in ac} - set(bmap):
        new = [c for c in ac if c.id not in bmap]
        if any(not any(c.id.startswith(b + "_") for b in bmap) for c in new) and \
                {str(c.src) for c in new} - {str(c.src) for c in bc}:
            d.categories.add("music:add")
        else:
            d.categories.add("music:trim")
    if _dump(bt.duck.model_dump() if bt and bt.duck else None) != _dump(at.duck.model_dump() if at and at.duck else None):
        d.categories.add("music:duck")
    d.music = info


def _diff_texts(d: Diff, before: EDL, after: EDL) -> None:
    bm, am = _text_map(before), _text_map(after)
    info: dict[str, Any] = {"added": [], "removed": [], "retexted": [], "restyled": [], "retimed": [],
                            "moved": []}
    for tid, t in bm.items():
        a = am.get(tid)
        if a is None:
            info["removed"].append(t)
            continue
        bs, as_ = _text_state(t), _text_state(a)
        if bs["text"] != as_["text"]:
            info["retexted"].append((t, a))
        if bs["style"] != as_["style"] or bs["anim"] != as_["anim"]:
            info["restyled"].append((t, a))
        if bs["start"] != as_["start"] or bs["end"] != as_["end"]:
            info["retimed"].append((t, a))
        if bs["transform"] != as_["transform"]:
            info["moved"].append((t, a))
    info["added"] = [a for tid, a in am.items() if tid not in bm]
    for key, cat in (("added", "text:add"), ("removed", "text:remove"), ("retexted", "text:content"),
                     ("restyled", "text:style"), ("retimed", "text:time"), ("moved", "text:position")):
        if info[key]:
            d.categories.add(cat)
    d.texts = info


def _diff_captions(d: Diff, before: EDL, after: EDL) -> None:
    b, a = before.get_track("captions"), after.get_track("captions")
    bc = [c for c in (b.clips if b else []) if isinstance(c, TextClip)]
    ac = [c for c in (a.clips if a else []) if isinstance(c, TextClip)]
    info = {"before": len(bc), "after": len(ac)}
    if not bc and ac:
        d.categories.add("captions:add")
    elif bc and not ac:
        d.categories.add("captions:remove")
    elif [(c.text) for c in bc] != [(c.text) for c in ac]:
        d.categories.add("captions:text")
    elif [(round(c.start, 2), round(c.end, 2)) for c in bc] != [(round(c.start, 2), round(c.end, 2)) for c in ac]:
        d.categories.add("captions:time")
    bcfg = _dump(b.config.model_dump() if b and b.config else None)
    acfg = _dump(a.config.model_dump() if a and a.config else None)
    if bcfg != acfg and bc and ac:
        d.categories.add("captions:style")
    if bc and ac and [_dump(c.style.model_dump()) for c in bc] != [_dump(c.style.model_dump()) for c in ac] \
            and len(bc) == len(ac):
        d.categories.add("captions:style")
    info["config_before"] = b.config if b else None
    info["config_after"] = a.config if a else None
    d.captions = info


def _lane_dump(edl: EDL, tid: str) -> str:
    t = edl.get_track(tid)
    return _dump(t.model_dump() if t else None)


#: A layer's TIMING fields — what the main lane's layer-follow (P3) moves.
_LAYER_TIME = frozenset({"start", "in_", "out"})


def _only_retimed(before: EDL, after: EDL, tid: str) -> bool:
    """True when lane `tid` differs ONLY in its clips' start / in / out —
    the same clips, nothing restyled, added or removed. A PIP that moved
    with the picture under it is `overlay:time`, licensed by the main-lane
    edit that moved it; a zoom, blend or effect stays `overlay:change`."""
    tb, ta = before.get_track(tid), after.get_track(tid)
    if tb is None or ta is None:
        return False
    if _dump(tb.model_dump(exclude={"clips"})) != _dump(ta.model_dump(exclude={"clips"})):
        return False
    if [c.id for c in tb.clips] != [c.id for c in ta.clips]:
        return False
    return all(_dump(b.model_dump(exclude=_LAYER_TIME)) == _dump(a.model_dump(exclude=_LAYER_TIME))
               for b, a in zip(tb.clips, ta.clips))


def _diff_lanes(d: Diff, before: EDL, after: EDL) -> None:
    for tid, cat in (("vo", "vo"), ("v2", "overlay"), ("v3", "overlay")):
        if _lane_dump(before, tid) != _lane_dump(after, tid):
            timing = cat == "overlay" and _only_retimed(before, after, tid)
            d.categories.add(f"{cat}:time" if timing else f"{cat}:change")
    bs = [(_dump(c.model_dump())) for t in before.tracks for c in t.clips if isinstance(c, Sticker)]
    as_ = [(_dump(c.model_dump())) for t in after.tracks for c in t.clips if isinstance(c, Sticker)]
    if bs != as_:
        d.categories.add("sticker:change")
    known = {"v1", "music", "vo", "v2", "v3", "captions", "text"}
    for t in after.tracks:
        if t.id in known or t.type in ("text", "sticker"):
            continue
        if _lane_dump(before, t.id) != _lane_dump(after, t.id):
            d.categories.add("other:lane")


def _diff_canvas(d: Diff, before: EDL, after: EDL) -> None:
    b, a = before.canvas, after.canvas
    if (b.w, b.h) != (a.w, a.h):
        d.categories.add("canvas:size")
    if b.fps != a.fps:
        d.categories.add("canvas:fps")
    if b.loudness_lufs != a.loudness_lufs:
        d.categories.add("canvas:loudness")
    if b.bitrate_kbps != a.bitrate_kbps:
        d.categories.add("canvas:export")
    rest_b = {k: v for k, v in b.model_dump().items() if k not in ("w", "h", "fps", "loudness_lufs", "bitrate_kbps")}
    rest_a = {k: v for k, v in a.model_dump().items() if k not in ("w", "h", "fps", "loudness_lufs", "bitrate_kbps")}
    if rest_b != rest_a:
        d.categories.add("canvas:other")
    d.canvas = {"before": (b.w, b.h, b.loudness_lufs), "after": (a.w, a.h, a.loudness_lufs)}
    if _dump(before.brand_kit.model_dump() if getattr(before, "brand_kit", None) else None) != \
            _dump(after.brand_kit.model_dump() if getattr(after, "brand_kit", None) else None):
        d.categories.add("brand:change")


__all__ = ["Diff", "ClipDelta", "diff", "clip_attrs", "coverage", "media_v1", "lut_names", "color_params"]
