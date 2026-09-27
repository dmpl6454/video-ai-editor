"""V2 picture-in-picture overlay path.

V1 is the base layer (concatenated full-screen). Each clip on V2 (or any
non-V1 video track) is overlaid on top with its transform (scale, x, y,
rotation, opacity) applied, and only visible during its RENDER window —
`render/clock.py` maps the clip's layout `[start, start+duration)` past the
v1 cross-fades before it, so the picture and the v1 frame it sits on agree
(a PIP authored at layout 30 s on a timeline with 2 s of transitions before
it plays at 28 s, together with the v1 frame authored at 30 s).

Audio from V2 clips also gets mixed into the final audio output so PiP
clips with sound (talking-head over screen recording, etc.) play correctly.
"""
from __future__ import annotations
import math
from pathlib import Path
from typing import NamedTuple
from ..edl import EDL
from ..edl.schema import Clip
from ..edl.keyframes import frame_exact_expr, is_keyframed
from .effects import build_chromakey_filter
from .sar import square_pixels_filter
from .text_overlay import enable_expr
from . import clock
from ..edl import timebase as _tb


def collect_pip_clips(edl: EDL) -> list[tuple[str, Clip]]:
    """Return [(track_id, clip), ...] for every clip on a non-V1 video track."""
    out: list[tuple[str, Clip]] = []
    for t in edl.tracks:
        if t.type != "video" or t.id == "v1" or t.muted:
            continue
        for c in t.clips:
            if isinstance(c, Clip):
                out.append((t.id, c))
    out.sort(key=lambda p: p[1].start)
    return out


# Shapes a PIP can be cut to. Written in the stream's OWN W/H so one expression
# fits any scaled size, and with every comma escaped for the filtergraph parser
# (a bare comma there ends the filter). `rectangle` is the natural shape of the
# frame, so it is deliberately absent — no mask is cheaper than a full-white one.
#
# Only shapes that are actually implemented appear here. `Mask.type` also allows
# linear/mirror/heart/star, which effects.render_mask_png either handles for v1
# only (linear) or silently renders as "fully visible" (mirror/heart/star) — a
# pre-existing no-op. Returning None for those keeps the PIP a plain rectangle
# rather than inventing a shape that the v1 path would not produce.
_PIP_SHAPES: dict[str, str] = {
    # Inscribed ellipse — which is a true CIRCLE because choosing this shape
    # also forces the element's box square (see the framing block below).
    # Normalising to half-width/half-height rather than hardcoding a radius
    # keeps it correct if that box is ever allowed to be non-square again,
    # and makes it degrade to an ellipse instead of clipping to a rectangle.
    "circle":
        "if(lte(((X-W/2)/(W/2))*((X-W/2)/(W/2))"
        "+((Y-H/2)/(H/2))*((Y-H/2)/(H/2))\\,1)\\,255\\,0)",
    # Rounded rectangle: distance outside the straight edges, cornered by a
    # radius of 12% of the shorter side.
    "rounded":
        "if(lte("
        "(max(0\\,abs(X-W/2)-(W/2-0.12*min(W\\,H))))*(max(0\\,abs(X-W/2)-(W/2-0.12*min(W\\,H))))"
        "+(max(0\\,abs(Y-H/2)-(H/2-0.12*min(W\\,H))))*(max(0\\,abs(Y-H/2)-(H/2-0.12*min(W\\,H))))"
        "\\,(0.12*min(W\\,H))*(0.12*min(W\\,H)))\\,255\\,0)",
}


def _shape_alpha_expr(mask) -> str | None:
    """geq alpha expression for a PIP's shape mask, or None to leave it square."""
    if mask is None:
        return None
    mtype = str(getattr(mask, "type", "") or "")
    expr = _PIP_SHAPES.get(mtype)
    if not expr:
        return None
    # `invert` is honoured because the schema offers it and a "hole" PIP is a
    # legitimate look; feather is NOT — geq is a hard per-pixel test, and a
    # soft edge would need a distance ramp per shape. Squaring that away
    # silently would make the Feather control another dead knob.
    if getattr(mask, "invert", False):
        return f"255-({expr})"
    return expr


def _scalar_or_last(v, default: float = 0.0) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    if v is None:
        return default
    if isinstance(v, dict):
        kfs = v.get("keyframes") or []
    else:
        kfs = getattr(v, "keyframes", []) or []
    if not kfs:
        return default
    return float(sorted(kfs, key=lambda p: p[0])[-1][1])


