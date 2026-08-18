"""PIP (v2 overlay) — placement when a clip is DRAGGED onto the lane, and the
time offset that makes it play its own footage.

Reported as "PIP/overlay not working properly, instead of adding a layer on the
video, it just applies a black box on the top left". That is two defects at
once, and each on its own is enough to produce the description:

  * a clip dragged onto v2 kept Transform's plain x=0/y=0, which the PIP
    renderer reads as "centre this on the canvas ORIGIN" — three-quarters
    off-screen, only the bottom-right corner showing, in the top-left;
  * the PIP's input carried no `-itsoffset`, so its frames started at t=0 in
    the filtergraph while `enable=between(t,start,…)` only revealed it later.
    By then the stream had ended and overlay's default eof_action=repeat held
    the LAST decoded frame for the entire appearance — a still image, and a
    black one whenever the clip ends dark.

The PIP AUDIO path has always applied that offset via `adelay`, so the sound
played in the right place while the picture was frozen.
"""
from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl.schema import Clip, Keyframe
from video_ai_editor.edl.snapshot import EDLStore
from video_ai_editor.render.pip import build_pip_overlay_chain


def _store(tmp_path: Path) -> EDLStore:
    s = EDLStore(tmp_path)
    s.edl.canvas.w, s.edl.canvas.h = 1080, 1920
    s.edl.get_track("v1").clips.append(
        Clip(id="base", src="/x/base.mp4", in_=0, out=10, start=0.0))
    s.commit("seed", {}, "seed")
    return s


def _chain(edl, **kw):
    return build_pip_overlay_chain(
        edl, source_label="[v]", out_label="[out]", first_input_index=1,
        out_w=kw.get("w", 1080), out_h=kw.get("h", 1920))


# ------------------------------------------------- placement on a dragged clip

def test_dragging_a_clip_onto_v2_centres_it(tmp_path):
    """The reported top-left box. Transform defaults are 0,0 — fine as a v1
    crop pan, nonsense as a PIP's canvas centre."""
    s = _store(tmp_path)
    v1 = s.edl.get_track("v1")
    v1.clips.append(Clip(id="c1", src="/x/a.mp4", in_=0, out=3, start=0.0))
    s.commit("seed2", {}, "seed2")

    res = dispatch(s, "move_clip", {"clip_id": "c1", "new_track": "v2",
                                    "new_start": 2.0})
    _, c = s.edl.get_clip("c1")
    assert (c.transform.x, c.transform.y) == pytest.approx((540.0, 960.0))
    assert c.transform.scale == pytest.approx(0.6)
    assert res["transform_rebased"] == "centred as a PIP"


def test_the_centred_pip_is_not_drawn_off_canvas(tmp_path):
    """Prove it through the filtergraph, not just the stored numbers: at 0,0
    the overlay x expression is `(0.00)-overlay_w/2`, i.e. negative."""
    s = _store(tmp_path)
    v2 = s.edl.get_track("v2")
    off = Clip(id="off", src="/x/a.mp4", in_=0, out=3, start=0.0)
    v2.clips.append(off)
    chain, *_ = _chain(s.edl)
    assert "x='(0.00)-overlay_w/2'" in chain, "pre-fix placement (kept as the foil)"

    off.transform.x, off.transform.y = 540.0, 960.0
    chain, *_ = _chain(s.edl)
    assert "x='(540.00)-overlay_w/2'" in chain
    assert "y='(960.00)-overlay_h/2'" in chain


def test_moving_a_pip_back_to_v1_clears_the_placement(tmp_path):
    """A PIP's centre is meaningless as a v1 crop pan, where it instead slides
    an already-fitted picture and crops the far edge."""
    s = _store(tmp_path)
    v2 = s.edl.get_track("v2")
    pip = Clip(id="p", src="/x/a.mp4", in_=0, out=3, start=0.0)
    pip.transform.x, pip.transform.y, pip.transform.scale = 800.0, 1500.0, 0.6
    v2.clips.append(pip)
    s.commit("seed2", {}, "seed2")

    dispatch(s, "move_clip", {"clip_id": "p", "new_track": "v1", "new_start": 20.0})
    _, c = s.edl.get_clip("p")
    assert (c.transform.x, c.transform.y, c.transform.scale) == pytest.approx((0.0, 0.0, 1.0))


