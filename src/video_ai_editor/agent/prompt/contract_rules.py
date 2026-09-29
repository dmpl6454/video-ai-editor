"""The per-family rules of the prompt contract (contract.py): scope, then
direction and completeness per clause — speed, mute, level, look, adjust,
reverse / flip / rotate / zoom, cuts and keeps, captions, titles,
transitions, freezes. Each takes the judging context (`contract._Ctx`: the
reading, the diff, the timelines before and after) and returns violations.
Kept apart from the reading and the licensing so each file stays readable.
"""
from __future__ import annotations

import re
from typing import Callable

from ...edl.schema import EDL, Clip, TextClip
from . import semantics as M
from .contract import (TOL_DB, TOL_SPEED, _COLOURS, _TEXT_ADD_RE, _TEXT_REMOVE_RE, _TEXT_RESTYLE_RE,
                       _TEXT_RETEXT_RE, _TEXT_RETIME_RE, ClauseRead, Violation, _Ctx, _msg_clip, _rule_licensed,
                       families_of)
from .contract_diff import ClipDelta, color_params, coverage, lut_names, media_v1

# --------------------------------------------------------------------------
# 5. Scope: an attribute may only change on the clips its clauses name
# --------------------------------------------------------------------------

#: clip attribute → the families whose clauses may change it.
_ATTR_FAMILIES: dict[str, tuple[str, ...]] = {
    "speed": ("speed",), "reverse": ("reverse",), "mute": ("mute",), "gain": ("level",),
    "look": ("look", "adjust"), "flip": ("flip",), "rotate": ("rotate",), "zoom": ("zoom",),
    "voice_fx": ("voice_fx",), "anim": ("animation",), "fade": ("fade",), "canvas_bg": ("canvas_bg",),
    "effects": ("look", "adjust"),
}


def _range_overlap_ids(ctx: _Ctx, times: list) -> set[str] | None:
    """The main-lane clips (before the edit) a clause's time phrases cover;
    None when one cannot be placed."""
    vend = ctx.before.video_extent()
    if vend <= 0:
        return None
    spans: list[tuple[float, float]] = []
    for tr in times:
        if tr.kind == "first":
            spans.append((0.0, min(tr.a, vend)))
        elif tr.kind == "last":
            spans.append((max(0.0, vend - tr.a), vend))
        elif tr.kind == "range" and tr.b is not None:
            spans.append((tr.a, tr.b))
        elif tr.kind == "after":
            spans.append((tr.a, vend))
        elif tr.kind == "before":
            spans.append((0.0, tr.a))
        elif tr.kind == "at":
            spans.append((tr.a - 1e-3, tr.a + 1e-3))
        else:
            return None
    out: set[str] = set()
    for c in ctx.v1:
        s0, e0 = c.start, c.start + c.effective_duration
        if any(min(e0, b) - max(s0, a) > (0.0 if b - a < 0.01 else 0.05) for a, b in spans):
            out.add(c.id)
    return out


def _rule_scope(ctx: _Ctx) -> list[Violation]:
    if ctx.c.composite:
        return []
    out: list[Violation] = []
    for attr, fams in _ATTR_FAMILIES.items():
        changed = [dl for dl in ctx.d.clips if attr in dl.changed and dl.pieces]
        if not changed:
            continue
        reads = [r for r in ctx.c.reads if r.families & set(fams)]
        if not reads:
            continue          # licensing reports it
        allowed: set[str] = set()
        open_scope = False
        for r in reads:
            if M.time_refs(r.t) and not r.scope.refs:
                # final sweep 2 r2: "make the first 4 seconds black and white"
                # put the look on every clip — a time range names the clips it covers
                ids = _range_overlap_ids(ctx, M.time_refs(r.t))
                if ids is None:
                    open_scope = True
                    break
                allowed |= ids
                continue
            if attr in ("gain", "mute") and not r.scope.refs and not r.scope.all and \
                    set(r.scope.media) & {"music", "vo", "overlay"}:
                continue       # a music / VO level clause names no main-lane clip
            alts = ctx.targets(r)
            if alts is None:
                open_scope = True
                break
            for a in alts:
                allowed |= a
        if open_scope:
            continue
        for dl in changed:
            if dl.id not in allowed:
                named = ", ".join(sorted({f"clip {ctx.ids.index(x) + 1}" for x in allowed})) or "no clip"
                out.append(Violation("scope", reads[0].text,
                                     f"it changed the {attr.replace('_', ' ')} of {_msg_clip(ctx, dl.id)} too — "
                                     f"the request named {named}"))
    return out


# --------------------------------------------------------------------------
# 6. Direction and completeness per clause
# --------------------------------------------------------------------------

def _all_pieces(ctx: _Ctx, ids: set[str]) -> list[tuple[ClipDelta, Clip]]:
    out = []
    for cid in ids:
        dl = ctx.delta(cid)
        if dl is None:
            continue
        out.extend((dl, p) for p in dl.pieces if p.freeze is None)
    return out


def _satisfies_any(ctx: _Ctx, alts: list[set[str]], ok: Callable[[ClipDelta, Clip], bool]) -> bool:
    for alt in alts:
        pairs = _all_pieces(ctx, alt)
        if pairs and all(ok(dl, p) for dl, p in pairs):
            return True
    return False


def _clause_is_timed(r: ClauseRead) -> bool:
    return bool(M.time_refs(r.t))


def _rule_speed(ctx: _Ctx) -> list[Violation]:
    out = []
    for r in ctx.c.reads_with("speed"):
        t = r.t
        if re.search(r"\b(?:ramp|curve|montage|hero|bullet|jump[- ]?cut|flash[- ]?(?:in|out)|time[- ]?lapse|smooth)\b", t):
            continue
        if set(r.scope.media) & {"music", "vo", "overlay"} and not r.scope.refs:
            if "clip:speed" in ctx.d.categories and ctx.d.changed_clips("speed"):
                out.append(Violation("scope", r.text, "it changed the speed of the video's clips, but the request "
                                                      "was about " + "/".join(r.scope.media)))
            continue
        sa = M.speed_ask(t)
        if sa.ambiguous:
            if ctx.d.changed_clips("speed"):
                out.append(Violation("direction", r.text, sa.ambiguous))
            continue
        if sa.direction in (None, "both"):
            if sa.factor is None and ctx.d.changed_clips("speed") and re.search(r"\bspeed\b|\bpace\b|\btempo\b", t) \
                    and not r.scope.media:
                out.append(Violation("direction", r.text, "it did not say faster or slower, or by how much"))
            continue
        # direction guard: no clip may move against the clause's direction
        # (unless another clause asks for the other way on it)
        others = [o for o in ctx.c.reads_with("speed") if o is not r]
        for dl in ctx.d.changed_clips("speed"):
            if not dl.pieces:
                continue
            b = float(dl.before.speed_factor)
            for p in dl.pieces:
                a = float(p.speed_factor)
                wrong = (sa.direction == "up" and a < b - 1e-6) or (sa.direction == "down" and a > b + 1e-6) \
                    or (sa.direction == "reset" and abs(a - 1.0) > TOL_SPEED and not others)
                if wrong and not any(M.speed_ask(o.t).direction not in (sa.direction, None) for o in others):
                    out.append(Violation("direction", r.text,
                                         f"it asked for {'faster' if sa.direction == 'up' else 'slower' if sa.direction == 'down' else 'normal speed'}, "
                                         f"but {_msg_clip(ctx, dl.id)} went from {b:g}x to {a:g}x"))
                    break
        if _clause_is_timed(r):
            continue
        alts = ctx.targets(r)
        if alts is None:
            continue

        def ok(dl: ClipDelta, p: Clip) -> bool:
            b, a = float(dl.before.speed_factor), float(p.speed_factor)
            if sa.factor is not None:
                return abs(a - sa.factor) <= TOL_SPEED
            # no amount: a step from the clip's own speed, never a leap past
            # half / double it ("a bit slower" took 2x to 0.8x)
            if sa.direction == "up":
                return b + 1e-6 < a <= b * 2.0 + 1e-6
            if sa.direction == "down":
                return b * 0.5 - 1e-6 <= a < b - 1e-6
            return abs(a - 1.0) <= TOL_SPEED
        if not _satisfies_any(ctx, alts, ok):
            want = f"{sa.factor:g}x" if sa.factor is not None else ("faster" if sa.direction == "up" else "slower")
            out.append(Violation("partial", r.text, f"it asked for {want} on "
                                 f"{_names(ctx, alts[0])}, and that is not what the timeline plays"))
    return out