def _max_key(v, default: float = 1.0) -> float:
    """Largest value a keyframed property takes (keys only — interpolation
    between two keys never exceeds them, for every interp this app emits
    except `back-out`'s cubic, which is monotone on [0, 1] too)."""
    kfs = (v.get("keyframes") if isinstance(v, dict) else getattr(v, "keyframes", None)) or []
    vals = [float(p[1]) for p in kfs]
    return max(vals) if vals else default


#: ffmpeg inputs pip.py adds PER PIP: the picture input (`-itsoffset` to its
#: render start) followed by a separate AUDIO input of the same trimmed span
#: with no offset. One input cannot serve both: `-t` is compared against the
#: `-itsoffset`-shifted timestamps for audio but the unshifted ones for video
#: (measured on ffmpeg 8.1: `-ss 0 -t 0.5 -itsoffset 1.0` decodes 15 video
#: frames and ZERO audio samples; `-t 1.5` yields 0.5 s of audio), so every
#: PIP at start > 0 had been silent since the picture gained its offset. The
#: audio input is positioned by `adelay` alone, like music and voiceover.
INPUTS_PER_PIP = 2


def pip_audio_input_index(first_input_index: int, j: int) -> int:
    """ffmpeg input index of the j-th kept PIP's AUDIO input, given the index
    of the first PIP input — the one statement of the layout above, shared
    with compositor.py's audio fold."""
    return first_input_index + INPUTS_PER_PIP * j + 1


# ---- Frame-exact PIP timing (QA-002, the PIP half) ---------------------------
#
# v1 cuts by frame count since wave A (compositor's "Frame-exact clip timing"
# block); PIPs were still trimmed with float `-ss/-t %.3f` and placed with a
# float `-itsoffset`, so a PIP whose start sat off the frame grid (any legacy
# EDL) showed black on its first frame and every later frame one frame late,
# and on a 29.97 project the `%.3f` span let one extra source frame through at
# the tail. A PIP now follows the v1 recipe exactly: seek half a frame early,
# rebase, resample onto the grid, clone-pad and cut to an exact frame count,
# and only then shift onto the timeline at its SNAPPED start — so the frame
# count and the placement no longer depend on float formatting. Its sound
# drops the same pre-roll and is cut to the same number of frames' samples.

#: Frames of decode slack past a PIP's last frame (as v1's `-to` slack).
_DECODE_SLACK_FRAMES = 2


def pip_frames(rs: float, re: float, fps) -> tuple[int, int]:
    """(first output frame, frame count) of a PIP on screen for `[rs, re)` —
    the same frames its `enable` gate (`timebase.enable_window`) admits."""
    f0 = _tb.frame_of(rs, fps)
    return f0, max(1, _tb.frame_of(re, fps) - f0)


def pip_input_args(c: Clip, n: int, fps) -> list[str]:
    """`-ss/-t/-i` for a PIP showing `n` frames: seek half a frame before
    `in_` (timebase.seek_preroll), decode `n` frames plus slack.

    No `-itsoffset`, deliberately (QA-002): with an input offset ffmpeg 8.1
    stops seeking accurately — it keeps every frame from the keyframe BEFORE
    `-ss` whose shifted timestamp is still >= 0 — and `-t` is then counted
    from that keyframe, so a PIP trimmed far from a keyframe (every real
    camera file, GOPs of several seconds) lost that much of its TAIL, frozen
    on its last decoded frame. Measured: `-ss 0.983 -t 3 -itsoffset 2.5`
    decodes frames 0-89, not 30-119. Placement moved into the graph
    (`pip_video_timing`), where it is exact.

    A RETIMED PIP (a speed, a curve, a freeze — wave D3, E2) is opened
    exactly as v1 opens the same clip (`compositor.clip_input_args`: the
    whole source range, a curve's in-anchored seek, a freeze's one frame),
    because its chain is v1's (`pip_retime`) and the frames it picks depend
    on what was decoded. Safe now that no input carries `-itsoffset`."""
    if is_retimed(c):
        from .compositor import clip_input_args
        return clip_input_args(c, fps)
    pre = _tb.seek_preroll(c.in_, fps)
    seek = max(0.0, float(c.in_) - pre)
    span = pre + _tb.time_of(n + _DECODE_SLACK_FRAMES, fps)
    # No `-ss` from the file's start (audio_mix.input_seek: `-ss 0` garbles an
    # AAC source's first 21 ms — the PIP's sound is read from this input).
    from .audio_mix import input_seek
    return [*input_seek(seek), "-t", f"{span:.6f}", "-i", str(c.src)]