def test_a_keyframed_transform_is_left_alone(tmp_path):
    """Same rule set_clip_fit follows: one lane change cannot express what an
    authored curve should become, and destroying it silently is worse."""
    s = _store(tmp_path)
    v1 = s.edl.get_track("v1")
    c = Clip(id="k", src="/x/a.mp4", in_=0, out=3, start=0.0)
    c.transform.x = Keyframe(keyframes=[(0.0, 100.0), (2.0, 400.0)])
    v1.clips.append(c)
    s.commit("seed2", {}, "seed2")

    res = dispatch(s, "move_clip", {"clip_id": "k", "new_track": "v2", "new_start": 1.0})
    _, moved = s.edl.get_clip("k")
    assert not isinstance(moved.transform.x, (int, float)), "the curve survives"
    assert res["transform_rebased"] is None


def test_moving_between_two_pip_lanes_changes_nothing(tmp_path):
    """v2 and any other non-v1 video lane mean the same thing, so a move
    between them must preserve the layout exactly."""
    s = _store(tmp_path)
    s.edl.tracks.append(type(s.edl.get_track("v2"))(
        id="v3", type="video", z=2, label="PIP 2"))
    v2 = s.edl.get_track("v2")
    pip = Clip(id="p", src="/x/a.mp4", in_=0, out=3, start=0.0)
    pip.transform.x, pip.transform.y, pip.transform.scale = 800.0, 1500.0, 0.4
    v2.clips.append(pip)
    s.commit("seed2", {}, "seed2")

    dispatch(s, "move_clip", {"clip_id": "p", "new_track": "v3", "new_start": 0.0})
    _, c = s.edl.get_clip("p")
    assert (c.transform.x, c.transform.y, c.transform.scale) == pytest.approx((800.0, 1500.0, 0.4))


def test_a_same_lane_move_never_rebases(tmp_path):
    s = _store(tmp_path)
    v2 = s.edl.get_track("v2")
    pip = Clip(id="p", src="/x/a.mp4", in_=0, out=3, start=0.0)
    pip.transform.x, pip.transform.y = 800.0, 1500.0
    v2.clips.append(pip)
    s.commit("seed2", {}, "seed2")

    dispatch(s, "move_clip", {"clip_id": "p", "new_start": 7.0})
    _, c = s.edl.get_clip("p")
    assert (c.transform.x, c.transform.y) == pytest.approx((800.0, 1500.0))


# ------------------------------------------------------------ the time offset

def test_a_pip_input_is_offset_to_its_timeline_position(tmp_path):
    s = _store(tmp_path)
    s.edl.get_track("v2").clips.append(
        Clip(id="p", src="/x/a.mp4", in_=1.0, out=5.0, start=8.0))
    _, inputs, _, _ = _chain(s.edl)
    assert "-itsoffset" in inputs
    assert inputs[inputs.index("-itsoffset") + 1] == "8.000"
    # …decoding only the trimmed span, expressed as a DURATION.
    assert inputs[inputs.index("-ss") + 1] == "1.000"
    assert inputs[inputs.index("-t") + 1] == "4.000"


def test_a_duration_is_used_not_an_absolute_end(tmp_path):
    """`-to` is an absolute input timestamp and `-itsoffset` shifts the
    timestamps it is compared against, so the two together can truncate the
    input to nothing. A duration is immune."""
    s = _store(tmp_path)
    s.edl.get_track("v2").clips.append(
        Clip(id="p", src="/x/a.mp4", in_=2.0, out=6.0, start=30.0))
    _, inputs, _, _ = _chain(s.edl)
    assert "-to" not in inputs


def test_a_pip_at_zero_needs_no_offset(tmp_path):
    """Nothing to shift, and the argv stays exactly what it always was."""
    s = _store(tmp_path)
    s.edl.get_track("v2").clips.append(
        Clip(id="p", src="/x/a.mp4", in_=0.0, out=4.0, start=0.0))
    _, inputs, _, _ = _chain(s.edl)
    assert "-itsoffset" not in inputs


def test_every_pip_gets_its_own_offset(tmp_path):
    s = _store(tmp_path)
    v2 = s.edl.get_track("v2")
    v2.clips.append(Clip(id="p1", src="/x/a.mp4", in_=0, out=2, start=1.0))
    v2.clips.append(Clip(id="p2", src="/x/b.mp4", in_=0, out=2, start=6.0))
    _, inputs, _, _ = _chain(s.edl)
    offsets = [inputs[i + 1] for i, a in enumerate(inputs) if a == "-itsoffset"]
    assert offsets == ["1.000", "6.000"]


