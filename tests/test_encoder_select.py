from pathlib import Path

from video_ai_editor.edl.schema import empty_edl
from video_ai_editor.render import compositor as c


def test_pick_encoder_prefers_first_usable(monkeypatch):
    """When a hardware encoder probes usable, its args are returned; order is
    videotoolbox → nvenc → qsv → amf → libx264."""
    monkeypatch.setattr(c, "_usable_encoder",
                        lambda name: name == "h264_nvenc")
    args = c._video_encoder_args(preview=False)
    assert "h264_nvenc" in args
    assert "-cq" in args  # nvenc quality knob


def test_pick_encoder_falls_back_to_libx264(monkeypatch):
    monkeypatch.setattr(c, "_usable_encoder", lambda name: False)
    args = c._video_encoder_args(preview=True)
    assert args[:2] == ["-c:v", "libx264"]


def test_libx264_uses_ultrafast_preview(monkeypatch):
    monkeypatch.setattr(c, "_usable_encoder", lambda name: False)
    assert "ultrafast" in c._video_encoder_args(preview=True)
    assert "medium" in c._video_encoder_args(preview=False)


def test_crf_override_replaces_default_libx264_crf(monkeypatch):
    """An explicit crf= must replace the hardcoded -crf value for libx264,
    not just get ignored (the Task 11b dead-code bug)."""
    monkeypatch.setattr(c, "_usable_encoder", lambda name: False)
    args = c._video_encoder_args(preview=False, crf=28)
    assert "-crf" in args
    assert args[args.index("-crf") + 1] == "28"


def test_crf_none_keeps_existing_default(monkeypatch):
    """No crf override (crf=None, the default) must preserve prior behavior
    exactly — existing preview/export quality is unaffected."""
    monkeypatch.setattr(c, "_usable_encoder", lambda name: False)
    args = c._video_encoder_args(preview=False)
    assert args[args.index("-crf") + 1] == "20"
    args_preview = c._video_encoder_args(preview=True)
    assert args_preview[args_preview.index("-crf") + 1] == "30"


def test_crf_override_maps_onto_hw_encoder_quality_knob(monkeypatch):
    """HW encoders (nvenc/qsv/amf/videotoolbox) don't take -crf, but an
    explicit crf override must still change their quality knob — otherwise
    the export Quality selector is a silent no-op on Mac/Windows HW-encoder
    exports (Task 2c). crf is mapped onto each encoder's own knob
    (-q:v / -cq / -global_quality / -qp)."""
    monkeypatch.setattr(c, "_usable_encoder", lambda name: name == "h264_nvenc")
    args = c._video_encoder_args(preview=False, crf=28)
    assert "h264_nvenc" in args
    assert "-crf" not in args
    assert "-cq" in args
    cq_default = args[args.index("-cq") + 1]

    args_hi = c._video_encoder_args(preview=False, crf=18)
    cq_hi = args_hi[args_hi.index("-cq") + 1]
    assert cq_hi != cq_default


def test_hw_encoder_args_default_unchanged_when_crf_none(monkeypatch):
    """crf=None (the default, e.g. preview renders) must reproduce the exact
    prior hardcoded per-encoder values byte-for-byte — no regressions to
    existing preview/export quality when the caller doesn't ask for a
    specific crf."""
    assert c._hw_encoder_args("h264_videotoolbox", preview=False, crf=None) == [
        "-c:v", "h264_videotoolbox", "-q:v", "48", "-allow_sw", "1",
        "-realtime", "0", "-pix_fmt", "yuv420p",
    ]
    assert c._hw_encoder_args("h264_videotoolbox", preview=True, crf=None) == [
        "-c:v", "h264_videotoolbox", "-q:v", "60", "-allow_sw", "1",
        "-realtime", "1", "-pix_fmt", "yuv420p",
    ]
    assert c._hw_encoder_args("h264_nvenc", preview=False, crf=None) == [
        "-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq",
        "-rc", "vbr", "-cq", "21", "-b:v", "0", "-pix_fmt", "yuv420p",
    ]


def test_videotoolbox_crf_to_qv_inverse_mapping(monkeypatch):
    """VideoToolbox -q:v is 0-100, HIGHER=better (opposite of x264 crf, where
    lower=better). A lower crf (higher quality request) must map to a HIGHER
    -q:v, and the mapping must be clamped to [0, 100]."""
    q18 = c._hw_encoder_args("h264_videotoolbox", preview=False, crf=18)
    q23 = c._hw_encoder_args("h264_videotoolbox", preview=False, crf=23)
    q28 = c._hw_encoder_args("h264_videotoolbox", preview=False, crf=28)

    def qv(args):
        return int(args[args.index("-q:v") + 1])

    assert qv(q18) > qv(q23) > qv(q28)
    # Extreme crf values must stay within ffmpeg's valid 0-100 range.
    q_extreme_lo = qv(c._hw_encoder_args("h264_videotoolbox", preview=False, crf=0))
    q_extreme_hi = qv(c._hw_encoder_args("h264_videotoolbox", preview=False, crf=51))
    assert 0 <= q_extreme_lo <= 100
    assert 0 <= q_extreme_hi <= 100


def test_video_encoder_args_threads_crf_into_hw_encoder(monkeypatch):
    """_video_encoder_args (the public entry point _render calls) must pass
    crf through to _hw_encoder_args for the HW branch too, not only libx264."""
    monkeypatch.setattr(c, "_usable_encoder",
                        lambda name: name == "h264_videotoolbox")
    args_18 = c._video_encoder_args(preview=False, crf=18)
    args_28 = c._video_encoder_args(preview=False, crf=28)
    qv18 = int(args_18[args_18.index("-q:v") + 1])
    qv28 = int(args_28[args_28.index("-q:v") + 1])
    assert qv18 > qv28


def test_render_export_threads_crf_into_render(monkeypatch, tmp_path):
    """render_export must forward its crf arg all the way down to _render,
    not silently drop it (the Task 11b dead-code bug)."""
    captured = {}

    def fake_render(edl, dst, *, height, fps, preview, cache_dir=None,
                     on_progress=None, cancel_event=None, crf=None):
        captured["crf"] = crf
        captured["preview"] = preview
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"fake")
        return dst

    monkeypatch.setattr(c, "_render", fake_render)
    edl = empty_edl()
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    result = c.render_export(edl, session_dir, crf=28)
    assert result.path.exists()
    assert captured["crf"] == 28
    assert captured["preview"] is False
