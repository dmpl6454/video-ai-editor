"""B3-panels lane regressions (QA-0xx pro-editor sweep, wave B)."""
from __future__ import annotations

import re

import pytest

from video_ai_editor.agent.prompt import planner as P
from video_ai_editor.agent.prompt.facts import TimelineFacts


def test_download_question_is_card_copy_without_tool_ids():
    """QA-063: the downloads card has Download / Skip buttons, so its question
    must not say "Reply download or skip", and it named dispatch tool ids
    ("… for auto_caption")."""
    f = TimelineFacts.minimal().with_(first_use={"madlad": 3_000_000_000, "whisper:large-v3": 3_100_000_000})
    p = P.plan("add hindi captions", f)
    q = p.needs_input[0]
    assert q.key == "downloads"
    assert "Reply" not in q.question
    assert not re.search(r"\bfor [a-z]+_[a-z_]+\b", q.question), q.question
    assert "GB" in q.question and q.question.endswith("?")


def test_native_save_prefills_the_export_dialogs_file_name(monkeypatch, tmp_path):
    """QA-100: the Export dialog's File name reaches the native Save-As box;
    the copied SOURCE is still the render in exports/."""
    import sys
    import types
    from video_ai_editor import desktop

    sd = tmp_path / "s_0123456789"
    (sd / "exports").mkdir(parents=True)
    (sd / "exports" / "export_abcd.mp4").write_bytes(b"x" * 10)
    monkeypatch.setattr(desktop, "session_path", lambda sid: sd)
    monkeypatch.setattr(desktop, "is_valid_session_id", lambda sid: True)
    asked = {}

    class _Win:
        def create_file_dialog(self, kind, save_filename=None):
            asked["name"] = save_filename
            return str(tmp_path / "out.mp4")

    fake = types.SimpleNamespace(windows=[_Win()], FileDialog=types.SimpleNamespace(SAVE="save"))
    monkeypatch.setitem(sys.modules, "webview", fake)
    api = desktop._Api("127.0.0.1", 8765)
    out = api.save_export("s_0123456789", "export_abcd.mp4", "My reel final")
    assert asked["name"] == "My reel final.mp4"
    assert (tmp_path / "out.mp4").read_bytes() == b"x" * 10 and out
    api.save_export("s_0123456789", "export_abcd.mp4", "../../evil/name.mp4")
    assert asked["name"] == "name.mp4"
    api.save_export("s_0123456789", "export_abcd.mp4")
    assert asked["name"] == "export_abcd.mp4"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient pinned at a tmp WORKDIR — never the owner's real projects."""
    from fastapi.testclient import TestClient
    from video_ai_editor import config, main as _main, storage as _storage
    from video_ai_editor.api.hardening import RATE
    for mod in (config, _storage, _main):
        monkeypatch.setattr(mod, "WORKDIR", tmp_path / "wd", raising=False)
    (tmp_path / "wd").mkdir()
    RATE.windows.clear()
    _main._STORES.clear()
    return TestClient(_main.app)


def test_export_download_takes_the_dialogs_file_name(client, tmp_path):
    """QA-100: GET …/files/exports/<render>?name=<File name> downloads under the
    user's name (the Content-Disposition decides it — an <a download> attribute
    cannot override a server filename). Only a bare leaf, extension kept."""
    from video_ai_editor.storage import session_dir

    c = client
    sid = c.post("/api/sessions", json={"name": "b3"}).json()["id"]
    exp = session_dir(sid) / "exports"
    assert str(exp).startswith(str(tmp_path))
    exp.mkdir(parents=True, exist_ok=True)
    (exp / "export_0123456789abcdef.mp4").write_bytes(b"\x00" * 16)
    url = f"/api/sessions/{sid}/files/exports/export_0123456789abcdef.mp4"
    from urllib.parse import unquote

    def saved_as(resp) -> str:
        cd = resp.headers["content-disposition"]
        return unquote(cd.split("filename*=utf-8''")[1]) if "filename*=" in cd else cd.split('filename="')[1].rstrip('"')

    r = c.get(url, params={"name": "My reel: final"})
    assert r.status_code == 200 and r.content == b"\x00" * 16
    assert saved_as(r) == "My reel final.mp4"
    assert saved_as(c.get(url, params={"name": "../../etc/passwd"})) == "passwd.mp4"
    assert saved_as(c.get(url, params={"name": "clip.mov"})) == "clip.mov.mp4"
    assert saved_as(c.get(url)) == "export_0123456789abcdef.mp4"


