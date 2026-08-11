"""Round-7 tester findings: sticker stacking, clip reordering, text emoji.

Each was reported as "it doesn't work" and each had a different shape — a
deliberate-but-wrong default, a missing opt-in, and a silent strip.
"""
from __future__ import annotations
import subprocess
import tempfile
from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Canvas, Sticker, TextClip, empty_edl


def _store(tmp_path: Path) -> EDLStore:
    (tmp_path / "edl.json").write_text(
        empty_edl(Canvas(w=1080, h=1920, fps=30)).model_dump_json())
    return EDLStore(tmp_path)


def _mk_video(path: Path, dur: float = 6.0) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=30:duration={dur}",
         "-f", "lavfi", "-i", f"sine=f=440:duration={dur}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(path)], check=True, capture_output=True)
    return path


def _mk_png(path: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "color=c=red:s=64x64:d=1", "-frames:v", "1", str(path)],
        check=True, capture_output=True)
    return path


# --------------------------------------------------- dragging a sticker forward

def test_set_clip_transform_can_raise_a_sticker_in_the_same_commit(tmp_path):
    """Stacking is (track_z, clip_z, start), so with every sticker at the
    default z=0 the newest-added always won and dragging an older one on top
    of it changed nothing ("the latest emoji still overlaps the emoji that was
    earlier selected"). The raise has to ride along on the SAME op as the move
    — two dispatches means one Undo reverts the raise and leaves it moved.
    """
    store = _store(tmp_path)
    png = _mk_png(tmp_path / "s.png")
    track = store.edl.get_track("stickers")
    old = Sticker(src=str(png), start=0.0, end=5.0, id="st_old")
    new = Sticker(src=str(png), start=0.0, end=5.0, id="st_new")
    track.clips.extend([old, new])
    store.commit("seed", {}, "two stickers")
    assert old.z == 0 and new.z == 0

    ops_before = len(store.ops.ops)
    dispatch(store, "set_clip_transform",
             {"clip_id": "st_old", "x": 500, "y": 900, "raise_to_front": True})

    _, moved = store.edl.get_clip("st_old")
    _, other = store.edl.get_clip("st_new")
    assert moved.z > other.z, "the dragged sticker must end up on top"
    assert moved.transform.x == 500 and moved.transform.y == 900
    assert len(store.ops.ops) == ops_before + 1, "move + raise must be ONE undo step"


def test_transform_without_the_flag_never_restacks(tmp_path):
    """Opt-in: a plain set_clip_transform (Properties panel, Claude, MCP) must
    not silently override an explicit Send-to-back."""
    store = _store(tmp_path)
    png = _mk_png(tmp_path / "s.png")
    sk = Sticker(src=str(png), start=0.0, end=5.0, id="st_a")
    sk.z = -3
    store.edl.get_track("stickers").clips.append(sk)
    store.commit("seed", {}, "one sticker")

    dispatch(store, "set_clip_transform", {"clip_id": "st_a", "x": 10, "y": 20})
    _, after = store.edl.get_clip("st_a")
    assert after.z == -3


# ------------------------------------------------------------------ keyframes

def _kf_store(tmp_path: Path) -> tuple[EDLStore, str]:
    store = _store(tmp_path)
    store.edl.get_track("tx_super").clips.append(
        TextClip(text="hi", start=0.0, end=5.0, role="super", id="t_kf"))
    store.commit("seed", {}, "seed")
    return store, "t_kf"


KF_ALL = ["scale", "rotation", "opacity", "x", "y"]


def test_one_click_keys_the_whole_transform_in_one_commit(tmp_path):
    """The panel had five keyframe diamonds, one per property ("I don't
    understand why there are 5 buttons") — now one button that pins the whole
    pose. It has to be ONE op: five dispatches would be five undo steps, and an
    Undo could leave the clip keyed on some properties and not others.
    """
    store, cid = _kf_store(tmp_path)
    before = len(store.ops.ops)

    dispatch(store, "add_keyframe", {"clip_id": cid, "props": KF_ALL, "time": 1.5})

    assert len(store.ops.ops) == before + 1, "one click must be one undo step"
    _, c = store.edl.get_clip(cid)
    for p in KF_ALL:
        v = getattr(c.transform, p)
        assert hasattr(v, "keyframes"), f"{p} not keyed"
        # Exactly ONE key per press. The single-prop form seeds an extra anchor
        # at t=0 (so `add_keyframe scale @2s` ramps from the current value, the
        # contract Claude/MCP callers rely on); the props form must not, or one
        # click would report "2 keys" and a tester would count more keyframes
        # than clicks.
        assert [k[0] for k in v.keyframes] == pytest.approx([1.5])