def test_the_render_behaviour_salt_moved_for_this_fix():
    """A cache that outlives the fix hides the fix.

    The preview cache keys on `edl.hash()`, the chunk cache and the audio-only
    remux fast path key on their own fingerprints, and all three fold in
    RENDER_BEHAVIOR_VERSION. A session holding a cached render of a PIP at
    start>0 (or of a rotated clip — same round) would otherwise be served the
    pre-fix pixels forever for an EDL nobody edited, which reads as "the fix
    did nothing" and is exactly the failure this salt exists to prevent.
    """
    from video_ai_editor.edl.schema import RENDER_BEHAVIOR_VERSION
    assert RENDER_BEHAVIOR_VERSION >= 8


def test_the_enable_window_still_matches_the_clip(tmp_path):
    """The offset places the frames; `enable` still gates the appearance. If
    these two ever disagree the PIP flashes the wrong footage at its edges."""
    s = _store(tmp_path)
    s.edl.get_track("v2").clips.append(
        Clip(id="p", src="/x/a.mp4", in_=1.0, out=5.0, start=8.0))
    chain, _, _, _ = _chain(s.edl)
    assert "between(t\\,8.000\\,12.000)" in chain


# ------------------------------------- the preview/export split (client pixels)

def test_preview_skips_the_pip_picture_but_keeps_its_audio(tmp_path):
    """In PREVIEW the browser draws the PIP's picture (lib/pipDraw.ts) so a drag
    moves real frames under the pointer instead of waiting on an ffmpeg
    round-trip — the same split text and stickers already use, and for the same
    unavoidable reason: a client cannot erase a baked pixel, so painting a live
    copy over a baked one shows TWO PIPs for the whole gesture.

    Three separate claims, and each one has a failure mode of its own:
      * no picture filters, or the preview double-draws;
      * the INPUT is still added, because the audio block indexes off it;
      * the clip still comes back as an audio clip, or a PIP with sound goes
        silent in the preview while playing fine on export.
    """
    s = _store(tmp_path)
    s.edl.get_track("v2").clips.append(
        Clip(id="p", src="/x/a.mp4", in_=0, out=3, start=0.0))

    baked_chain, baked_inputs, baked_label, baked_audio = _chain(s.edl)
    prev_chain, prev_inputs, prev_label, prev_audio = build_pip_overlay_chain(
        s.edl, source_label="[v]", out_label="[out]", first_input_index=1,
        out_w=1080, out_h=1920, preview=True)

    assert baked_chain and "overlay=" in baked_chain
    assert prev_chain == ""
    # The video label must fall THROUGH untouched: an [out] that nothing writes
    # to is an ffmpeg "matches no streams" failure, not a missing PIP.
    assert baked_label == "[out]" and prev_label == "[v]"
    assert prev_inputs == baked_inputs         # audio indexes off these
    assert [c.id for c in prev_audio] == [c.id for c in baked_audio] == ["p"]


def test_a_pip_timeline_is_never_assembled_by_chunk_streamcopy(tmp_path):
    """The chunk-streamcopy fast path returns BEFORE the PIP audio fold, so it
    must be gated on "no PIP at all" — and its old gate was `not pip_chain`,
    which stopped meaning that the moment preview stopped emitting PIP video
    filters. It admitted PIP timelines and stream-copied the chunks with the
    PIP's audio never mixed in (measured: a near-silent v1 plus a full-gain PIP
    rendered at -74.2 dB, i.e. silence).

    Asserted against the source because the gate is a claim about control flow
    that no output pixel can distinguish; the audible half is covered
    end-to-end by test_track_and_pip_audio.py::test_pip_clip_gain_is_honored.
    """
    import inspect
    from video_ai_editor.render import compositor

    src = inspect.getsource(compositor)
    i = src.index("_assemble_chunks_streamcopy(edl, chunk_paths, dst)")
    gate = src[src.rindex("if (preview and chunk_paths", 0, i):i]
    assert "not pip_audio_clips" in gate, (
        "the streamcopy gate must test for PIP AUDIO, not just an empty PIP "
        f"video chain; got:\n{gate}")


# ---------------------------------------- the box table shared with the preview

