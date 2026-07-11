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


def test_crf_override_ignored_for_hw_encoder(monkeypatch):
    """HW encoders (nvenc/qsv/amf/videotoolbox) keep their own quality ladder;
    crf is a libx264-only knob per the plan's explicit scope."""
    monkeypatch.setattr(c, "_usable_encoder", lambda name: name == "h264_nvenc")
    args = c._video_encoder_args(preview=False, crf=28)
    assert "h264_nvenc" in args
    assert "-crf" not in args
    assert "-cq" in args  # untouched nvenc quality knob


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