def test_a_prop_left_out_of_values_keeps_what_it_already_reads(tmp_path):
    """Keying an untouched property must PIN it, not jump it to a default —
    otherwise pressing the button would visibly change the clip."""
    store, cid = _kf_store(tmp_path)
    dispatch(store, "set_clip_transform", {"clip_id": cid, "scale": 1.75, "opacity": 0.4})

    dispatch(store, "add_keyframe", {"clip_id": cid, "props": ["scale", "opacity"],
                                     "time": 2.0})

    _, c = store.edl.get_clip(cid)
    assert c.transform.scale.keyframes[0][1] == pytest.approx(1.75)
    assert c.transform.opacity.keyframes[0][1] == pytest.approx(0.4)


def test_removing_the_last_key_restores_its_value_not_zero(tmp_path):
    """Dropping the only keyframe collapsed the property to 0.0 — scale 0.0
    (clamped by Transform to 0.01) and opacity 0.0, i.e. the clip VANISHED.
    A lone keyframe is a constant, so removing it must leave the clip looking
    exactly as it did. Survivable when five buttons each removed one property;
    with one button it is two clicks from a blank clip.
    """
    store, cid = _kf_store(tmp_path)
    dispatch(store, "add_keyframe", {"clip_id": cid, "props": KF_ALL, "time": 0.0,
                                     "values": {"scale": 1.0, "opacity": 1.0,
                                                "rotation": 0.0, "x": 100.0, "y": 200.0}})

    dispatch(store, "remove_keyframe", {"clip_id": cid, "props": KF_ALL, "time": 0.0})

    _, c = store.edl.get_clip(cid)
    assert c.transform.scale == pytest.approx(1.0), "clip shrank to nothing"
    assert c.transform.opacity == pytest.approx(1.0), "clip went invisible"
    assert c.transform.x == pytest.approx(100.0)
    assert c.transform.y == pytest.approx(200.0)


def test_removing_where_there_is_no_key_commits_nothing(tmp_path):
    """A stray click must not cost the user their redo history: commit() clears
    the redo stack, so a no-op op is not free."""
    store, cid = _kf_store(tmp_path)
    dispatch(store, "add_keyframe", {"clip_id": cid, "props": KF_ALL, "time": 0.0})
    before = len(store.ops.ops)

    r = dispatch(store, "remove_keyframe", {"clip_id": cid, "props": KF_ALL, "time": 3.3})

    assert len(store.ops.ops) == before, "no-op removal must not commit"
    assert r["keys"] == 0


def test_single_prop_keyframe_calls_still_work(tmp_path):
    """Claude, MCP and every saved project use the single-`prop` form."""
    store, cid = _kf_store(tmp_path)
    dispatch(store, "add_keyframe", {"clip_id": cid, "prop": "scale",
                                     "time": 0.0, "value": 1.0})
    dispatch(store, "add_keyframe", {"clip_id": cid, "prop": "scale",
                                     "time": 2.0, "value": 2.0})
    _, c = store.edl.get_clip(cid)
    assert [k[1] for k in c.transform.scale.keyframes] == pytest.approx([1.0, 2.0])

    dispatch(store, "remove_keyframe", {"clip_id": cid, "prop": "scale", "time": 2.0})
    _, c = store.edl.get_clip(cid)
    assert c.transform.scale == pytest.approx(1.0)


def test_keyframe_rejects_an_unknown_property(tmp_path):
    store, cid = _kf_store(tmp_path)
    with pytest.raises(ValueError):
        dispatch(store, "add_keyframe", {"clip_id": cid, "props": ["scale", "wat"],
                                         "time": 0.0})
    with pytest.raises(ValueError):
        dispatch(store, "add_keyframe", {"clip_id": cid, "time": 0.0})


# ------------------------------------------------------ reordering a media clip

def test_move_clip_close_gap_pulls_the_remaining_clips_left(tmp_path):
    """Dragging clip 1 to the end left its slot empty and made the timeline
    longer ("the video duration got increased and left an empty space at the
    start"). close_gap reorders instead of relocating."""
    store = _store(tmp_path)
    src = _mk_video(tmp_path / "v.mp4", 6.0)
    dispatch(store, "add_clip", {"src": str(src), "track": "v1",
                                 "in": 0.0, "out": 6.0, "start": 0.0})
    dispatch(store, "split_at", {"track": "v1", "time": 2.0})
    first = store.edl.get_track("v1").clips[0].id
    before = store.edl.duration

    dispatch(store, "move_clip", {"clip_id": first, "new_start": 6.0, "close_gap": True})

    clips = store.edl.get_track("v1").clips
    assert clips[0].start == pytest.approx(0.0), "no hole at the head"
    assert clips[1].start == pytest.approx(4.0)
    assert clips[1].id == first, "the dragged clip ends up last"
    assert store.edl.duration == pytest.approx(before), "timeline must not grow"