def _rule_zoom_not_speed(ctx: _Ctx) -> list[Violation]:
    """final sweep 2 r2: "zoom clip 1 to 2x" committed set_speed 2x. A speed
    change needs a clause that talks about speed; a zoom / scale clause alone
    never licenses one."""
    if ctx.c.composite or not ctx.d.changed_clips("speed"):
        return []
    zooms = [r for r in ctx.c.reads if M.ZOOM_WORD_RE.search(r.t)]
    if zooms and not any(M.SPEED_WORD_RE.search(r.t) or M.direction(r.t, "speed") for r in ctx.c.reads):
        return [Violation("unasked", zooms[0].text, "it changed a clip's speed, but the request was about its zoom")]
    return []


_MUSIC_OBJ_RE = re.compile(r"\b(?:of|off|from)\s+(?:the\s+|my\s+)?(?:background\s+)?(?:music|song|soundtrack|bgm|bed|tune)\b"
                           r"|^(?:please\s+)?(?:trim|cut|shorten|end|stop|chop)\s+(?:the\s+|my\s+)?(?:background\s+)?"
                           r"(?:music|song|soundtrack|bgm|bed|tune)\b")


def _rule_music_words(ctx: _Ctx) -> list[Violation]:
    """final sweep 2 r2: music requests that edited something else —
    "remove the last 3 seconds of the music" cut the VIDEO, "remove the music
    ducking" turned ducking ON, "replace the music with silence" added a bed."""
    out: list[Violation] = []
    bm = {c.id for c in (ctx.before.get_track("music").clips if ctx.before.get_track("music") else [])}
    am = [c for c in (ctx.after.get_track("music").clips if ctx.after.get_track("music") else [])]
    for r in ctx.c.reads:
        t = r.t
        if "cut" in r.families and _MUSIC_OBJ_RE.search(t) and not r.scope.refs \
                and coverage(ctx.before) != coverage(ctx.after):
            out.append(Violation("scope", r.text, "it cut the video, but the request was about the music"))
        if re.search(r"\bduck|\bsidechain", t) and _REMOVAL_RE.search(t) and _ducked_now(ctx):
            out.append(Violation("direction", r.text, "it asked to take the ducking off, but the music now ducks"))
        if re.search(r"\b(?:with|for)\s+(?:silence|nothing|no\s+music|no\s+sound|quiet)\b", t) \
                and any(c.id not in bm for c in am):
            out.append(Violation("unasked", r.text, "it added new music, but the request asked for silence"))
    return out


def _rule_gap_words(ctx: _Ctx) -> list[Violation]:
    """final sweep 2 r2: "remove the gap" (no gap) removed 3.8 s of SILENCE;
    "remove clip 2 but keep the gap" closed it."""
    out: list[Violation] = []
    dropped = ctx.after.video_extent() < ctx.before.video_extent() - 0.05
    for r in ctx.c.reads:
        t = r.t
        if dropped and re.search(r"\b(?:keep|leave)\s+(?:the\s+|a\s+|its\s+)?(?:gap|space|hole|spot)\b", t):
            out.append(Violation("unasked", r.text, "it closed the gap the request said to keep"))
        elif coverage(ctx.before) != coverage(ctx.after) and re.search(r"\bgaps?\b", t) \
                and not re.search(r"\b(?:silen\w*|pauses?|quiet|dead air|awkward|breath\w*|umm?s?|fillers?)\b", t) \
                and "cut" in r.families and not M.time_refs(t) and not r.scope.refs:
            out.append(Violation("unasked", r.text, "it cut parts of the clips, but the request named a gap between "
                                                    "them"))
    return out


def _names(ctx: _Ctx, ids: set[str]) -> str:
    if set(ids) == set(ctx.ids) and len(ids) > 1:
        return "every clip"
    return ", ".join(sorted(_msg_clip(ctx, i) for i in ids)) or "no clip"


def _rule_mute(ctx: _Ctx) -> list[Violation]:
    out = []
    for r in ctx.c.reads_with("mute"):
        t = r.t
        if "mute" not in families_of(t) and r.inherited is False:
            continue
        unmute = bool(re.search(r"\bun-?mute|\bsound back on\b|\baudio back on\b", t))
        media = set(r.scope.media)
        keep_music = bool(re.search(r"\b(?:but|except|keep|apart from|other than|besides)\b.*\b(?:music|song|soundtrack)\b", t))
        if "music" in media and not r.scope.refs and not r.scope.all and not keep_music:
            mt = ctx.after.get_track("music")
            muted = bool(mt and (mt.muted or all(c.audio.mute for c in mt.clips if isinstance(c, Clip))))
            if muted == unmute and mt is not None and mt.clips:
                out.append(Violation("partial", r.text, "the music is " + ("still muted" if unmute else "still playing")))
            if ctx.d.changed_clips("mute"):
                out.append(Violation("scope", r.text, "it muted a clip, but the request was about the music"))
            continue
        if keep_music and "music:mute" in ctx.d.categories:
            out.append(Violation("scope", r.text, "it muted the music, which the request said to keep"))
        if _clause_is_timed(r) or (media - {"voice"} and not r.scope.refs and not r.scope.all):
            continue
        alts = ctx.targets(r)
        if alts is None:
            continue
        if not _satisfies_any(ctx, alts, lambda dl, p: bool(p.audio.mute) != unmute):
            out.append(Violation("partial", r.text, f"{_names(ctx, alts[0])} "
                                 f"{'is still muted' if unmute else 'still has sound'}"))
    return out


def _ducked_now(ctx: _Ctx) -> bool:
    b, a = ctx.before.get_track("music"), ctx.after.get_track("music")
    return bool(a and a.duck and (not b or not b.duck or a.duck.to_db < b.duck.to_db))


def _music_gains(e: EDL) -> list[float]:
    t = e.get_track("music")
    return [float(c.audio.gain_db) for c in (t.clips if t else []) if isinstance(c, Clip)]


def _lane_gains(e: EDL, lane: str) -> list[float]:
    t = e.get_track(lane)
    return [float(c.audio.gain_db) for c in (t.clips if t else []) if isinstance(c, Clip)]


