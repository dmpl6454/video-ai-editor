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