def test_move_clip_without_close_gap_is_unchanged(tmp_path):
    """The default stays a plain move: apply_template / b-roll callers place
    clips at absolute times and must not have neighbours shuffle underneath."""
    store = _store(tmp_path)
    src = _mk_video(tmp_path / "v.mp4", 6.0)
    dispatch(store, "add_clip", {"src": str(src), "track": "v1",
                                 "in": 0.0, "out": 6.0, "start": 0.0})
    dispatch(store, "split_at", {"track": "v1", "time": 2.0})
    first = store.edl.get_track("v1").clips[0].id

    dispatch(store, "move_clip", {"clip_id": first, "new_start": 6.0})
    starts = sorted(c.start for c in store.edl.get_track("v1").clips)
    assert starts == pytest.approx([2.0, 6.0]), "the gap at 0-2s must remain"


# -------------------------------------------------------------- emoji in text

def _colourful(im) -> int:
    """Saturated pixel count. Text is white-on-black, so anything saturated
    can only be emoji artwork."""
    return sum(n for n, (r, g, b) in (im.convert("RGB").getcolors(1 << 20) or [])
               if max(r, g, b) - min(r, g, b) > 40)


def test_emoji_in_text_render_as_artwork_not_nothing():
    """Text was pushed through _strip_emoji before drawing, so an emoji typed
    into a text clip silently vanished from preview AND export ("I was unable
    to apply the emojis through the text section")."""
    from video_ai_editor.render.text_overlay import render_text_png

    plain = render_text_png("BIG SALE", "super", 1080, 1920)
    withemoji = render_text_png("\U0001F525 BIG SALE \U0001F525", "super", 1080, 1920)

    assert _colourful(plain) == 0
    assert _colourful(withemoji) > 500, "emoji artwork missing from the render"
    # Wider too: the emoji occupy real layout space rather than being painted
    # on top of the words.
    assert withemoji.getchannel("A").getbbox()[2] > plain.getchannel("A").getbbox()[2]


def _bands(im):
    """(text rows, emoji rows). Text is white with a black outline, so a
    saturated pixel can only be emoji artwork and a near-white one only text."""
    rgb, alpha = im.convert("RGB"), im.getchannel("A")
    px, ax = rgb.load(), alpha.load()
    trow, erow = [], []
    for y in range(im.height):
        t = e = 0
        for x in range(im.width):
            if ax[x, y] < 30:
                continue
            r, g, b = px[x, y]
            if max(r, g, b) - min(r, g, b) > 40:
                e += 1
            elif r > 200 and g > 200 and b > 200:
                t += 1
        if t:
            trow.append(y)
        if e:
            erow.append(y)
    return trow, erow


def test_emoji_sit_on_the_same_band_as_the_text():
    """"The emoji are not aligned with the text." Placement was a fixed
    fraction of the em box, but where the cap band sits inside the em box is a
    per-font property — and the two renderers start from different origins
    (Pillow from the ascender top, canvas from the em-box middle), so ONE
    constant could not be right for both: the bake sat 26.5px high while the
    preview sat low. Both now centre on the measured cap band.
    """
    from video_ai_editor.render.text_overlay import render_text_png

    for text in ("GG\U0001F60A\U0001F602", "Hello \U0001F525 world"):
        t, e = _bands(render_text_png(text, "super", 1080, 1920))
        assert t and e, f"missing band for {text!r}"
        t_mid, e_mid = (t[0] + t[-1]) / 2, (e[0] + e[-1]) / 2
        # Tolerance is a couple of px: integer paste coords + antialiasing.
        assert abs(e_mid - t_mid) <= 3, (
            f"{text!r}: emoji centre {e_mid} vs text centre {t_mid}")


def _col_runs(im):
    """Visible pixel groups across x — one per glyph cluster/emoji."""
    a = im.getchannel("A")
    px = a.load()
    cols = [any(px[x, y] > 30 for y in range(im.height)) for x in range(im.width)]
    runs, x = [], 0
    while x < im.width:
        if cols[x]:
            s = x
            while x < im.width and cols[x]:
                x += 1
            runs.append((s, x - 1))
        else:
            x += 1
    return runs