def _rule_level(ctx: _Ctx) -> list[Violation]:
    out = []
    for r in ctx.c.reads_with("level"):
        t = r.t
        if "level" not in families_of(t) or re.search(r"\blufs\b|\bloudness\b|\bnormali", t):
            continue
        la = M.level_ask(t)
        if la.ambiguous:
            out.append(Violation("direction", r.text, la.ambiguous))
            continue
        # final sweep 2 r2: a lane named only as the REFERENCE ("… under the
        # voiceover", "… than the music", "so my voiceover is clear") keeps its
        # level ("lower the clip audio to -18 dB under the voiceover" lowered the VO)
        ref_moved = [lane for lane in ("vo", "music") if lane in r.scope.ref_media and lane not in r.scope.media
                     and _lane_gains(ctx.before, lane) != _lane_gains(ctx.after, lane)]
        if ref_moved:
            what = "voiceover" if ref_moved[0] == "vo" else "music"
            out.append(Violation("scope", r.text, f"it changed the {what}'s level, but the request named the {what} "
                                                  "only as the sound to keep clear"))
            continue
        if la.direction in (None, "both") and la.db is None and la.delta_db is None:
            if la.bare_number and (ctx.d.changed_clips("gain") or _music_gains(ctx.before) != _music_gains(ctx.after)):
                # "clip 2 volume +2" took -6 dB to -12 dB: a number the reading
                # could not place never licenses a default step
                out.append(Violation("partial", r.text, "it changed a level by an amount the request did not give"))
            continue
        media = set(r.scope.media[:1]) if r.scope.media else set()
        down, up = la.direction == "down", la.direction == "up"
        bm, am = _music_gains(ctx.before), _music_gains(ctx.after)
        music_moved = bm != am and len(bm) == len(am)
        if music_moved and "music" in r.scope.except_media:
            out.append(Violation("scope", r.text, "it changed the music's volume, which the request said to leave out"))
        # direction guard on the music
        if music_moved and ("music" in media or (not media and not r.scope.refs)):
            for b, a in zip(bm, am):
                if la.db is not None and "music" in media:
                    if abs(a - la.db) > TOL_DB and not (la.delta_db is not None):
                        pass
                if (down and a > b + 1e-6) or (up and a < b - 1e-6):
                    out.append(Violation("direction", r.text,
                                         f"it asked for {'quieter' if down else 'louder'} but the music went "
                                         f"from {b:g} dB to {a:g} dB"))
                    break
        # loudness target moved the wrong way
        bl, al = ctx.before.canvas.loudness_lufs, ctx.after.canvas.loudness_lufs
        if bl is not None and al is not None and bl != al and ((down and al > bl) or (up and al < bl)):
            out.append(Violation("direction", r.text, "the overall loudness went the other way"))
        if "music" in media and not r.scope.refs and "voice" not in media:
            if ctx.d.categories & {"canvas:loudness"} and not ctx.c.reads_with("loudness"):
                out.append(Violation("scope", r.text, "it changed the whole video's loudness, but the request was "
                                                      "about the music"))
            if not am:
                continue
            clamped = [a <= -40.0 + 1e-6 or a >= 6.0 - 1e-6 for a in am]      # the planner's level bounds
            if la.delta_db is not None and len(am) == len(bm) and not _ducked_now(ctx) and any(
                    abs(a - (b + la.delta_db)) > max(TOL_DB, 0.11) and not c for a, b, c in zip(am, bm, clamped)):
                # "music +3db" set +3 dB (17 dB louder than asked)
                out.append(Violation("partial", r.text, f"the music did not move by {la.delta_db:+g} dB"))
                continue
            if la.db is not None and la.delta_db is None:
                if any(abs(a - la.db) > TOL_DB for a in am):
                    if (down and la.db > max(bm or [la.db])) or (up and la.db < min(bm or [la.db])):
                        out.append(Violation("direction", r.text, f"{la.db:g} dB would make the music "
                                             f"{'louder' if down else 'quieter'}, the opposite of what was asked"))
                    else:
                        out.append(Violation("partial", r.text, f"the music is not at {la.db:g} dB"))
            elif down and not all(a < b - 1e-6 for a, b in zip(am, bm)) and not _ducked_now(ctx):
                out.append(Violation("partial", r.text, "the music did not get quieter"))
            elif up and not all(a > b + 1e-6 for a, b in zip(am, bm)):
                out.append(Violation("partial", r.text, "the music did not get louder"))
            continue
        clip_level = bool(r.scope.refs) or "voice" in media
        if not clip_level:
            continue
        if "canvas:loudness" in ctx.d.categories and r.scope.refs and not ctx.c.reads_with("loudness"):
            out.append(Violation("scope", r.text, f"it changed the whole video's loudness, but the request named "
                                                  f"{_names(ctx, ctx.targets(r)[0]) if ctx.targets(r) else 'a clip'}"))
        if music_moved and "music" not in media and not any("music" in o.scope.media for o in ctx.c.reads):
            out.append(Violation("scope", r.text, "it changed the music's volume, but the request named the clips"))
        alts = ctx.targets(r) if r.scope.refs else [set(ctx.ids)]
        if alts is None or _clause_is_timed(r):
            continue

        def ok(dl: ClipDelta, p: Clip) -> bool:
            b, a = float(dl.before.audio.gain_db), float(p.audio.gain_db)
            if la.db is not None and la.delta_db is None:
                # "volume to 0%" (−60 dB) is the quietest a clip goes (−40 dB) or a mute
                return abs(a - la.db) <= TOL_DB or (la.db < -40.0 and (a <= -40.0 + 1e-6 or bool(p.audio.mute)))
            if la.delta_db is not None:
                return abs(a - (b + la.delta_db)) <= TOL_DB
            return a > b + 1e-6 if up else a < b - 1e-6
        if "vo" in media or (ctx.after.get_track("vo") and ctx.after.get_track("vo").clips and "voice" in media
                             and "vo:change" in ctx.d.categories):
            continue
        if not _satisfies_any(ctx, alts, ok):
            out.append(Violation("partial", r.text, f"the level of {_names(ctx, alts[0])} is not what was asked"))
    return out


#: Removal wording: "remove X", "take / turn / switch X off", "get rid of X".
_REMOVAL_RE = re.compile(r"\b(?:remove|take off|get rid of|strip|clear|turn off|switch off|delete|undo|no|without|lose"
                         r"|ditch|kill)\b|\b(?:take|turn|switch|get)\b.*\boff\b")


def _rule_fade_off(ctx: _Ctx) -> list[Violation]:
    """"take the fade off the last clip" / "remove the fades" ADDED fades: a
    fade clause with removal wording may only lower fades."""
    out = []
    for r in ctx.c.reads_with("fade"):
        if not _REMOVAL_RE.search(r.t):
            continue
        says_in = bool(re.search(r"\bfade[- ]?ins?\b|\bfrom black\b", r.t))
        says_out = bool(re.search(r"\bfade[- ]?outs?\b|\bto black\b", r.t))
        if says_in != says_out:
            # final sweep 2 r2: "remove the fade in" removed the fade OUT too
            keep = ("video_fade_out", "out") if says_in else ("video_fade_in", "in")
            for dl in ctx.d.clips:
                b = dl.before
                for p in dl.pieces:
                    if abs(getattr(p, keep[0]) - getattr(b, keep[0])) > 1e-6 or \
                            abs(getattr(p.audio, f"fade_{keep[1]}") - getattr(b.audio, f"fade_{keep[1]}")) > 1e-6:
                        out.append(Violation("unasked", r.text, f"it took the fade-{keep[1]} off {_msg_clip(ctx, dl.id)} "
                                                                f"too — the request named the fade-{'in' if says_in else 'out'}"))
                        break
                if out:
                    return out
        for dl in ctx.d.clips:
            b = dl.before
            for p in dl.pieces:
                if (p.video_fade_in > b.video_fade_in + 1e-6 or p.video_fade_out > b.video_fade_out + 1e-6
                        or p.audio.fade_in > b.audio.fade_in + 1e-6 or p.audio.fade_out > b.audio.fade_out + 1e-6):
                    return [Violation("direction", r.text, f"it asked to take a fade off, but {_msg_clip(ctx, dl.id)} "
                                                           "got one")]
        alts = ctx.targets(r)
        want_in, want_out = (says_in or not says_out), (says_out or not says_in)
        if alts and not _satisfies_any(ctx, alts, lambda dl, p: not ((want_in and (p.video_fade_in or p.audio.fade_in))
                                                                     or (want_out and (p.video_fade_out
                                                                                       or p.audio.fade_out)))):
            if any(dl.before.video_fade_in or dl.before.video_fade_out or dl.before.audio.fade_in
                   or dl.before.audio.fade_out for dl in ctx.d.clips) or ctx.d.categories & {"clip:fade", "clip:audio_fade"}:
                out.append(Violation("partial", r.text, f"{_names(ctx, alts[0])} still fades"))
    return out


_LOOK_LUT = (
    (r"\bteal\b|\bcinematic\b|\bblockbuster\b|\bmovie look\b|\bfilmic\b", "teal_orange.cube"),
    (r"\bwarm(?:er)?\b|\bgolden\b|\bsunset\b|\bcozy\b|\bcosy\b", "warm.cube"),
    (r"\bcool(?:er)?\b|\bcold\b|\bicy\b", "cool.cube"),
    (r"\bb\s*and\s*w\b|\bb&w\b|\bblack and white\b|\bblak and white\b|\bmono(?:chrome)?\b|\bgr[ae]y\s*scale\b", "mono.cube"),
)


def _rule_look(ctx: _Ctx) -> list[Violation]:
    out = []
    for r in ctx.c.reads_with("look"):
        t = r.t
        if _REMOVAL_RE.search(t):
            # polarity: "take the black and white off" must never ADD a look
            # (it put black and white on every clip)
            gained = [dl for dl in ctx.d.clips for p in dl.pieces
                      if set(lut_names(p)) - set(lut_names(dl.before))]
            if gained:
                out.append(Violation("direction", r.text, f"it asked to take a look off, but {_msg_clip(ctx, gained[0].id)} "
                                                          "got one"))
            continue
        lut = next((name for pat, name in _LOOK_LUT if re.search(pat, t)), None)
        if lut is None or _clause_is_timed(r) or set(r.scope.media) & {"captions", "text", "music", "overlay"}:
            continue
        alts = ctx.targets(r)
        if alts is None:
            continue

        def ok(dl: ClipDelta, p: Clip, lut=lut) -> bool:
            if lut in lut_names(p):
                return True
            return lut == "mono.cube" and color_params(p).get("saturation") == 0.0
        if not _satisfies_any(ctx, alts, ok):
            out.append(Violation("partial", r.text, f"{_names(ctx, alts[0])} did not get the "
                                 f"{lut.replace('.cube', '').replace('_', ' ')} look"))
    return out