def test_features_report_says_which_tools_download_and_how_much(client, tmp_path, monkeypatch):
    """QA-065: /api/downloads names every first-run download with its size and
    whether it is already on disk, so the panels can ask before fetching GBs."""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("U2NET_HOME", str(empty))
    monkeypatch.setenv("TORCH_HOME", str(empty))
    monkeypatch.delenv("LAMA_MODEL", raising=False)
    from video_ai_editor.agent.prompt import facts
    monkeypatch.setattr(facts, "first_use_probe",
                        lambda backend: {"whisper:large-v3-turbo": 1_600_000_000, "madlad": 3_000_000_000})
    d = client.get("/api/downloads").json()["downloads"]
    assert d["captions:large-v3-turbo"] == {"what": "the fast caption model", "bytes": 1_600_000_000, "cached": False}
    assert d["captions:large-v3"]["cached"] is True
    assert d["translate"]["cached"] is False and d["translate"]["bytes"] >= 1_000_000_000
    assert d["bg_remove"]["cached"] is False and d["object_erase"]["cached"] is False
    (empty / "u2net.onnx").write_bytes(b"x")
    assert client.get("/api/downloads").json()["downloads"]["bg_remove"]["cached"] is True


def _y_code(path, t: float) -> int:
    """Frame index luma-coded by `_luma_clip` (Y = 16 + 3·n), read at `t`."""
    import subprocess
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t}", "-i", str(path), "-frames:v", "1",
         "-vf", "crop=iw/8:ih/8:iw*7/16:ih*7/16,extractplanes=y,scale=1:1:flags=area",
         "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    return round((raw[0] - 16) / 3)


def _luma_clip(dst, seconds: float = 3.0):
    import subprocess
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
         f"color=c=black:s=320x240:r=30:d={seconds},format=yuv420p,geq=lum='16+mod(N\\,64)*3':cb=128:cr=128",
         "-f", "lavfi", "-i", f"sine=f=440:d={seconds}", "-c:v", "libx264", "-crf", "5",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(dst)], check=True)


def test_preview_video_starts_at_zero_so_a_paused_seek_shows_the_playhead_frame(tmp_path, monkeypatch):
    """QA-077: the paused preview showed the frame BEFORE the playhead because
    the chunked preview's video track started ~21 ms late (an AAC priming
    offset from the concat mux), so a seek to n/30 landed on frame n-1. Pin the
    two-clip preview: video starts at 0 and the frame at n/30 is frame n."""
    import json as _json
    import subprocess
    from video_ai_editor.edl import EDLStore
    from video_ai_editor.edl.schema import EDL, Canvas, Clip, Track
    from video_ai_editor.render import render_preview

    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    _luma_clip(a)
    _luma_clip(b)
    edl = EDL(canvas=Canvas(w=320, h=240, fps=30), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(src=str(a), in_=0, out=3, start=0, id="c1"),
            Clip(src=str(b), in_=0, out=3, start=3, id="c2")])])
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)
    from video_ai_editor.render import compositor as _comp
    used: list[int] = []
    real = _comp._assemble_chunks_streamcopy
    monkeypatch.setattr(_comp, "_assemble_chunks_streamcopy",
                        lambda *a, **k: (used.append(1), real(*a, **k))[1])
    r = render_preview(store.edl, tmp_path, height=240)
    assert used, "the chunk concat path (where the offset came from) must be the one measured"
    probe = _json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,start_time", "-of", "json", str(r.path)],
        capture_output=True, text=True, check=True).stdout)["streams"]
    starts = {s["codec_type"]: float(s["start_time"]) for s in probe}
    assert abs(starts["video"]) < 1e-3, starts
    for n in (3, 33, 60):
        assert _y_code(r.path, n / 30) == n
    assert _y_code(r.path, 3 + 7 / 30) == 7          # into the second clip