def test_wrapping_does_not_invent_spaces_between_emoji():
    """"The space between the emoji is too much." Not a styling choice — the
    wrapper made every emoji its own wrap-word (it must, or the line overflows
    by each emoji's box) and then rejoined words with a hard " ". So "GG🔥🔥"
    was LAID OUT as "GG 🔥 🔥": spaces nobody typed, 0.32em apart against a
    0.24em word space. Splitting a string to measure it must not change it.
    """
    from video_ai_editor.render.text_overlay import ROLE_STYLES, render_text_png

    size = ROLE_STYLES["super"]["size"]
    runs = _col_runs(render_text_png(
        "GG\U0001F60A\U0001F602\U0001F606", "super", 1080, 1920))
    assert len(runs) == 4, f"expected GG + 3 emoji, got {len(runs)} groups"
    gaps = [(runs[i][0] - runs[i - 1][1] - 1) / size for i in range(2, 4)]
    assert all(g < 0.15 for g in gaps), f"emoji still spaced apart: {gaps}"


def test_a_space_the_writer_typed_survives():
    """The other half of the same contract: only INVENTED spaces go away."""
    from video_ai_editor.render.text_overlay import ROLE_STYLES, render_text_png

    size = ROLE_STYLES["super"]["size"]
    runs = _col_runs(render_text_png("A \U0001F525 B", "super", 1080, 1920))
    assert len(runs) == 3, f"expected A, emoji, B — got {len(runs)}"
    gaps = [(runs[i][0] - runs[i - 1][1] - 1) / size for i in (1, 2)]
    assert all(g > 0.15 for g in gaps), f"typed spaces were swallowed: {gaps}"


def test_emoji_wrap_units_record_what_the_source_had():
    from video_ai_editor.render.text_overlay import _emoji_words

    assert _emoji_words("GG\U0001F60A\U0001F602") == [
        (False, "GG"), (True, "\U0001F60A"), (True, "\U0001F602")]
    assert _emoji_words("A \U0001F525 B") == [
        (False, "A"), (False, "\U0001F525"), (False, "B")]
    # Leading emoji, and a word glued to the emoji before it.
    assert _emoji_words("\U0001F525SALE now") == [
        (False, "\U0001F525"), (True, "SALE"), (False, "now")]


def test_emoji_only_text_still_renders():
    """`_strip_emoji` reduced a pure-emoji clip to "" and returned a blank
    canvas — the most visible form of the bug."""
    from video_ai_editor.render.text_overlay import render_text_png
    im = render_text_png("\U0001F60D\U0001F929", "super", 1080, 1920)
    assert im.getchannel("A").getbbox() is not None


def test_adjacent_emoji_split_into_separate_artwork():
    """The emoji regex ends in `+`, so "(heart-eyes)(star-struck)" arrived as
    ONE token, was looked up as codepoints `1f60d-1f929`, found no artwork and
    drew nothing. Real multi-codepoint emoji must still stay whole."""
    from video_ai_editor.render.text_overlay import _emoji_clusters
    assert _emoji_clusters("\U0001F60D\U0001F929") == ["\U0001F60D", "\U0001F929"]
    assert _emoji_clusters(
        "\U0001F1EE\U0001F1F3\U0001F468‍\U0001F4BB❤️"
    ) == ["\U0001F1EE\U0001F1F3", "\U0001F468‍\U0001F4BB", "❤️"]


def test_text_png_cache_key_distinguishes_emoji():
    """The key used the emoji-STRIPPED text, so "SALE" and "(fire) SALE" hashed
    the same and the second would have been served the first's PNG forever."""
    from video_ai_editor.render.text_overlay import cache_text_pngs

    edl = empty_edl(Canvas(w=320, h=180, fps=30))
    tr = edl.get_track("tx_super")
    tr.clips.append(TextClip(text="SALE", start=0.0, end=1.0, role="super"))
    tr.clips.append(TextClip(text="\U0001F525 SALE", start=1.0, end=2.0, role="super"))
    with tempfile.TemporaryDirectory() as d:
        paired = cache_text_pngs(edl, Path(d))
        names = {p.name for _, _, p in paired}
    assert len(names) == 2, f"emoji-only difference collapsed to one entry: {names}"


# ------------------------------------------------------------ emoji artwork API

def test_emoji_route_rejects_anything_that_is_not_codepoints():
    """The route turns the path segment back into characters — it must never
    accept something that could name a file."""
    from fastapi.testclient import TestClient
    from video_ai_editor import main as m

    client = TestClient(m.app)
    for bad in ("zzz", "1f60d.png", "-", "1f60d-", "%2e%2e%2fetc"):
        r = client.get(f"/api/emoji/{bad}.png")
        assert r.status_code in (400, 404), f"{bad!r} -> {r.status_code}"