def is_retimed(c: Clip) -> bool:
    """A PIP whose picture is not its source at 1x: a freeze, a constant
    speed != 1 or a speed curve. (A reverse is substituted by
    `render.reverse.with_reversed_sources` before any of this runs — the
    intermediate plays forwards, retimed or not.)"""
    if getattr(c, "freeze", None) is not None:
        return True
    sp = c.speed
    if isinstance(sp, (int, float)):
        return bool(sp) and sp > 0 and float(sp) != 1.0
    from ..edl import speed_curve as _sc
    return _sc.curve_points(sp) is not None


class PipRetime(NamedTuple):
    """The pieces of v1's clip chain that decide WHICH frames a clip shows
    (compositor._build_clip_video_chain), in the order they run there:
    `head` (the clock the chain starts on), `retime` (the speed stage) and
    `anchor` (an option of the project-grid `fps=` that follows)."""
    head: str
    retime: str
    anchor: str


_REBASE = "setpts=PTS-STARTPTS,"


def pip_retime(c: Clip, fps) -> PipRetime:
    """v1's retime for clip `c` (wave D3, E2), so a PIP's project-grid `fps=`
    picks the SAME source frames v1 would — which `frame_map.clip_frame_list`
    models and tests/test_b5_pip_frame_exact.py decodes:

    * 1x       — the rebase only (a plain PIP's graph is byte-identical);
    * speed s  — rebase, `setpts=PTS/s`;
    * a curve  — the FILE clock anchored at `in` (`speed_curve.
      file_clock_expr` of the in-anchored seek), `settb` to whole-tick
      frames, the anchored closed-form integral, and `fps=…:start_time=0`
      (edl/speed_curve.py, "the v1 chain's clock");
    * a freeze — the first frame on the grid, one frame long, which the
      clone-pad that follows holds for the whole window
      (`frame_map.freeze_frame`).

    Mirrors the chain's retime branch rather than sharing it only because
    that branch is interleaved with v1's geometry; the frame-exact PIP tests
    fail the moment the two disagree."""
    if getattr(c, "freeze", None) is not None:
        return PipRetime(_REBASE, f"fps={_tb.ffmpeg_rate(fps)},trim=end_frame=1,setpts=PTS-STARTPTS,", "")
    sp = c.speed
    if isinstance(sp, (int, float)) and sp and sp > 0 and float(sp) != 1.0:
        return PipRetime(_REBASE, f"setpts=PTS/{float(sp)},", "")
    from .compositor import _v1_curve_map
    cm = _v1_curve_map(c)
    if cm is None:
        return PipRetime(_REBASE, "", "")
    from ..edl.speed_curve import anchored_setpts_expr, curve_seek, curve_settb_expr, file_clock_expr
    fc = file_clock_expr(curve_seek(c.in_))
    return PipRetime(f"setpts={fc}," if fc else "",
                     f"settb={curve_settb_expr(_tb.rate_of(fps).numerator)},"
                     f"setpts={anchored_setpts_expr(cm, float(c.in_))},",
                     ":start_time=0")


def pip_layout_end(c: Clip) -> float:
    """Layout end of a PIP: `start` plus its TIMELINE footprint
    (`effective_duration` — a 2x PIP fills half its source length, a curve
    its integral, a freeze its hold). The one window rule the picture and
    both audio folds in compositor.py share."""
    return float(c.start) + float(c.effective_duration)


def pip_video_timing(n: int, first_frame: int, fps, retime: PipRetime | None = None) -> str:
    """Filters (no labels, trailing comma) that make a PIP's picture exactly
    `n` frames on the project grid, starting at timeline frame `first_frame`:
    the v1 recipe (rebase, `fps=`, clone-pad, `trim=end_frame`), then a shift
    to the snapped start. This shift is what places a PIP in TIME — without it
    the PIP's frames would enter at t=0 and be over before its enable window
    opened (the "black box" bug, see build_pip_overlay_chain).

    The shift is in whole TICKS: after `fps=` the time base is exactly one
    frame, so `+first_frame` is exact. A seconds expression is not — setpts
    TRUNCATES, and 1.001/(1001/30000) evaluates to 29.999999999999996, which
    put every PIP on a 29.97 project one frame early.

    `retime` (`pip_retime`) replaces the rebase with v1's clock and puts v1's
    speed stage before `fps=`: the grid then picks the same frames."""
    rt = retime or PipRetime(_REBASE, "", "")
    return (f"{rt.head}{rt.retime}fps={_tb.ffmpeg_rate(fps)}{rt.anchor},"
            f"tpad=stop={n}:stop_mode=clone,trim=end_frame={n},"
            f"setpts=PTS-STARTPTS+{int(first_frame)},")