def test_the_box_pip_py_builds_matches_the_table(tmp_path):
    """The exported box, pinned case by case.

    Since preview stopped baking the PIP picture, the box exists twice: here and
    in `pipGeom` (frontend/src/lib/overlay.ts), with no shared source. The same
    table is asserted on the other side by overlay.test.ts's "pipGeom matches
    pip.py box dimensions" — this test is the half that says what the number
    actually IS, so a change here fails there too instead of only showing up
    when somebody exports.

    Canvas and output are both 1080x1920, so target_long = 1920*0.35 = 672.

    The ordering claim matters most: `want_square` is tested BEFORE `cover`, so
    a circle keeps its square even with fit='cover'. Deriving the box from a
    single aspect plus a rule about which edge 672 lands on gets that case wrong
    (0.5625x too small on a portrait canvas) — which it did, on the preview side.
    """
    from video_ai_editor.edl.schema import Canvas, Mask, Framing, Transform, Track, EDL

    def box(**kw) -> str:
        c = Clip(src="/x/a.mp4", in_=0, out=3, start=0.0, id="p")
        for k, v in kw.items():
            setattr(c, k, v)
        edl = EDL(canvas=Canvas(w=1080, h=1920, fps=30), tracks=[
            Track(id="v1", type="video", clips=[]),
            Track(id="v2", type="video", z=1, clips=[c])])
        chain, _, _, _ = build_pip_overlay_chain(
            edl, source_label="[v]", out_label="[o]", first_input_index=1,
            out_w=1080, out_h=1920)
        return chain

    # Default: WIDTH is pinned and the height follows the source, so a portrait
    # source is TALLER than 672 — not "the long edge is 672".
    assert "scale=w=672:h=-1" in box()
    # `rounded` does NOT force a square; only `circle` does.
    assert "scale=w=672:h=-1" in box(mask=Mask(type="rounded"))

    assert "crop=672:672" in box(mask=Mask(type="circle"))
    # cover takes the canvas's shape; the long edge is the height here.
    assert "crop=378:672" in box(fit="cover")
    # The ordering case: circle beats cover.
    assert "crop=672:672" in box(mask=Mask(type="circle"), fit="cover")
    assert "crop=1344:1344" in box(mask=Mask(type="circle"),
                                   transform=Transform(scale=2.0))

    # Framing zoom scales what is COVERED and crops the same box back out, so
    # the element's size on the canvas is unchanged — the zoom reframes the
    # picture inside the shape, which is the whole point of the control.
    zoomed = box(mask=Mask(type="circle"), framing=Framing(zoom=2.0))
    assert "scale=1344:1344" in zoomed and "crop=672:672" in zoomed


def test_a_chromakeyed_pip_is_still_baked_in_preview(tmp_path):
    """The one carve-out from the preview/client split.

    Every other picture stage pip.py applies has a canvas equivalent — box crop,
    framing, mask, rotation, opacity — but a per-pixel chroma key does not, so
    that clip keeps being baked and trades real-time dragging for being visually
    TRUE. Without the carve-out the preview showed a green-screen PIP UNKEYED,
    background and all, while the export keyed it.

    Live, not hypothetical: `chroma_key` is a tool and `remove_background` sets a
    key by itself, which is a usual route to a PIP in the first place.

    Mirrored by `pipIsClientDrawn` in frontend/src/lib/pipDraw.ts; the two must
    agree or the PIP is drawn twice (the client cannot erase a baked pixel) or
    not at all.
    """
    from video_ai_editor.edl.schema import ChromaKey

    s = _store(tmp_path)
    v2 = s.edl.get_track("v2")
    v2.clips.append(Clip(id="plain", src="/x/a.mp4", in_=0, out=3, start=0.0))
    v2.clips.append(Clip(id="keyed", src="/x/b.mp4", in_=0, out=3, start=3.0,
                         chromakey=ChromaKey(color="#00FF00")))

    chain, inputs, label, audio = build_pip_overlay_chain(
        s.edl, source_label="[v]", out_label="[o]", first_input_index=1,
        out_w=1080, out_h=1920, preview=True)

    # The keyed clip is baked: its key, and therefore an overlay, must be there.
    assert "colorkey" in chain or "chromakey" in chain, chain
    # ...and the plain one is NOT: exactly one overlay, for the keyed clip.
    assert chain.count("overlay=") == 1, chain
    # Both still contribute audio and an input, whichever way the picture went.
    assert [c.id for c in audio] == ["plain", "keyed"]
    assert inputs.count("-i") == 2
    # The chain must END on the declared out label, or ffmpeg fails with
    # "matches no streams" — a mixed timeline is exactly where an off-by-one in
    # the is_last bookkeeping would hide.
    assert label == "[o]" and chain.rstrip().endswith("[o]"), chain