# ---- QA-076 / QA-078 / QA-075: the text model ------------------------------

def _fixture_block():
    import json as _json
    from pathlib import Path as _P
    p = _P(__file__).resolve().parents[1] / "frontend" / "src" / "lib" / "__fixtures__" / "text_layout_cases.json"
    return _json.loads(p.read_text(encoding="utf-8"))["block"]


def test_text_block_rules_match_the_contract():
    """Rule 7 (QA-078/075) on the export side — the preview side asserts the
    same fixture in frontend/src/lib/textBlock.test.ts."""
    from video_ai_editor.edl.schema import EDL, Canvas, CaptionsConfig, Track
    from video_ai_editor.render import text_overlay as T
    B = _fixture_block()
    assert (T.BG_PAD_X_RATIO, T.BG_PAD_Y_RATIO, T.BG_RADIUS_RATIO, T.ANIM_DUR) == (
        B["BG_PAD_X_RATIO"], B["BG_PAD_Y_RATIO"], B["BG_RADIUS_RATIO"], B["ANIM_DUR"])
    for c in B["line_x"]:
        assert T.line_x(c["align"], c["anchor_x"], c["block_w"], c["w"]) == pytest.approx(c["x"])
    for c in B["background"]:
        assert list(T.background_rect(c["anchor_x"], c["centers"], c["block_w"], c["size"], c["spacing"])) == pytest.approx(c["rect"])
    for c in B["caption_positions"]:
        e = EDL(canvas=Canvas(w=c["w"], h=c["h"]),
                tracks=[Track(id="captions", type="captions", config=CaptionsConfig(position=c["position"]))])
        assert T.block_anchor_y("caption", T.caption_position_y(e), c["h"], c["w"]) == pytest.approx(c["y"])


def test_v2_project_migrates_to_what_it_rendered():
    """QA-076: loading a v2 EDL turns its sentinels into real values — font
    "Inter-Black" (= role font) → None, the old add_text y = 0.85·h (= role
    anchor) → the role anchor — so it renders exactly as before, and the file
    is v3 from the next commit on."""
    import json as _json
    from video_ai_editor.edl.schema import EDL, EDL_VERSION
    from video_ai_editor.render import text_overlay as T
    v2 = {"version": 2, "canvas": {"w": 1920, "h": 1080, "fps": 30},
          "tracks": [{"id": "text", "type": "text", "clips": [
              {"id": "t_a", "text": "A", "start": 0, "end": 1, "role": "hook",
               "style": {"font": "Inter-Black"}, "transform": {"x": 960, "y": 918}},
              {"id": "t_b", "text": "B", "start": 0, "end": 1,
               "style": {"font": "Anton-Regular"}, "transform": {"x": 300, "y": 919}}]}]}
    e = EDL.model_validate_json(_json.dumps(v2))
    a, b = e.tracks[0].clips
    assert e.version == EDL_VERSION == 3
    assert a.style.font is None and a.transform.y == pytest.approx(T._y_for_role("hook", None, 1080, 1920))
    assert b.style.font == "Anton-Regular" and b.transform.y == 919 and b.transform.x == 300
    # a current (v3) EDL is never rewritten
    again = EDL.model_validate_json(e.to_json())
    assert again.tracks[0].clips[1].transform.y == 919


def _ink_rows(img):
    import numpy as np
    a = np.asarray(img.getchannel("A"))
    ys = np.nonzero(a.any(axis=1))[0]
    return int(ys[0]), int(ys[-1])