def pip_audio_chain(c: Clip, input_label: str, label_out: str, *, rs: float,
                    re: float, fps) -> str:
    """A PIP's sound, sample-exact to its picture: resampled, the seek
    pre-roll dropped, retimed, gain/fade/mute applied, cut to exactly its
    frames' samples and delayed (in samples) to its snapped start. A source
    with no audio stream contributes silence of that length instead of
    failing the graph (the v1 rule, QA-040).

    Retimed by v1's rules, because the picture now is (`pip_retime`): a
    constant speed through `audio_mix.speed_filters` (varispeed, or atempo
    centred by its lag), a curve from its cached intermediate
    (render/speed_audio.py, built by `speed_audio.prepare`, read from
    clip-local 0 with no pre-roll), a freeze is digital silence. A reverse
    was substituted upstream (its intermediate's sound already runs
    backwards)."""
    from .compositor import _audio_props_filters, source_has_audio
    from . import speed_audio as _speed_audio
    f0, n = pip_frames(rs, re, fps)
    m = _tb.samples_for_frames(n, fps)
    delay = _tb.samples_for_frames(f0, fps)
    tail = f",adelay=delays={delay}S:all=1" if delay > 0 else ""
    if getattr(c, "freeze", None) is not None:
        # A FREEZE is a still: silence of its exact length (v1's rule).
        return ("anullsrc=channel_layout=stereo:sample_rate=48000,"
                "aformat=channel_layouts=stereo:sample_rates=48000"
                f",atrim=end_sample={m}{tail}{label_out}")
    curve_src = _speed_audio.chain_source(c, fps) if _speed_audio.has_curve(c) else None
    if curve_src is not None:
        chain = (f"{curve_src}aresample=async=1:first_pts=0,"
                 f"aformat=channel_layouts=stereo:sample_rates=48000")
    else:
        if not source_has_audio(str(c.src)):
            input_label = "anullsrc=channel_layout=stereo:sample_rate=48000,"
        chain = (f"{input_label}aresample=async=1:first_pts=0,"
                 f"aformat=channel_layouts=stereo:sample_rates=48000")
        pre = _tb.seek_preroll(c.in_, fps)
        if pre > 1e-9:
            chain += f",atrim=start={pre:.6f},asetpts=PTS-STARTPTS"
        from .audio_mix import speed_filters
        chain += speed_filters(c)
    chain += _audio_props_filters(c)
    chain += f",apad=whole_len={m},atrim=end_sample={m}"
    return chain + tail + label_out


def _on_render_clock(pips: list[tuple[str, Clip]], seams: clock.SeamTable
                     ) -> list[tuple[str, Clip, float, float]]:
    """`(track_id, clip, render_start, render_end)` for every PIP the seams
    leave visible, in the order `collect_pip_clips` gave. The layout length
    is the clip's TIMELINE footprint (`pip_layout_end`): the chain retimes a
    PIP exactly as v1 (`pip_retime`), and its sound follows (E2)."""
    placed: list[tuple[str, Clip, float, float]] = []
    for tid, c in pips:
        win = clock.render_window(seams, c.start, pip_layout_end(c))
        if win is None:
            continue
        placed.append((tid, c, win[0], win[1]))
    return placed