def test_the_client_drawn_rule_is_the_same_on_both_sides():
    """Pins the two implementations of one rule to each other, by source.

    Nothing else can: the Python side decides whether to emit filters, the TS
    side decides whether to paint, and they only disagree at runtime, in a
    browser, as either a doubled or a missing PIP.
    """
    import pathlib as _pl

    ts = _pl.Path("frontend/src/lib/pipDraw.ts").read_text(encoding="utf-8")
    assert "export function pipIsClientDrawn" in ts
    assert "clip.chromakey == null" in ts, (
        "pipIsClientDrawn must key off chromakey and nothing else, to match "
        "pip.py's preview branch")

    py = _pl.Path("src/video_ai_editor/render/pip.py").read_text(encoding="utf-8")
    assert 'if preview and getattr(c, "chromakey", None) is None:' in py


@pytest.mark.parametrize("keyed_first", [True, False])
def test_a_mixed_preview_still_ends_on_the_declared_out_label(tmp_path, keyed_first):
    """One baked PIP + one client-drawn one, in BOTH orders.

    `is_last` was positional (`i == len(pips)-1`), which stops meaning "the last
    BAKED clip" once preview skips some: put the client-drawn one last and
    `out_label` went to a stage that never ran, leaving the graph ending on
    `[pip_post0]`.

    NOT a render failure — measured, not assumed: compositor.py uses the RETURNED
    label rather than `out_label`, and the mixed preview rendered byte-identically
    (159615 bytes) before and after. What this pins is the FUNCTION'S CONTRACT: a
    parameter named `out_label` should name the stream the function produces, and
    the sole caller escapes the discrepancy only by not relying on it.

    Parametrised on order because the passing order hides it completely, and a
    one-PIP timeline cannot see it at all.

    Order is set through `start`, NOT list position: collect_pip_clips sorts by
    start, so appending in the other order changes nothing and the parametrize
    would silently run the same case twice — which it did on the first attempt.
    """
    from video_ai_editor.edl.schema import ChromaKey

    s = _store(tmp_path)
    keyed_at, plain_at = (0.0, 3.0) if keyed_first else (3.0, 0.0)
    plain = Clip(id="plain", src="/x/a.mp4", in_=0, out=3, start=plain_at)
    keyed = Clip(id="keyed", src="/x/b.mp4", in_=0, out=3, start=keyed_at,
                 chromakey=ChromaKey(color="#00FF00"))
    v2 = s.edl.get_track("v2")
    v2.clips.extend([plain, keyed])

    chain, _, label, _ = build_pip_overlay_chain(
        s.edl, source_label="[v]", out_label="[o]", first_input_index=1,
        out_w=1080, out_h=1920, preview=True)

    assert chain.count("overlay=") == 1, chain
    assert label == "[o]"
    assert chain.rstrip().endswith("[o]"), (
        "the one baked overlay must write the promised out label regardless of "
        f"where it sits in the list: {chain}")
    # And it must not ALSO emit a dangling intermediate that nothing reads.
    assert "[pip_post" not in chain, chain


def test_an_all_client_drawn_preview_hands_the_source_label_straight_back(tmp_path):
    """The ordinary preview case: nothing baked, so the caller must keep using
    its own label. Returning `out_label` here would name a stream the graph never
    produces — the same ffmpeg failure from the other direction."""
    s = _store(tmp_path)
    v2 = s.edl.get_track("v2")
    v2.clips.append(Clip(id="a", src="/x/a.mp4", in_=0, out=3, start=0.0))
    v2.clips.append(Clip(id="b", src="/x/b.mp4", in_=0, out=3, start=3.0))

    chain, inputs, label, audio = build_pip_overlay_chain(
        s.edl, source_label="[v]", out_label="[o]", first_input_index=1,
        out_w=1080, out_h=1920, preview=True)

    assert chain == ""
    assert label == "[v]"
    # Inputs and audio are still both there — a silent PIP is its own bug.
    assert inputs.count("-i") == 2
    assert [c.id for c in audio] == ["a", "b"]