def test_typed_y_is_honoured_one_pixel_at_a_time(tmp_path):
    """QA-076: on 1920×1080, y=918 rendered at the role anchor (ink 789-835)
    and y=919 at 898-944 — a 1 px edit moved the text ~109 px."""
    from video_ai_editor.edl.schema import EDL, Canvas, TextClip, Track, Transform
    from video_ai_editor.render.text_overlay import cache_text_pngs
    from PIL import Image
    tops = {}
    for y in (918, 919):
        e = EDL(canvas=Canvas(w=1920, h=1080), tracks=[Track(id="text", type="text", clips=[
            TextClip(text="HELLO", start=0, end=1, transform=Transform(x=960, y=y))])])
        (_, _, png), = cache_text_pngs(e, tmp_path / f"c{y}")
        with Image.open(png) as im:
            top, bottom = _ink_rows(im)
        tops[y] = (top, bottom)
        assert abs((top + bottom) / 2 - y) < 20, (y, top, bottom)
    assert tops[919][0] - tops[918][0] in (0, 1, 2)


def test_inter_black_can_be_chosen_on_any_role():
    """QA-076: "Inter-Black" was the unset sentinel, so picking it did nothing."""
    from video_ai_editor.edl.schema import TextClip, TextStyle
    from video_ai_editor.render.text_overlay import resolve_style_overrides
    c = TextClip(text="x", start=0, end=1, role="hook", style=TextStyle(font="Inter-Black"))
    _fill, font = resolve_style_overrides(c, "hook")
    assert font is not None and font.name == "Inter-Black.ttf"
    assert resolve_style_overrides(TextClip(text="x", start=0, end=1, role="hook"), "hook")[1] is None


def test_background_box_alignment_and_spacing_render():
    """QA-078: a boxed, left-aligned, double-spaced two-line block on the
    export side: the box is drawn in its colour around the ink, both lines
    start at the same left edge, and the line gap doubles."""
    import numpy as np
    from video_ai_editor.render.text_overlay import render_text_png, background_rect, line_centers
    W, H = 1920, 1080
    kw = dict(anchor_x=960, anchor_y=540, size=80)
    plain = render_text_png("WIDE WIDE WIDE\nNARROW", "default", W, H, **kw)
    boxed = render_text_png("WIDE WIDE WIDE\nNARROW", "default", W, H, background=(0, 0, 255, 255),
                            align="left", **kw)
    a = np.asarray(boxed)
    # a pixel inside the box's padding (left of the ink) is the box colour
    px = np.asarray(plain.getchannel("A"))
    xs = np.nonzero(px.any(axis=0))[0]
    assert tuple(a[540, int(xs[0]) - 10]) == (0, 0, 255, 255)
    # left alignment: the two lines' ink starts at the same x
    ink = (np.asarray(boxed.convert("RGB")) > 200).all(axis=2)
    def first_x(y0, y1):
        cols = np.nonzero(ink[y0:y1].any(axis=0))[0]
        return int(cols[0])
    c1, c2 = line_centers(540, 2, 80)
    assert abs(first_x(int(c1) - 30, int(c1) + 10) - first_x(int(c2) - 30, int(c2) + 10)) <= 6
    spaced = line_centers(540, 2, 80, 2.0)
    assert spaced[1] - spaced[0] == pytest.approx(2 * (c2 - c1))


def test_caption_position_moves_the_cues(tmp_path):
    """QA-075: the captions track's position (top/center/bottom) was accepted
    and never rendered — cues always sat at the bottom anchor."""
    from video_ai_editor.edl.schema import EDL, Canvas, CaptionsConfig, TextClip, Track
    from video_ai_editor.render.text_overlay import cache_text_pngs
    from PIL import Image
    mids = {}
    for pos in ("bottom", "center", "top"):
        e = EDL(canvas=Canvas(w=1080, h=1920), tracks=[Track(id="captions", type="captions",
                config=CaptionsConfig(position=pos), clips=[TextClip(text="hello there", start=0, end=1, role="caption")])])
        (_, _, png), = cache_text_pngs(e, tmp_path / pos)
        with Image.open(png) as im:
            t, b = _ink_rows(im)
        mids[pos] = (t + b) / 2
    assert abs(mids["bottom"] - 1920 * 0.76) < 30
    assert abs(mids["center"] - 960) < 30
    assert abs(mids["top"] - 1920 * 0.14) < 30