def build_pip_overlay_chain(
    edl: EDL,
    *,
    source_label: str,
    out_label: str,
    first_input_index: int,
    out_w: int,
    out_h: int,
    preview: bool = False,
    fps=None,
) -> tuple[str, list[str], str, list[Clip]]:
    """Return (filter_chain, extra_inputs, final_video_label, audio_clips).

    Each PiP clip is added as a new ffmpeg input (decoded from its src). The
    chain scales it relative to the canvas (default 35% of canvas long side),
    optionally rotates, then overlays at its timeline position with
    a half-open `[rs, re)` enable gate (`text_overlay.enable_expr`) — its RENDER window. Audio for each clip is returned separately
    so the audio mixer can fold it in with the same timing.
    """
    # Place each PIP on the RENDER clock first: a PIP whose window the v1
    # cross-fades consumed entirely gets no input, no filter and no audio, and
    # the indices/`audio_clips` list below count only what is kept — the
    # compositor pairs `audio_clips[j]` with `pip_audio_input_index(first, j)`.
    pips = _on_render_clock(collect_pip_clips(edl), clock.seam_table(edl))
    if not pips:
        return "", [], source_label, []

    canvas = edl.canvas
    # The RENDER's rate (an export may override the canvas's) — the grid the
    # v1 base is built on, so a PIP's frames land on the same instants.
    fps = canvas.fps if fps is None else fps
    extra_inputs: list[str] = []
    parts: list[str] = []
    audio_clips: list[Clip] = []
    cur = source_label

    # Index of the last clip that will actually be BAKED, so that `out_label` —
    # the name this function is ASKED to produce — is the one the final overlay
    # writes. A positional `i == len(pips)-1` stops meaning that in preview, when
    # the last PIP in the list may be the one the client draws: the label then
    # goes to a stage that never runs and the graph ends on `[pip_postN]`.
    #
    # HARMLESS TODAY, and measured rather than assumed: compositor.py uses the
    # RETURNED label, not `out_label`, so a mixed preview rendered fine either way
    # — byte-identical output (159615 bytes) with a keyed PIP at 0s and a plain
    # one at 3s. This is therefore not a bug fix; it is closing a trap. A
    # parameter named `out_label` that the function silently declines to produce
    # is a promise any future caller would reasonably trust, and the one that
    # already exists only escapes it by not trusting it.
    #
    # Reachable only on a MIXED timeline (a chromakey'd PIP, still baked, ordered
    # before a plain one), and ordering is by `start` — collect_pip_clips sorts —
    # not by list position.
    _last_baked = None
    for _j, (_t, _c, _rs, _re) in enumerate(pips):
        if not (preview and getattr(_c, "chromakey", None) is None):
            _last_baked = _j

    for i, (_tid, c, rs, re) in enumerate(pips):
        idx = first_input_index + INPUTS_PER_PIP * i   # the picture input
        # Trim source on input side so we only decode what's needed, and place
        # the decoded stream at the clip's ABSOLUTE timeline position.
        #
        # Without `-itsoffset` the PIP's frames start at t=0 in the filtergraph
        # while `enable=between(t,start,…)` only reveals it at `start` — so by
        # the time the window opens the stream has already ENDED, and overlay's
        # default eof_action=repeat holds the last decoded frame for the whole
        # appearance. A PIP anywhere but t=0 was therefore a still image of its
        # own final frame; when that frame is dark (a fade-out, a cut to black)
        # the result is a literal black box, which is how this was reported.
        # Measured on a 4s source of 1s colour blocks placed at start=5: the
        # window showed YELLOW (source second 3, the last frame) throughout,
        # where RED (source second 0) was due.
        #
        # This is the same defect the text-overlay path already fixed for
        # animated overlays ("an animated overlay later on the timeline had
        # finished its whole animation before its enable-window even opened"),
        # and the same offset the PIP AUDIO side has always applied via
        # `adelay` in compositor.py — the picture was simply never given it.
        #
        # `-t` rather than `-to`: `-to` is an absolute input timestamp, and
        # `-itsoffset` shifts the timestamps it is compared against, so the two
        # together can truncate the input to nothing. A duration is immune.
        #
        # Both in RENDER time (`rs`): the offset AND the enable gate below, or
        # the two drift apart by the overlap and the old still-image bug is
        # back. `-t` is capped to the render window as well: a PIP straddling a
        # seam is on screen for less than its source length, and the same span
        # feeds the AUDIO input below — trimming both keeps the sound from
        # outlasting the picture by the seconds the seam consumed.
        #
        # Frame-exact since QA-002: `n` frames from `pip_frames`, placed at the
        # SNAPPED start `t0` by `pip_video_timing` inside the graph — not by
        # `-itsoffset`, which breaks input seeking (see pip_input_args).
        f0, n = pip_frames(rs, re, fps)
        t0 = _tb.time_of(f0, fps)
        extra_inputs += pip_input_args(c, n, fps)
        # The AUDIO input — same span, no offset (see INPUTS_PER_PIP). Added on
        # both the baked and the client-drawn branch, since the audio fold in
        # compositor.py indexes off it either way.
        extra_inputs += pip_input_args(c, n, fps)

        if preview and getattr(c, "chromakey", None) is None:
            # PREVIEW: do not bake the PIP's PICTURE — the browser draws it live
            # (lib/pipDraw + StickerLayer), so dragging, resizing and reframing
            # move real frames under the pointer instead of waiting on a
            # re-render. Reported as "the video doesn't follow the blue box… it
            # reacts very late" and "everything should work along the blue box".
            #
            # Same split text and stickers already use, for the same unavoidable
            # reason: a client cannot erase a baked pixel, so painting a live
            # copy over a baked one shows TWO PIPs for the whole gesture and
            # leaves the stale one behind until the render lands.
            #
            # Skipped BEFORE any filter is appended rather than by popping the
            # ones already added — chromakey/mask/rotate/opacity each append
            # conditionally, so a pop count would be wrong the moment one of
            # them changes.
            #
            # The input above is still added, and the AUDIO block below still
            # runs: only the picture is the client's, and a PIP with sound must
            # stay audible in the preview. Export always bakes (no client
            # there), so pipDraw's geometry must match this file's — the same
            # contract TextLayer holds against text_overlay.py.
            #
            # A CHROMAKEY'D clip is excluded from this branch (see the condition)
            # and keeps being baked even in preview: a per-pixel key is the one
            # picture stage a 2D canvas cannot reproduce at 60 Hz, so that clip
            # trades real-time dragging for staying visually TRUE. It is a live
            # case, not a hypothetical — `remove_background` sets a key by itself,
            # and green-screen-then-PIP is exactly why people reach for a PIP.
            # `frontend/src/lib/pipDraw.ts::pipIsClientDrawn` mirrors this rule
            # and must not drift: agreeing the wrong way draws the PIP twice (the
            # client cannot erase the baked copy), the other way draws it never.
            audio_clips.append(c)   # unconditional, matching the bake path below
            continue

        tx = c.transform
        # Speed, curve, freeze (E2): the v1 retime between the rebase and the
        # grid, so the frames are v1's. Keyframes and the enable gate stay on
        # the render clock (`t - t0`): they are TIMELINE-local, like the UI.
        retime = pip_retime(c, fps)
        # Scale relative to canvas long edge. Default size = 35% of canvas long edge.
        # A KEYFRAMED scale (QA-035) builds the element at its LARGEST keyed
        # size — so an animated grow/shrink only ever DOWN-scales pixels — and
        # a per-frame `scale` right before the overlay (below) animates it.
        # It used to take the last key for the whole clip, so the preview grew
        # the PiP while the export sat at its final size throughout.
        scale_kf = is_keyframed(tx.scale)
        sc_static = (_max_key(tx.scale, 1.0) if scale_kf
                     else _scalar_or_last(tx.scale, 1.0))
        # Default PiP "1.0" = 35% of canvas. >1 = larger PiP.
        canvas_long = max(canvas.w, canvas.h)
        # Translate canvas-space scale to output-pixel scale
        out_long = max(out_w, out_h)
        target_long = max(40, int(out_long * 0.35 * sc_static))

        # FRAMING. The element's box is not always the source's own shape:
        #
        #   circle       -> a SQUARE. A circular mask over a 16:9 element is an
        #                   ELLIPSE, which is what "when i chose circle it gave
        #                   me ellipse" was. A circle needs a square to live in,
        #                   so choosing it centre-crops the picture rather than
        #                   squashing it — the framing and the shape are one
        #                   decision, not two.
        #   fit='cover'  -> the CANVAS's aspect, so the PIP reads as a small
        #                   version of the frame. This is what makes the
        #                   Properties panel's "Fill frame" checkbox do anything
        #                   on a PIP lane; pip.py ignored `fit` entirely before,
        #                   so the control was there and inert.
        #   otherwise    -> the source's own aspect (h=-1), unchanged default.
        #
        # A box is filled by scaling to COVER it and cropping the overflow —
        # never by padding, which would put black bars inside the PIP. Both
        # expressions are in output pixels, so no source probe is needed.
        mask_type = str(getattr(getattr(c, "mask", None), "type", "") or "")
        want_square = mask_type == "circle"
        cover = getattr(c, "fit", "contain") == "cover"
        box_w = box_h = 0
        if want_square:
            box_w = box_h = target_long
        elif cover:
            if canvas.w >= canvas.h:
                box_w, box_h = target_long, max(2, round(target_long * canvas.h / max(1, canvas.w)))
            else:
                box_w, box_h = max(2, round(target_long * canvas.w / max(1, canvas.h))), target_long
        scaled_label = f"[pip{i}]"
        if box_w and box_h:
            # Even dimensions: the element is later encoded in a yuv420p graph,
            # and an odd size there is the same chroma-parity trap the v1 chain
            # snaps for.
            box_w += box_w % 2
            box_h += box_h % 2
            # Framing INSIDE the box: zoom past "just covering" and slide the
            # crop window, so you choose WHICH part of the picture lands in the
            # circle rather than always getting its centre.
            fr = getattr(c, "framing", None)
            zoom = max(1.0, float(getattr(fr, "zoom", 1.0) or 1.0))
            fx = float(getattr(fr, "x", 0.0) or 0.0)
            fy = float(getattr(fr, "y", 0.0) or 0.0)
            f_rot = float(getattr(fr, "rotation", 0.0) or 0.0)
            # INNER rotation turns the picture inside the shape while the shape
            # stays put — distinct from Transform.rotation below, which turns
            # the whole element (shape included) on the canvas.
            #
            # It has to happen BEFORE the crop, and the covered source has to be
            # grown first or the rotation drags black corners into the shape: a
            # box_w x box_h window still fully inside a rotated rectangle needs
            # that rectangle to be at least
            #     w*|cos| + h*|sin|  by  w*|sin| + h*|cos|
            # (project the box's own corners onto the rotated axes). Rotating in
            # place then leaves the whole box covered, and the crop that follows
            # never sees an edge. This is the same "grow, then rotate, then cut"
            # shape as the v1 chain's rotation, arrived at from the other side:
            # v1 accepts the cut corners because the canvas IS the frame, while
            # a PIP must not show them inside its shape.
            cover_scale = 1.0
            if abs(f_rot) > 0.001:
                rad_in = math.radians(f_rot)
                ca, sa = abs(math.cos(rad_in)), abs(math.sin(rad_in))
                need_w = box_w * ca + box_h * sa
                need_h = box_w * sa + box_h * ca
                cover_scale = max(need_w / box_w, need_h / box_h)
            cover_w = max(box_w, int(round(box_w * zoom * cover_scale)))
            cover_h = max(box_h, int(round(box_h * zoom * cover_scale)))
            cover_w += cover_w % 2
            cover_h += cover_h % 2
            # `crop` pins x/y into [0, in-out] itself, so a normalised offset
            # with no margin to move in is a no-op rather than a black edge —
            # the same clamp the v1 cover-pan documents. Expressed against
            # in_w/in_h (not the requested cover size) because
            # force_original_aspect_ratio=increase can overshoot on one axis.
            x_expr = f"(in_w-out_w)/2+({fx:.4f})*(in_w-out_w)/2"
            y_expr = f"(in_h-out_h)/2+({fy:.4f})*(in_h-out_h)/2"
            inner_rot = ""
            if abs(f_rot) > 0.001:
                # In place (no ow/oh): the grown cover above is what keeps the
                # crop clear of the corners.
                inner_rot = f"rotate={math.radians(f_rot):.6f}:c=black@0,"
            parts.append(
                f"[{idx}:v]{pip_video_timing(n, f0, fps, retime)}{square_pixels_filter(c.src)}"
                f"scale={cover_w}:{cover_h}:force_original_aspect_ratio=increase,"
                f"{inner_rot}"
                f"crop={box_w}:{box_h}:'{x_expr}':'{y_expr}'{scaled_label}"
            )
        else:
            # We don't know the source aspect; -1 preserves it
            # (an anamorphic source is squared first, lane E1a: `h=-1` and
            # the UI's box, <video>.videoWidth, use its DISPLAYED aspect)
            parts.append(f"[{idx}:v]{pip_video_timing(n, f0, fps, retime)}{square_pixels_filter(c.src)}"
                         f"scale=w={target_long}:h=-1{scaled_label}")

        # Optional chroma key BEFORE rotate/opacity so transparency survives.
        if getattr(c, "chromakey", None) is not None:
            keyed_label = f"[pipk{i}]"
            parts.append(f"{scaled_label}{build_chromakey_filter(c.chromakey)}{keyed_label}")
            scaled_label = keyed_label

        # Optional SHAPE mask — a circular/rounded PIP instead of a hard
        # rectangle. Before rotate/opacity, for the same reason as chromakey:
        # those stages must inherit the alpha, not overwrite it.
        #
        # Cut procedurally with `geq` rather than by alphamerging the canvas-
        # sized PNG that effects.render_mask_png builds for v1. The PIP is
        # scaled to `target_long` on its WIDTH with `h=-1`, so its pixel height
        # depends on the source's aspect, which this module never probes — a
        # canvas-sized mask would be the wrong size and off-centre. A geq
        # expression is written in the stream's own W/H, so it fits whatever the
        # scaler produced and needs no dimensions up front.
        mask_expr = _shape_alpha_expr(getattr(c, "mask", None))
        if mask_expr:
            shaped = f"[pipm{i}]"
            parts.append(
                f"{scaled_label}format=yuva420p,"
                f"geq=lum='p(X\\,Y)':cb='p(X\\,Y)':cr='p(X\\,Y)':a='{mask_expr}'{shaped}"
            )
            scaled_label = shaped

        # Keyed values run on the element's own clock, `t - t0` (timeline-
        # local, like the UI), frame-exact (`frame_exact_expr`).
        kt = f"(t-{t0:.9f})"

        # Optional rotation. KEYED (review RD3): per frame, on a square
        # canvas big enough for any angle (hypot), so the overlay's centring
        # on overlay_w/2 keeps the pivot where the preview has it. It used to
        # take the LAST key for the whole clip — a keyed spin exported still.
        if is_keyframed(tx.rotation):
            re_ = frame_exact_expr(tx.rotation, kt)
            rotated = f"[pipr{i}]"
            parts.append(f"{scaled_label}rotate=a='({re_})*PI/180':c=black@0"
                         f":ow='hypot(iw\\,ih)':oh='hypot(iw\\,ih)'{rotated}")
            scaled_label = rotated
        else:
            rot_static = _scalar_or_last(tx.rotation, 0.0)
            if abs(rot_static) > 0.01:
                rad = rot_static * 3.14159265 / 180.0
                rotated = f"[pipr{i}]"
                parts.append(f"{scaled_label}rotate={rad}:c=black@0:ow=rotw({rad}):oh=roth({rad}){rotated}")
                scaled_label = rotated

        # Optional opacity. KEYED (review RD3): a per-frame alpha multiply,
        # after the shape mask and the rotation so their alphas multiply in.
        # (geq's clock is `T`.) It used to be the LAST key throughout.
        if is_keyframed(tx.opacity):
            oe = frame_exact_expr(tx.opacity, f"(T-{t0:.9f})")
            faded = f"[pipo{i}]"
            parts.append(f"{scaled_label}format=yuva420p,"
                         f"geq=lum='p(X\\,Y)':cb='p(X\\,Y)':cr='p(X\\,Y)'"
                         f":a='alpha(X\\,Y)*({oe})'{faded}")
            scaled_label = faded
        else:
            opa_static = _scalar_or_last(tx.opacity, 1.0)
            if opa_static < 0.999:
                faded = f"[pipo{i}]"
                parts.append(f"{scaled_label}format=yuva420p,colorchannelmixer=aa={opa_static:.3f}{faded}")
                scaled_label = faded

        # Animated scale (QA-035): the element above is built at the largest
        # keyed scale; shrink it per frame to S(t)/S_max. Same mechanism the
        # text `pop` preset has always used (scale with eval=frame, then an
        # overlay whose x/y centre on overlay_w/overlay_h, which overlay
        # re-reads every frame). Keyframes are clip-local and the input sits
        # at `rs` via -itsoffset, so the local clock is (t - rs). LAST stage,
        # so every filter before it (shape geq, rotate, opacity) sees a fixed
        # frame size.
        if scale_kf and sc_static > 0:
            se = frame_exact_expr(tx.scale, kt)
            ratio = f"(({se})/{sc_static:.6f})"
            animated = f"[pips{i}]"
            parts.append(
                f"{scaled_label}scale=w='max(2\\,trunc(iw*{ratio}/2)*2)'"
                f":h='max(2\\,trunc(ih*{ratio}/2)*2)':eval=frame{animated}")
            scaled_label = animated

        # Position: x/y are CANVAS-space pixels of the clip's center.
        # Translate to OUTPUT-space top-left.
        sx = out_w / max(1, canvas.w)
        sy = out_h / max(1, canvas.h)
        x_kf = tx.x
        y_kf = tx.y
        if is_keyframed(x_kf):
            xe = frame_exact_expr(x_kf, kt)
            x_expr = f"({xe})*{sx:.6f}-overlay_w/2"
        else:
            xc = float(getattr(tx, "x", 0)) if isinstance(tx.x, (int, float)) else canvas.w / 2
            x_expr = f"({xc * sx:.2f})-overlay_w/2"
        if is_keyframed(y_kf):
            ye = frame_exact_expr(y_kf, kt)
            y_expr = f"({ye})*{sy:.6f}-overlay_h/2"
        else:
            yc = float(getattr(tx, "y", 0)) if isinstance(tx.y, (int, float)) else canvas.h / 2
            y_expr = f"({yc * sy:.2f})-overlay_h/2"

        # The last BAKED clip, not the last clip — see `_last_baked` above.
        is_last = i == _last_baked
        next_label = out_label if is_last else f"[pip_post{i}]"
        parts.append(
            f"{cur}{scaled_label}overlay=x='{x_expr}':y='{y_expr}'"
            f":enable='{enable_expr(rs, re, fps)}'{next_label}"
        )
        cur = next_label
        audio_clips.append(c)

    return ";".join(parts), extra_inputs, cur, audio_clips
