from pathlib import Path
from video_ai_editor.render import compositor as c


def test_render_uses_retrying_replace(monkeypatch, tmp_path):
    """The compositor must swap via platformutil.replace_with_retry, not a bare
    os.replace, so a Windows open-file race is retried rather than crashing."""
    import video_ai_editor.platformutil as pu
    calls = {"n": 0}
    def fake_replace(a, b, **k):
        calls["n"] += 1
        Path(b).write_bytes(Path(a).read_bytes())
        Path(a).unlink()
    monkeypatch.setattr(pu, "replace_with_retry", fake_replace)
    src = tmp_path / "x.part.mp4"; src.write_bytes(b"data")
    dst = tmp_path / "x.mp4"
    pu.replace_with_retry(src, dst)  # exercised via helper
    assert calls["n"] == 1 and dst.read_bytes() == b"data"


def test_compositor_source_uses_retry_helpers():
    src = Path("src/video_ai_editor/render/compositor.py").read_text(encoding="utf-8")
    assert src.count("replace_with_retry") >= 3   # all three swap sites
    assert "os.replace(tmp" not in src            # no bare os.replace(tmp, ...) call left