def test_only_circle_and_rounded_cut_a_pip_and_invert_cannot_change_that():
    """The shape list, pinned against the client's `maskCuts`.

    `Mask.type` allows rectangle/linear/mirror/heart/star too, and a PIP
    implements none of them — rectangle deliberately (it is the frame's own
    shape) and the rest because render_mask_png is v1-only. So those emit NO
    alpha and the PIP stays a full rectangle.

    The invert half is the subtle one: this returns None before it ever looks at
    invert, so an inverted rectangle bakes fully VISIBLE. A client that applied a
    hole for it would blank the PIP in preview while the export stayed intact —
    a divergence in the reassuring direction, which is the kind that ships.
    """
    from video_ai_editor.edl.schema import Mask
    from video_ai_editor.render.pip import _shape_alpha_expr

    assert _shape_alpha_expr(None) is None
    for t in ("circle", "rounded"):
        assert _shape_alpha_expr(Mask(type=t)), t
        assert _shape_alpha_expr(Mask(type=t, invert=True)), t
    for t in ("rectangle", "linear", "mirror", "heart", "star"):
        assert _shape_alpha_expr(Mask(type=t)) is None, t
        assert _shape_alpha_expr(Mask(type=t, invert=True)) is None, t

    # And the client agrees, by source — the two lists have no shared origin.
    import pathlib as _pl
    ts = _pl.Path("frontend/src/lib/pipDraw.ts").read_text(encoding="utf-8")
    assert "mask?.type === 'circle' || mask?.type === 'rounded'" in ts, (
        "maskCuts must implement exactly the shapes _shape_alpha_expr does")


def test_moving_a_pip_back_to_v1_drops_its_shape_and_framing(tmp_path):
    """Reported as: "when i moved the pip video to the main video, it showed me
    this" — a mostly-black frame with the footage cut to a circle off to one side.

    Resetting x/y/scale while leaving `mask` behind is not a reset. compositor.py
    alphamerges a canvas-sized mask PNG for a v1 clip, so the circle chosen to
    make a ROUND PIP kept cutting the picture once the clip WAS the main video —
    and the mask's own `position`, a sensible centre for the 1080x1920 canvas it
    was authored on, put the hole off-centre on a 1920x1080 one.

    Confirmed against the real session (s_91712d3b26), whose second v1 clip
    carried mask=circle, fit=cover and framing={x:-0.2, zoom:1.25} after being
    moved back from the PIP lane.

    `framing` goes too: only render/pip.py reads it, so on v1 it is invisible dead
    state that would silently resurrect a stale crop if the clip ever returned to
    a PIP lane.
    """
    from video_ai_editor.edl.schema import Framing, Mask

    s = _store(tmp_path)
    s.edl.canvas.w, s.edl.canvas.h = 1920, 1080
    pip = Clip(id="p", src="/x/a.mp4", in_=2.0, out=4.0, start=0.0)
    pip.transform.x, pip.transform.y, pip.transform.scale = 800.0, 500.0, 0.6
    pip.mask = Mask(type="circle")
    pip.framing = Framing(zoom=1.25, x=-0.2)
    pip.fit = "cover"
    s.edl.get_track("v2").clips.append(pip)
    s.commit("seed2", {}, "seed2")

    res = dispatch(s, "move_clip", {"clip_id": "p", "new_track": "v1", "new_start": 2.0})
    _, c = s.edl.get_clip("p")
    assert c.mask is None, "a PIP's shape must not keep cutting the main video"
    assert c.framing is None, "PIP-only framing must not ride along to v1"
    assert (c.transform.x, c.transform.y, c.transform.scale) == pytest.approx((0.0, 0.0, 1.0))
    # `fit` deliberately survives: it is meaningful on BOTH lanes, and `cover` on
    # v1 means "fill the frame", which is what the reset promises.
    assert c.fit == "cover"
    assert "dropped" in (res.get("transform_rebased") or ""), res


def test_the_mask_survives_a_move_between_two_pip_lanes(tmp_path):
    """v2 and v3 mean the same thing, so a round PIP must stay round. The drop is
    scoped to the lane CHANGE that redefines these fields, not to any move."""
    from video_ai_editor.edl.schema import Framing, Mask

    s = _store(tmp_path)
    s.edl.tracks.append(type(s.edl.get_track("v2"))(
        id="v3", type="video", z=2, label="PIP 2"))
    pip = Clip(id="p", src="/x/a.mp4", in_=0, out=2, start=0.0)
    pip.mask, pip.framing = Mask(type="circle"), Framing(zoom=2.0)
    s.edl.get_track("v2").clips.append(pip)
    s.commit("seed2", {}, "seed2")

    dispatch(s, "move_clip", {"clip_id": "p", "new_track": "v3", "new_start": 0.0})
    _, c = s.edl.get_clip("p")
    assert c.mask is not None and c.mask.type == "circle"
    assert c.framing is not None and c.framing.zoom == pytest.approx(2.0)