_ADJ_AXES = ("brightness", "contrast", "saturation")


def _rule_adjust(ctx: _Ctx) -> list[Violation]:
    out = []
    for r in ctx.c.reads_with("adjust"):
        t = r.t
        if _clause_is_timed(r) or set(r.scope.media) & {"captions", "text", "music", "overlay"}:
            continue
        for axis in _ADJ_AXES:
            d = M.direction(t, axis)
            if d not in ("up", "down"):
                continue
            neutral = 0.0 if axis == "brightness" else 1.0
            alts = ctx.targets(r)
            if alts is None:
                continue

            def ok(dl: ClipDelta, p: Clip, axis=axis, d=d, neutral=neutral) -> bool:
                b = color_params(dl.before).get(axis, neutral)
                a = color_params(p).get(axis, neutral)
                if axis == "saturation" and d == "down" and "mono.cube" in lut_names(p):
                    return True
                return a > b + 1e-6 if d == "up" else a < b - 1e-6
            if not _satisfies_any(ctx, alts, ok):
                out.append(Violation("partial" if not ctx.d.changed_clips("look") else "direction", r.text,
                                     f"it asked for {'more' if d == 'up' else 'less'} {axis} on "
                                     f"{_names(ctx, alts[0])}, which is not what changed"))
    return out


def _rule_simple_flags(ctx: _Ctx) -> list[Violation]:
    """reverse, flip, rotate, zoom: the named clips carry it."""
    out = []
    for r in ctx.c.reads:
        t = r.t
        if _clause_is_timed(r) or set(r.scope.media) & {"captions", "text", "music", "overlay", "sticker"}:
            continue
        checks: list[tuple[str, Callable[[ClipDelta, Clip], bool]]] = []
        if "reverse" in families_of(t) and not re.search(r"\border\b", t):
            off = bool(re.search(r"\bun-?reverse|\bforwards?\b|\bnormal direction\b", t))
            checks.append(("play " + ("forwards" if off else "backwards"), lambda dl, p, off=off: bool(p.reverse) != off))
        if "flip" in families_of(t) and not re.search(r"\bback\b|\bunflip|\bun-flip", t):
            checks.append(("be mirrored", lambda dl, p: (p.transform.flip_h, p.transform.flip_v)
                           != (dl.before.transform.flip_h, dl.before.transform.flip_v)
                           or abs(_rot(p) - _rot(dl.before)) > 1e-6))
        if "rotate" in families_of(t) and "flip" not in families_of(t):
            deg = M.rotation_degrees(t)
            if deg is None:
                m = re.search(r"\bby\s+(-?\d{1,3})\b(?!\s*(?:%|x\b|s\b|sec))"
                              r"|\bturn\b.*?\b(-?(?:90|180|270|45))\b(?!\s*(?:%|x\b|s\b|sec))", t)
                deg = float(next(g for g in m.groups() if g)) if m else None
            # a sign or a way round ("-45", "minus 30", "counter-clockwise")
            # fixes the direction; only a bare angle may go either way
            signed = bool(re.search(r"(?<![\w.])-\d|\bminus\b|\bnegative\b|clockwise\b|\bto the (?:left|right)\b", t))
            if deg:
                checks.append((f"rotate {deg:g}°", lambda dl, p, deg=deg, signed=signed:
                               _same_angle(_rot(p) - _rot(dl.before), deg) or _same_angle(_rot(p), deg)
                               or (not signed and _same_angle(_rot(p), -deg))))
            elif re.search(r"\bupside down\b", t):
                checks.append(("turn upside down", lambda dl, p: abs(abs(_rot(p)) - 180) < 0.5 or p.transform.flip_v))
        if "zoom" in families_of(t) and not re.search(r"\banimation\b|\btransition\b|\bremove\b|\breset\b", t):
            zd = M.direction(t, "zoom")
            if zd == "up":
                checks.append(("zoom in", lambda dl, p: _scale_end(p) > _scale_end(dl.before) + 1e-6
                               or "zoom" in " ".join(str(x) for x in (p.anim_in, p.anim_combo))))
            elif zd == "down":
                checks.append(("zoom out", lambda dl, p: _zoomed_out(dl.before, p)))
        if not checks:
            continue
        alts = ctx.targets(r)
        if alts is None:
            continue
        for what, ok in checks:
            if not _satisfies_any(ctx, alts, ok):
                out.append(Violation("partial", r.text, f"{_names(ctx, alts[0])} did not {what}"))
    return out


def _same_angle(a: float, b: float) -> bool:
    return abs(((a - b) + 180.0) % 360.0 - 180.0) < 0.5


def _rot(c: Clip) -> float:
    v = c.transform.rotation
    return float(v.keyframes[-1][1]) if hasattr(v, "keyframes") else float(v)


def _scale_end(c: Clip) -> float:
    v = c.transform.scale
    return float(v.keyframes[-1][1]) if hasattr(v, "keyframes") else float(v)


def _zoomed_out(b: Clip, p: Clip) -> bool:
    v = p.transform.scale
    if hasattr(v, "keyframes"):
        return float(v.keyframes[-1][1]) < float(v.keyframes[0][1]) - 1e-6
    return float(v) < _scale_end(b) - 1e-6


# ---- structure: delete / keep / cut ranges / split --------------------------

def _timeline_to_source(before: EDL, a: float, b: float) -> dict[str, list[tuple[float, float]]] | None:
    """Source spans the BEFORE timeline plays over [a, b] (constant-speed,
    forward clips only; None when a curve / reverse / freeze makes it fuzzy)."""
    out: dict[str, list[tuple[float, float]]] = {}
    for c in media_v1(before):
        if c.freeze is not None or c.reverse or isinstance(c.speed, dict):
            return None
        s, e = c.start, c.start + c.effective_duration
        lo, hi = max(a, s), min(b, e)
        if hi - lo <= 1e-6:
            continue
        f = float(c.speed_factor)
        out.setdefault(str(c.src), []).append((c.in_ + (lo - s) * f, c.in_ + (hi - s) * f))
    return out


def _sub(cov: dict[str, list[tuple[float, float]]], rem: dict[str, list[tuple[float, float]]]
         ) -> dict[str, list[tuple[float, float]]]:
    from .contract_diff import _merge
    out: dict[str, list[tuple[float, float]]] = {}
    for k, spans in cov.items():
        cur = list(spans)
        for x, y in rem.get(k, []):
            nxt = []
            for p, q in cur:
                if q <= x or p >= y:
                    nxt.append((p, q))
                    continue
                if p < x:
                    nxt.append((p, x))
                if q > y:
                    nxt.append((y, q))
            cur = nxt
        out[k] = _merge([s for s in cur if s[1] - s[0] > 1e-6])
    return out


def _cov_equal(a: dict[str, list[tuple[float, float]]], b: dict[str, list[tuple[float, float]]],
               tol: float = 0.07) -> bool:
    keys = {k for k in a if a[k]} | {k for k in b if b[k]}
    for k in keys:
        x, y = a.get(k, []), b.get(k, [])
        if len(x) != len(y):
            return False
        for (p, q), (r, s) in zip(x, y):
            if abs(p - r) > tol or abs(q - s) > tol:
                return False
    return True


#: "remove the speed change / filter / transition / animation … (from) clip 2"
#: removes a FEATURE of the clip, never the clip.
_FEATURE_OBJECT_RE = re.compile(
    r"\b(?:speed|ramp|curve|filters?|luts?|looks?|effects?|transitions?|cross ?fades?|dissolves?|wipes?|animations?"
    r"|audio|sound|music|voice|text|titles?|captions?|subtitles?|blend|background|fades?|zoom|keyframes?|flip|mirror"
    r"|rotation|colou?r|grade|grading|vignett\w*|grain|glitch|freeze|reverse|mute|echo|robot|reverb|effect"
    r"|volume|level|gain|db|decibels?|loudness|brightness|contrast|saturation)\b"
    r"|\bfrom\s+(?:the\s+)?(?:\w+\s+)?(?:clip|shot)\b|\bbetween\b")