def test_animation_length_reaches_the_filtergraph(tmp_path):
    """QA-078: every animation lasted 0.35 s; `anim_dur` now sets it."""
    from video_ai_editor.edl.schema import EDL, Canvas, TextClip, Track
    from video_ai_editor.render.text_overlay import build_overlay_chain
    e = EDL(canvas=Canvas(w=320, h=240), tracks=[Track(id="text", type="text", clips=[
        TextClip(text="HI", start=0, end=6, anim_in="fade", anim_dur=1.2)])])
    chain, _, _ = build_overlay_chain(e, tmp_path / "cache", source_label="[v]", out_label="[vout]",
                                      first_input_index=1, out_w=320, out_h=240)
    assert "fade=t=in:st=0.000:d=1.200" in chain, chain


def test_set_caption_style_restyles_every_cue_in_one_step(tmp_path):
    """QA-075: caption look and position from one tool, one undo step, hand
    edits kept; a later caption rebuild keeps the look."""
    from video_ai_editor.edl import EDLStore
    from video_ai_editor.edl.schema import EDL, Canvas, CaptionsConfig, TextClip, Track
    from video_ai_editor.agent.dispatch import dispatch
    cues = [TextClip(id=f"t_c{i}", text=f"line {i}", start=i, end=i + 1, role="caption") for i in range(3)]
    cues[1].text = "hand edited"
    edl = EDL(canvas=Canvas(w=1080, h=1920), tracks=[
        Track(id="captions", type="captions", config=CaptionsConfig(enabled=True), clips=cues)])
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)
    r = dispatch(store, "set_caption_style", {"position": "top", "color": "#FFD400", "background": "#000000B3",
                                              "font": "Anton-Regular"})
    cap = store.edl.get_track("captions")
    assert cap.config.position == "top" and r["cues"] == 3
    for c in cap.clips:
        assert (c.style.color, c.style.background, c.style.font) == ("#FFD400", "#000000B3", "Anton-Regular")
    assert cap.clips[1].text == "hand edited"
    # one undo step restores every cue
    dispatch(store, "undo", {})
    cap = store.edl.get_track("captions")
    assert all(c.style.color == "#FFFFFF" for c in cap.clips) and cap.config.position == "bottom"
    dispatch(store, "redo", {})
    cap = store.edl.get_track("captions")
    # position-only change keeps the look
    dispatch(store, "set_caption_style", {"position": "center"})
    assert cap.clips[0].style.color == "#FFD400"
    # null clears back to the role style
    dispatch(store, "set_caption_style", {"background": None})
    assert all(c.style.background is None for c in cap.clips)
    import pytest as _pt
    with _pt.raises(ValueError):
        dispatch(store, "set_caption_style", {"color": "yellow"})


def test_add_text_takes_the_block_style_in_one_op(tmp_path):
    """QA-078: a styled template (boxed, left-aligned, spaced, no shadow, a 1 s
    fade) is ONE add_text — not add_text plus four set_property undo steps."""
    from video_ai_editor.edl import EDLStore
    from video_ai_editor.edl.schema import EDL, Canvas
    from video_ai_editor.agent.dispatch import dispatch
    (tmp_path / "edl.json").write_text(EDL(canvas=Canvas(w=1080, h=1920)).model_dump_json())
    store = EDLStore(tmp_path)
    r = dispatch(store, "add_text", {"text": "Boxed", "start": 0, "end": 3, "background": "#000000B3",
                                     "align": "Left", "line_spacing": 1.4, "shadow_on": False,
                                     "anim_in": "fade", "anim_dur": 9})
    c = store.edl.get_clip(r["id"])[1]
    assert (c.style.background, c.style.align, c.style.line_spacing, c.style.shadow_on) == ("#000000B3", "left", 1.4, False)
    assert c.anim_dur == 3.0                      # clamped to the model's range
    assert c.style.font is None                   # role font unless chosen
    from video_ai_editor.render.text_overlay import _y_for_role
    assert c.transform.y == _y_for_role("default", None, 1920, 1080)   # renders where it says
