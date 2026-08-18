"""V2 picture-in-picture overlay path.

V1 is the base layer (concatenated full-screen). Each clip on V2 (or any
non-V1 video track) is overlaid on top with its transform (scale, x, y,
rotation, opacity) applied, and only visible during its timeline range
(`enable=between(t,start,end)`).

Audio from V2 clips also gets mixed into the final audio output so PiP
clips with sound (talking-head over screen recording, etc.) play correctly.
"""
from __future__ import annotations
import math
from pathlib import Path
from ..edl import EDL
from ..edl.schema import Clip
from ..edl.keyframes import is_keyframed, to_ffmpeg_expr
from .effects import build_chromakey_filter


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


def build_pip_overlay_chain(
    edl: EDL,
    *,
    source_label: str,
    out_label: str,
    first_input_index: int,
    out_w: int,
    out_h: int,
    preview: bool = False,
) -> tuple[str, list[str], str, list[Clip]]:
    """Return (filter_chain, extra_inputs, final_video_label, audio_clips).

    Each PiP clip is added as a new ffmpeg input (decoded from its src). The
    chain scales it relative to the canvas (default 35% of canvas long side),
    optionally rotates, then overlays at its timeline position with
    `enable=between(t,start,end)`. Audio for each clip is returned separately
    so the audio mixer can fold it in with the same timing.
    """
    pips = collect_pip_clips(edl)
    if not pips:
        return "", [], source_label, []

    canvas = edl.canvas
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
    for _j, (_t, _c) in enumerate(pips):
        if not (preview and getattr(_c, "chromakey", None) is None):
            _last_baked = _j

    for i, (_tid, c) in enumerate(pips):
        idx = first_input_index + i
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
        extra_inputs += ["-ss", f"{c.in_:.3f}", "-t", f"{max(0.001, c.out - c.in_):.3f}"]
        if c.start > 0.0005:
            extra_inputs += ["-itsoffset", f"{c.start:.3f}"]
        extra_inputs += ["-i", c.src]

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
        # Scale relative to canvas long edge. Default size = 35% of canvas long edge.
        sc_static = _scalar_or_last(tx.scale, 1.0)
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
                f"[{idx}:v]scale={cover_w}:{cover_h}:force_original_aspect_ratio=increase,"
                f"{inner_rot}"
                f"crop={box_w}:{box_h}:'{x_expr}':'{y_expr}'{scaled_label}"
            )
        else:
            # We don't know the source aspect; -1 preserves it
            parts.append(f"[{idx}:v]scale=w={target_long}:h=-1{scaled_label}")

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

        # Optional rotation
        rot_static = _scalar_or_last(tx.rotation, 0.0)
        if abs(rot_static) > 0.01:
            rad = rot_static * 3.14159265 / 180.0
            rotated = f"[pipr{i}]"
            parts.append(f"{scaled_label}rotate={rad}:c=black@0:ow=rotw({rad}):oh=roth({rad}){rotated}")
            scaled_label = rotated

        # Optional opacity
        opa_static = _scalar_or_last(tx.opacity, 1.0)
        if opa_static < 0.999:
            faded = f"[pipo{i}]"
            parts.append(f"{scaled_label}format=yuva420p,colorchannelmixer=aa={opa_static:.3f}{faded}")
            scaled_label = faded

        # Position: x/y are CANVAS-space pixels of the clip's center.
        # Translate to OUTPUT-space top-left.
        sx = out_w / max(1, canvas.w)
        sy = out_h / max(1, canvas.h)
        x_kf = tx.x
        y_kf = tx.y
        if is_keyframed(x_kf):
            xe = to_ffmpeg_expr(x_kf, time_var=f"(t-{c.start:.4f})")
            x_expr = f"({xe})*{sx:.6f}-overlay_w/2"
        else:
            xc = float(getattr(tx, "x", 0)) if isinstance(tx.x, (int, float)) else canvas.w / 2
            x_expr = f"({xc * sx:.2f})-overlay_w/2"
        if is_keyframed(y_kf):
            ye = to_ffmpeg_expr(y_kf, time_var=f"(t-{c.start:.4f})")
            y_expr = f"({ye})*{sy:.6f}-overlay_h/2"
        else:
            yc = float(getattr(tx, "y", 0)) if isinstance(tx.y, (int, float)) else canvas.h / 2
            y_expr = f"({yc * sy:.2f})-overlay_h/2"

        # The last BAKED clip, not the last clip — see `_last_baked` above.
        is_last = i == _last_baked
        next_label = out_label if is_last else f"[pip_post{i}]"
        parts.append(
            f"{cur}{scaled_label}overlay=x='{x_expr}':y='{y_expr}'"
            f":enable='between(t\\,{c.start:.3f}\\,{c.start + c.duration:.3f})'{next_label}"
        )
        cur = next_label
        audio_clips.append(c)

    return ";".join(parts), extra_inputs, cur, audio_clips