_PART_OF_RE = re.compile(r"\b(?:the|a|some)\s+(?:middle|centre|center|start|end|beginning|ending|part|bit|section|piece"
                         r"|chunk|rest|portion)\s+of\b")
_CUT_VERB_RE = re.compile(r"\b(?:cut|trim|delete|delet|delte|remove|chop|lose|drop|get rid of|shorten|cut out|crop out"
                          r"|strip|erase|ditch|scrap|kill|trash|take out|take off)\b")


def _rule_structure(ctx: _Ctx) -> list[Violation]:
    out: list[Violation] = []
    before_cov = coverage(ctx.before)
    after_cov = coverage(ctx.after)
    vend = ctx.before.video_extent()
    expected_removals: list[dict[str, list[tuple[float, float]]]] = []
    exact = True
    for r in ctx.c.reads:
        t = r.t
        if "cut" not in r.families or set(r.scope.media) & {"music", "captions", "text", "overlay", "vo"}:
            continue
        if re.search(r"\b(?:silence|pause|dead air|filler|umm?s?|uhs?|tighten|fit|seconds? long|shorten the video"
                     r"|make (?:it|the video|this) \d)", t):
            exact = False
            continue
        times = [x for x in M.time_refs(t) if x.kind != "at"]
        keep = M.keeps_only(t)
        if keep:
            if r.scope.refs:
                ids = [ctx.resolve(x) for x in r.scope.refs]
                if any(i is None for i in ids):
                    exact = False
                    continue
                want: dict[str, list[tuple[float, float]]] = {}
                for c in ctx.v1:
                    if c.id in ids:
                        want.setdefault(str(c.src), []).append((c.in_, c.out))
                from .contract_diff import _merge
                want = {k: _merge(v) for k, v in want.items()}
            elif times:
                spans = _time_spans(times, vend)
                if spans is None:
                    exact = False
                    continue
                want = {}
                for a, b in spans:
                    got = _timeline_to_source(ctx.before, a, b)
                    if got is None:
                        exact = False
                        break
                    for k, v in got.items():
                        want.setdefault(k, []).extend(v)
                from .contract_diff import _merge
                want = {k: _merge(v) for k, v in want.items()}
            else:
                exact = False
                continue
            if not _cov_equal(after_cov, want):
                out.append(Violation("partial", r.text, "what is left is not what was asked to keep "
                                     f"({_fmt_cov(want)} expected, {_fmt_cov(after_cov)} kept)"))
            return out
        if times and (_CUT_VERB_RE.search(t) or r.inherited) and len(r.scope.refs) == 1 \
                and all(x.kind in ("first", "last") for x in times):
            # "cut the first 2 seconds of clip 3": inside THAT clip's span
            cid = ctx.resolve(r.scope.refs[0])
            c = next((x for x in ctx.v1 if x.id == cid), None)
            if c is None:
                exact = False
                continue
            s0, e0 = c.start, c.start + c.effective_duration
            for x in times:
                if x.a >= e0 - s0 - 1e-6:
                    exact = False
                    continue
                span = (s0, s0 + x.a) if x.kind == "first" else (e0 - x.a, e0)
                got = _timeline_to_source(ctx.before, *span)
                if got is None:
                    exact = False
                else:
                    expected_removals.append(got)
            continue
        if times and _CUT_VERB_RE.search(t) or (times and r.inherited):
            for x in times:
                if x.kind in ("first", "last") and x.a >= vend - 1e-6:
                    if ctx.d.removed_src > 0.05:
                        out.append(Violation("range", r.text, f"the video is only {vend:g}s — the {x.kind} "
                                                              f"{x.a:g}s is all of it"))
                    exact = False
                    continue
                spans = _time_spans([x], vend)
                if spans is None:
                    exact = False
                    continue
                for a, b in spans:
                    if b > vend + 0.05 and x.kind == "range":
                        if ctx.d.removed_src > 0.05:
                            out.append(Violation("range", r.text, f"{b:g}s is past the end of the video ({vend:g}s)"))
                        exact = False
                        continue
                    got = _timeline_to_source(ctx.before, a, b)
                    if got is None:
                        exact = False
                    else:
                        expected_removals.append(got)
            continue
        if r.scope.refs and re.search(r"\b(?:delete|delet|delte|remove|get rid of|lose|drop|trash|erase|ditch|scrap"
                                      r"|kill|cut out|take out)\b", t) and not re.search(r"\b(?:from|off|of)\s+(?:the\s+)?"
                                                                                           r"(?:start|end|beginning)", t) \
                and not _FEATURE_OBJECT_RE.search(t):
            ids = [ctx.resolve(x) for x in r.scope.refs]
            if any(i is None for i in ids):
                exact = False
                continue
            if _PART_OF_RE.search(t):
                # "remove the middle of clip 2" deleted ALL of clip 2
                for cid in ids:
                    dl = ctx.delta(cid)
                    if dl is not None and dl.gone:
                        out.append(Violation("scope", r.text, f"it deleted all of {_msg_clip(ctx, cid)}, but the "
                                                              "request named a part of it"))
                exact = False
                continue
            for cid in ids:
                dl = ctx.delta(cid)
                if dl is not None and not dl.gone and dl.removed_src < dl.before.out - dl.before.in_ - 0.07:
                    out.append(Violation("partial", r.text, f"{_msg_clip(ctx, cid)} is still there"))
            for c in ctx.v1:
                if c.id not in ids:
                    got = {str(c.src): [(c.in_, c.out)]}
                    expected_removals.append({})
            want_rm: dict[str, list[tuple[float, float]]] = {}
            for c in ctx.v1:
                if c.id in ids:
                    want_rm.setdefault(str(c.src), []).append((c.in_, c.out))
            expected_removals.append(want_rm)
            continue
        if _CUT_VERB_RE.search(t) and not times and not r.scope.refs:
            exact = False
    if expected_removals and exact:
        rem: dict[str, list[tuple[float, float]]] = {}
        for x in expected_removals:
            for k, v in x.items():
                rem.setdefault(k, []).extend(v)
        want = _sub(before_cov, rem)
        # speed / duplicate edits in the same prompt make coverage fuzzy
        if not (ctx.d.categories & {"v1:duplicate", "v1:freeze", "clip:src"}) and not _cov_equal(after_cov, want):
            out.append(Violation("partial", ctx.c.prompt, f"the cuts are not what was asked ({_fmt_cov(want)} "
                                 f"should remain, {_fmt_cov(after_cov)} remains)"))
    # delete-count / keep-only guard: nothing removed that no clause names
    return out


def _time_spans(times: list[M.TimeRef], vend: float) -> list[tuple[float, float]] | None:
    out = []
    for x in times:
        if x.kind == "first":
            out.append((0.0, min(x.a, vend)))
        elif x.kind == "last":
            out.append((max(0.0, vend - x.a), vend))
        elif x.kind == "range" and x.b is not None:
            out.append((x.a, x.b))
        elif x.kind == "after":
            out.append((x.a, vend))
        elif x.kind == "before":
            out.append((0.0, x.a))
        else:
            return None
    return out


def _fmt_cov(cov: dict[str, list[tuple[float, float]]]) -> str:
    spans = [s for v in cov.values() for s in v]
    return " + ".join(f"{a:.2f}–{b:.2f}s" for a, b in spans) or "nothing"


# ---- captions and titles ----------------------------------------------------

_COLOUR_NAME_RE = re.compile(rf"\b({_COLOURS})\b|(#[0-9a-f]{{6}})\b")


def _rule_captions(ctx: _Ctx) -> list[Violation]:
    out = []
    cap_b, cap_a = ctx.before.get_track("captions"), ctx.after.get_track("captions")
    had = bool(cap_b and any(isinstance(c, TextClip) for c in cap_b.clips))
    if not had or cap_a is None:
        return out
    from .contract import _CAPTIONS_ADDED_RE, _CAPTIONS_REMOVED_RE
    said = M.norm(ctx.c.prompt)
    if _CAPTIONS_REMOVED_RE.search(said) and not _CAPTIONS_ADDED_RE.search(said) and not ctx.c.composite \
            and any(isinstance(c, TextClip) for c in cap_a.clips):
        # "remove the music and the captions" re-laid them instead
        return [Violation("partial", ctx.c.prompt, "the captions are still there")]
    for r in ctx.c.reads_with("captions"):
        t = r.t
        if re.search(r"\b(?:add|generate|create|put|burn|redo|regenerate)\s+(?:some\s+|the\s+|new\s+|in\s+)?"
                     r"(?:captions?|subtitles?|subs)\b|\bcaption (?:it|this)\b|\bre-?transcribe|\btranslate|\bremove\b"
                     r"|\bdelete\b|\bturn off\b|\bget rid\b|\bhide\b|\bclear\b"
                     r"|\bin (?:hindi|spanish|english|french|german|hinglish)\b", t):
            continue
        cfg_b, cfg_a = cap_b.config, cap_a.config
        lb, la = (cfg_b.look if cfg_b else None), (cfg_a.look if cfg_a else None)
        wants: list[tuple[str, bool]] = []
        if m := _COLOUR_NAME_RE.search(t):
            if not re.search(r"\b(?:box|background|outline|stroke|border)\b", t):
                wants.append((f"{m.group(0)} captions", bool(la and la.color and (not lb or la.color != lb.color))))
        sz = M.size_ask(t)
        size_b = (lb.size if lb and lb.size else None)
        size_a = (la.size if la and la.size else None)
        cue_b = _cue_size(cap_b)
        cue_a = _cue_size(cap_a)
        if sz.px is not None:
            wants.append((f"captions at {sz.px:g} px", bool(size_a and abs(size_a - sz.px) < 0.5)))
        elif sz.direction == "up":
            wants.append(("bigger captions", (size_a or cue_a or 0) > (size_b or cue_b or 0) + 1e-6))
        elif sz.direction == "down":
            wants.append(("smaller captions", 0 < (size_a or cue_a or 0) < (size_b or cue_b or 1e9) - 1e-6))
        if re.search(r"\bbold(?:er)?\b|\bheavier\b|\bthicker font\b", t):
            wants.append(("bold captions", _font_bold((la.font if la and la.font else None) or "Inter-Black")))
        pos = re.search(r"\b(top|bottom|middle|center|centre)\b", t)
        if pos:
            want = {"middle": "center", "centre": "center"}.get(pos.group(1), pos.group(1))
            wants.append((f"captions at the {want}", bool(cfg_a and cfg_a.position == want)))
        if re.search(r"\ball caps\b|\buppercase\b|\bupper case\b|\bcapitals\b", t):
            wants.append(("captions in capitals", bool(la and la.upper)))
        if re.search(r"\b(?:box|background|backdrop|highlight)\b", t) and not re.search(r"\bno\b|\bremove\b|\bwithout\b", t):
            wants.append(("a box behind the captions", bool(la and la.background)))
        for what, ok in wants:
            if not ok:
                out.append(Violation("partial", r.text, f"it asked for {what}, which did not happen"))
        if wants and "captions:add" in ctx.d.categories:
            out.append(Violation("unasked", r.text, "it laid the captions again instead of restyling them"))
    return out


def _cue_size(track) -> float | None:
    sizes = [float(c.style.size) for c in track.clips if isinstance(c, TextClip)]
    return sizes[0] if sizes else None


def _font_bold(font: str | None) -> bool:
    f = (font or "").lower()
    return any(k in f for k in ("black", "bold", "anton", "bebas", "heavy"))


def _named_text(ctx: _Ctx, r: ClauseRead, texts: list[TextClip]) -> list[TextClip] | None:
    """The texts a clause names: by its words ("the Summer Trip title",
    "the SALE text"), else the selected text, else the only one. None when
    it names none of several (the plan must ask)."""
    words = M.norm(r.text)
    if re.search(r"\b(?:all|every|each|both)\s+(?:of\s+)?(?:the\s+|my\s+)?(?:texts?|titles?|words)\b", words):
        return list(texts)                     # "make all the text red"
    named = [x for x in texts if x.text and x.text.strip() and M.norm(x.text.split("\n")[0])[:40] in words]
    if named:
        return named
    sel = [x for x in texts if x.id == ctx.c.selection]
    if sel:
        return sel
    return texts if len(texts) == 1 else None


def _rule_titles(ctx: _Ctx) -> list[Violation]:
    out = []
    before_t = [c for t in ctx.before.tracks if t.id != "captions" for c in t.clips if isinstance(c, TextClip)
                and c.role not in ("watermark", "caption")]
    if not before_t:
        return out
    after_map = {c.id: c for t in ctx.after.tracks if t.id != "captions" for c in t.clips if isinstance(c, TextClip)}
    for i, r in enumerate(ctx.c.reads):
        if "text" not in r.families or "captions" in r.families:
            continue
        t = r.t
        nxt = ctx.c.reads[i + 1].t if i + 1 < len(ctx.c.reads) else ""
        if _PLACE_IT_RE.search(nxt):
            t = f"{t} {nxt}"             # "make the title bigger and move it to the top"
        if _TEXT_ADD_RE.search(r.text) and re.search(r"\b(?:add|put|insert|write|create|new|another)\b|[\"“”']", r.text) \
                and not _TEXT_RETEXT_RE.search(r.text):
            continue
        if _EXISTING_TEXT_RE.search(t) and not re.search(r"\b(?:add|put|insert|write|create|new|another|second)\b"
                                                         r"|[\"“”']", r.text):
            # final sweep 2 r2: "make the title say X" / "start the title at
            # 0:02" ADDED a second title: a clause about THE title never adds one
            added = [x for x in ctx.after.tracks for x in x.clips if isinstance(x, TextClip)
                     and x.id not in {b.id for b in before_t} and x.role not in ("watermark", "caption")]
            if added:
                out.append(Violation("unasked", r.text, f"it added a new title ('{added[0].text[:30]}'), but the "
                                                        "request was about the title already there"))
                continue
        if _TEXT_REMOVE_RE.search(t) or _TEXT_RETEXT_RE.search(r.text):
            continue
        restyle = bool(_TEXT_RESTYLE_RE.search(t))
        retime = bool(_TEXT_RETIME_RE.search(t)) and not restyle
        if not (restyle or retime):
            continue
        targets = _named_text(ctx, r, before_t)
        changed_ids = {x[0].id for x in ctx.d.texts.get("restyled", []) + ctx.d.texts.get("retimed", [])
                       + ctx.d.texts.get("moved", [])}
        if targets is None:
            if changed_ids:
                out.append(Violation("scope", r.text, f"there are {len(before_t)} titles and the request did not say "
                                                      "which one"))
            continue
        tid = {x.id for x in targets}
        for cid in changed_ids - tid:
            out.append(Violation("scope", r.text, "it changed a title the request did not name"))
        for x in targets:
            a = after_map.get(x.id)
            if a is None:
                out.append(Violation("unasked", r.text, f"the title '{x.text[:30]}' was removed"))
                continue
            if a.text != x.text:
                out.append(Violation("unasked", r.text, f"the title's words changed to '{a.text[:30]}'"))
            if restyle:
                if (m := _COLOUR_NAME_RE.search(t)) and not re.search(r"\b(?:box|background|outline|stroke|border|shadow)\b", t):
                    if a.style.color.upper() == x.style.color.upper() and m.group(0) not in ("white",):
                        out.append(Violation("partial", r.text, f"the title is not {m.group(0)}"))
                sz = M.size_ask(t)
                if sz.px is not None and abs(a.style.size - sz.px) > 0.5:
                    out.append(Violation("partial", r.text, f"the title is not {sz.px:g} px"))
                elif sz.direction == "up" and not a.style.size > x.style.size:
                    out.append(Violation("partial", r.text, "the title did not get bigger"))
                elif sz.direction == "down" and not a.style.size < x.style.size:
                    out.append(Violation("partial", r.text, "the title did not get smaller"))
                if re.search(r"\bbold", t) and not _font_bold(a.style.font) and _font_bold(x.style.font) is False:
                    out.append(Violation("partial", r.text, "the title is not bold"))
                pos = re.search(r"\b(top|bottom|middle|cent(?:er|re))\b", t)
                if pos and not re.search(r"\b(?:box|background|outline|stroke|border)\b", t):
                    h = float(ctx.after.canvas.h or 1080)
                    ya, yb = float(a.transform.y), float(x.transform.y)
                    where = pos.group(1)
                    ok = (ya < yb - 1 or ya <= h * 0.3) if where == "top" else \
                        (ya > yb + 1 or ya >= h * 0.7) if where == "bottom" else abs(ya - h / 2) <= h * 0.15
                    if not ok:
                        out.append(Violation("partial", r.text, f"the title is not at the {where}"))
            if retime:
                sh = re.search(r"(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)?\s+(later|earlier|sooner)\b", t)
                if sh:
                    d = float(sh.group(1)) * (1 if sh.group(2) == "later" else -1)
                    if abs((a.start - x.start) - d) > 0.05 or abs((a.end - x.end) - d) > 0.05:
                        out.append(Violation("partial", r.text, f"the title did not move {abs(d):g}s "
                                             f"{sh.group(2)} (it is on screen {a.start:g}–{a.end:g}s)"))
                for tr in M.time_refs(t):
                    if tr.kind == "range" and tr.b is not None and (abs(a.start - tr.a) > 0.05 or abs(a.end - tr.b) > 0.05):
                        out.append(Violation("partial", r.text, f"the title is not on screen {tr.a:g}–{tr.b:g}s"))
                ln = M.direction(t, "length")
                was, now = x.end - x.start, a.end - a.start
                if ln == "up" and not now > was + 1e-3:
                    # "make the title last 2 seconds longer" took 0-3 s to 0-2 s
                    out.append(Violation("direction", r.text, f"it asked for longer, but the title went from "
                                                              f"{was:g}s to {now:g}s on screen"))
                elif ln == "down" and not now < was - 1e-3:
                    out.append(Violation("direction", r.text, f"it asked for shorter, but the title went from "
                                                              f"{was:g}s to {now:g}s on screen"))
    return out


#: THE title the user already has ("the title", "my text"), not a new one.
_EXISTING_TEXT_RE = re.compile(r"\b(?:the|my|this|that)\s+(?:title|text|heading|headline)\b")


_PLACE_IT_RE = re.compile(r"^(?:and\s+|then\s+)*(?:move|put|place|position|shift|bring|stick|set)\s+(?:it|them)\b")


def _rule_transitions(ctx: _Ctx) -> list[Violation]:
    out = []
    from . import slots as S
    tr_a = ctx.after.get_track("v1").transitions if ctx.after.get_track("v1") else []
    for r in ctx.c.reads_with("transition"):
        t = r.t
        if re.search(r"\b(?:remove|delete|turn off|get rid of|take out|no)\b", t):
            continue
        nth = _TR_ORD_RE.search(t)
        if nth:
            # final sweep 2 r2: "make the second transition 1 second" set both
            out.extend(_one_transition(ctx, r, tr_a, _TR_ORD[nth.group(1)]))
            continue
        kind = S.transition_type_of(t) or S.transition_type_of(re.sub(r"\b(\w+?)e?s\b", r"\1", t))
        m = re.search(r"\b(wipe|dissolve|glitch|whip|slide|zoom|flash|cross ?fade|fade)s?\b", t)
        if m:
            kind = {"crossfade": "fade", "cross fade": "fade"}.get(m.group(1), m.group(1))
        if not kind:
            out.extend(_transition_lengths(ctx, r, tr_a))
            continue
        dm = re.search(r"(?<!\bat\s)\b(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)\b(?!\s+(?:in|into|from))", t)
        if dm and ctx.d.categories & {"transitions:type", "transitions:duration", "transitions:add"}:
            # "fade transitions everywhere, 1 sec" committed 0.5 s fades
            want = min(2.0, max(0.1, float(dm.group(1))))
            tv = ctx.before.get_track("v1")
            old_ids = {(round(x.at, 2), x.type, round(float(x.duration or 0), 2)) for x in (tv.transitions if tv else [])}
            new_ones = [x for x in tr_a if (round(x.at, 2), x.type, round(float(x.duration or 0), 2)) not in old_ids]
            short = [x for x in new_ones if abs(float(x.duration or 0) - want) > 0.05
                     and not _clamped_by_neighbour(ctx, x, want)]
            if short:
                out.append(Violation("partial", r.text, f"the transition at {short[0].at:g}s is "
                                                        f"{float(short[0].duration or 0):g}s, not {want:g}s"))
        change = re.search(r"\b(?:change|switch|turn|convert|replace|swap|make)\b.*\b(?:to|into|with|for)\b", t) \
            and re.search(r"\ball\b|\bevery\b|\btransitions\b", t)
        if change and tr_a:
            fam = re.sub(r"(left|right|up|down|in|out|open|close)$", "", kind)
            bad = [x for x in tr_a if fam not in x.type]
            if bad:
                out.append(Violation("partial", r.text, f"not every transition is a {fam} "
                                     f"({', '.join(sorted({x.type for x in bad}))} left)"))
    return out


_TR_ORD_RE = re.compile(r"\b(first|second|third|fourth|fifth|last|final|1st|2nd|3rd|4th|5th)\s+"
                        r"(?:transition|crossfade|cross fade|dissolve|wipe|fade)\b(?!s)")
_TR_ORD = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4, "fifth": 5,
           "5th": 5, "last": -1, "final": -1}


def _clamped_by_neighbour(ctx: _Ctx, tr, want: float) -> bool:
    """A transition cannot be longer than half its shorter neighbour; a
    shorter-than-asked one at such a seam is the clamp, not a misreading."""
    clips = sorted((c for c in ctx.after.get_track("v1").clips if isinstance(c, Clip)), key=lambda c: c.start)
    near = [c.effective_duration for c in clips if abs(c.start - tr.at) < 0.05 or abs(c.start + c.effective_duration
                                                                                         - tr.at) < 0.05]
    return bool(near) and float(tr.duration or 0) < want and float(tr.duration or 0) >= min(near) / 2 - 0.1


def _one_transition(ctx: _Ctx, r: ClauseRead, tr_a: list, n: int) -> list[Violation]:
    """"the second transition …" may change only that seam; a type change
    keeps its length unless one is said."""
    tv = ctx.before.get_track("v1")
    tr_b = sorted(tv.transitions if tv else [], key=lambda x: x.at)
    if not tr_b:
        return []
    k = n - 1 if n > 0 else len(tr_b) + n
    out = []
    for i, b in enumerate(tr_b):
        a = next((x for x in tr_a if abs(x.at - b.at) <= 0.05), None)
        same = a is not None and a.type == b.type and abs(float(a.duration or 0) - float(b.duration or 0)) <= 0.05
        if i != k and not same:
            out.append(Violation("scope", r.text, f"it changed the transition at {b.at:g}s too — the request named "
                                                  f"only one"))
        elif i == k and a is not None and a.type != b.type and not re.search(
                r"\b\d+(?:\.\d+)?\s*(?:s|sec|secs|seconds?)\b|\blonger\b|\bshorter\b", r.t) \
                and abs(float(a.duration or 0) - float(b.duration or 0)) > 0.05:
            out.append(Violation("unasked", r.text, f"it changed the transition's length ({float(b.duration or 0):g}s → "
                                                    f"{float(a.duration or 0):g}s) — only its type was asked"))
        elif i == k and (dm := re.search(r"\b(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)\b", r.t)) and a is not None \
                and abs(float(a.duration or 0) - min(2.0, max(0.1, float(dm.group(1))))) > 0.05 \
                and not _clamped_by_neighbour(ctx, a, float(dm.group(1))):
            out.append(Violation("partial", r.text, f"the transition at {b.at:g}s is {float(a.duration or 0):g}s, "
                                                    f"not {float(dm.group(1)):g}s"))
    return out[:2]


def _transition_lengths(ctx: _Ctx, r: ClauseRead, tr_a: list) -> list[Violation]:
    """"make all transitions 1 second long": every transition that was there
    keeps its TYPE and is that long (they became 0.4 s dissolves)."""
    dm = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)\b", r.t)
    tv = ctx.before.get_track("v1")
    tr_b = list(tv.transitions) if tv else []
    if not dm or not tr_b or not (ctx.d.categories & {"transitions:type", "transitions:duration"}):
        return []
    want = min(2.0, max(0.1, float(dm.group(1))))
    out = []
    for b in tr_b:
        a = next((x for x in tr_a if abs(x.at - b.at) <= 0.05), None)
        if a is None:
            continue
        if a.type != b.type:
            out.append(Violation("unasked", r.text, f"the {b.type} transition became {a.type} — only its length was "
                                                    "asked"))
        elif abs(float(a.duration or 0) - want) > 0.05:
            out.append(Violation("partial", r.text, f"the transition at {b.at:g}s is {float(a.duration or 0):g}s, "
                                                    f"not {want:g}s"))
    return out[:2]


_DELETE_VERB_RE = re.compile(r"\b(?:delete|delet|delte|remove|get rid of|lose|drop|trash|erase|ditch|scrap|kill|cut out"
                             r"|take out)\b")
_CLIPPISH_RE = re.compile(r"\b(?:clips?|shots?|scenes?|segments?|half|part|piece|bit|section|it|this|that|one)\b")


def _rule_text_inside(ctx: _Ctx) -> list[Violation]:
    """A title ADDED by this run must end inside the picture: "trim the last
    2 seconds and add a title at the end" placed it on the timeline as it
    was BEFORE the trim, 2 s past the new end."""
    vend = ctx.after.video_extent()
    out = []
    for t in ctx.d.texts.get("added", []):
        if vend > 0 and t.end > vend + 0.05:
            out.append(Violation("range", ctx.c.prompt, f"the new title '{t.text[:24]}' runs to {t.end:g}s, past the "
                                                        f"end of the video ({vend:g}s)"))
    return out


_QUOTED_RE = re.compile(r"(?:(?<=\s)|^)[\"“'‘]([^\"“”'‘’]{1,200})[\"”'’](?=\s|$|[,.!?;:])")
_IT_LOOK_RE = re.compile(r"^(?:and\s+|then\s+)?(?:make|turn|set)\s+(?:it|them)\b")
_ADD_VERB_RE = re.compile(r"\b(?:add|put|insert|write|create|place|type|new|another)\b")


def _rule_text_added(ctx: _Ctx) -> list[Violation]:
    """final sweep 2 r2: "add a caption 'Welcome' at 2 seconds" laid the
    transcript captions (no 'Welcome' anywhere), "add a title 'Intro' and make
    it red" added a WHITE title. A quoted line on an add clause must be in the
    text it adds, and a colour said for it must be its colour."""
    out: list[Violation] = []
    added = list(ctx.d.texts.get("added", []))
    for i, r in enumerate(ctx.c.reads):
        if not _ADD_VERB_RE.search(r.text) or _TEXT_RETEXT_RE.search(r.text) or "captions" in r.families \
                and not _QUOTED_RE.search(r.text):
            continue
        lits = [q.strip().lower() for q in _QUOTED_RE.findall(r.text) if q.strip()]
        caps_changed = bool(ctx.d.categories & {"captions:add", "captions:text"})
        if lits and (added or caps_changed):
            if not any(all(q in (t.text or "").lower() for q in lits) for t in added):
                out.append(Violation("partial", r.text, f"the new text does not say '{lits[0][:30]}'"))
                continue
        if not added or not lits:
            continue
        nxt = ctx.c.reads[i + 1].t if i + 1 < len(ctx.c.reads) else ""
        said = M.strip_quotes(r.t) + (" " + nxt if _IT_LOOK_RE.match(nxt) else "")
        m = _COLOUR_NAME_RE.search(said)
        if m and m.group(0) != "white" and not re.search(r"\b(?:box|background|outline|stroke|border|shadow)\b", said):
            mine = [t for t in added if all(q in (t.text or "").lower() for q in lits)]
            if mine and all((t.style.color or "").upper() == "#FFFFFF" for t in mine):
                out.append(Violation("partial", r.text, f"the new title is not {m.group(0)}"))
    return out


def _rule_freeze(ctx: _Ctx) -> list[Violation]:
    """"freeze at / on 6 seconds": the new still starts there ("freeze on 6
    seconds" once froze the playhead for 6 s)."""
    out = []
    starts = [round(c.start, 3) for c in ctx.d.new_clips if c.freeze is not None]
    starts += [round(p.start, 3) for dl in ctx.d.clips for p in dl.pieces if p.freeze is not None
               and dl.before.freeze is None]
    for r in ctx.c.reads_with("freeze"):
        m = re.search(r"\b(?:at|on|@)\s+(?:(\d{1,2}):(\d{2}(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)?)\b",
                      r.t)
        if not m or not starts:
            continue
        t = int(m.group(1)) * 60 + float(m.group(2)) if m.group(1) else float(m.group(3))
        if not any(abs(s - t) <= 0.05 for s in starts):
            out.append(Violation("partial", r.text, f"the freeze is at {starts[0]:g}s, not at {t:g}s"))
        held = [float(c.freeze) for c in ctx.d.new_clips if c.freeze is not None]
        held += [float(p.freeze) for dl in ctx.d.clips for p in dl.pieces if p.freeze is not None
                 and dl.before.freeze is None]
        fm = re.search(r"\bfor\s+(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)\b", r.t)
        want = float(fm.group(1)) if fm else (M.fraction_seconds(r.t) if re.search(r"\bfor\b", r.t) else None)
        if held and want is not None and not any(abs(h - want) <= 0.05 for h in held):
            out.append(Violation("partial", r.text, f"the freeze holds {held[0]:g}s, not {want:g}s"))
        elif held and want is None and re.search(r"\bfor\b", r.t) is None and \
                any(abs(h - t) <= 1e-6 and t > 3.0 for h in held):
            out.append(Violation("direction", r.text, f"it read {t:g}s as how long to hold the frame"))
    return out


def _rule_delete_happened(ctx: _Ctx) -> list[Violation]:
    """A clause that deletes a CLIP-ish thing ("delete the second half",
    "remove that bit") must remove footage — a plan that only split, or did
    nothing for it, is partial."""
    out = []
    for r in ctx.c.reads:
        t = r.t
        if not _DELETE_VERB_RE.search(t) or _FEATURE_OBJECT_RE.search(t) or not _CLIPPISH_RE.search(t):
            continue
        if set(r.scope.media) - {"voice"}:
            continue
        if not (ctx.d.categories & {"v1:cut", "v1:delete", "clip:cut"}):
            out.append(Violation("partial", r.text, "nothing was removed"))
    return out


_COPIES_RE = re.compile(r"\b(twice|thrice)\b|\b(\d{1,2}|two|three|four|five|six|seven|eight|nine|ten)\s+"
                        r"(?:times|copies|duplicates)\b")


def _rule_copies(ctx: _Ctx) -> list[Violation]:
    """"duplicate clip 1 three times" made ONE copy: the count is the count."""
    out = []
    if "v1:duplicate" not in ctx.d.categories or ctx.d.categories & {"v1:cut", "v1:delete", "v1:freeze"}:
        return out
    for r in ctx.c.reads_with("duplicate"):
        m = _COPIES_RE.search(r.t)
        if not m:
            continue
        w = m.group(1) or m.group(2)
        n = int(w) if w.isdigit() else {"twice": 2, "thrice": 3, "two": 2, "three": 3, "four": 4, "five": 5,
                                       "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}[w]
        added = len(media_v1(ctx.after)) - len(ctx.v1)
        if added != n:
            out.append(Violation("partial", r.text, f"it made {added} cop{'y' if added == 1 else 'ies'}, not {n}"))
    return out


def _rule_emptied(ctx: _Ctx) -> list[Violation]:
    """A run that leaves the main track EMPTY needs words that ask for all of
    it ("remove 15 seconds from the end" on a 12 s video cut every clip and
    kept the music over a black picture)."""
    if ctx.c.composite or not ctx.v1 or media_v1(ctx.after):
        return []
    if re.search(r"\b(?:everything|all|whole|entire|every)\b", M.norm(ctx.c.prompt)):
        return []
    return [Violation("range", ctx.c.prompt, f"it removed the whole video ({ctx.before.video_extent():g}s), "
                                             "which the request did not ask for")]


RULES: tuple[Callable[[_Ctx], list[Violation]], ...] = (
    _rule_emptied, _rule_copies, _rule_delete_happened, _rule_text_inside, _rule_text_added, _rule_freeze,
    _rule_licensed, _rule_scope, _rule_speed, _rule_zoom_not_speed, _rule_music_words, _rule_gap_words, _rule_mute, _rule_level, _rule_look, _rule_fade_off, _rule_adjust,
    _rule_simple_flags, _rule_structure, _rule_captions, _rule_titles, _rule_transitions,
)



__all__ = ["RULES"]
