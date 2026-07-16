"""main.py's _render_failure_message — distinguishes an app-generated overlay
PNG crash from a genuinely corrupt user media file (R2).

Before this fix, EVERY render_failed 422 said "this clip... may have corrupt
frames or an unusual codec" even when the actual failing ffmpeg input was our
own rasterized sticker/text/mask cache PNG — which read as "your media/music
upload is broken" when it wasn't.
"""
from __future__ import annotations

from video_ai_editor.main import _render_failure_message


def test_blames_the_users_media_for_a_generic_ffmpeg_failure():
    tail = ("Stream #0:0: Video: h264, yuv420p, 1920x1080\n"
            "Error opening input file /tmp/uploads/x/weird.mp4\n"
            "Invalid data found when processing input")
    msg = _render_failure_message(tail)
    assert "corrupt frames" in msg or "unusual codec" in msg
    assert "overlay" not in msg.lower()


def test_blames_the_overlay_cache_when_a_sticker_png_is_the_failing_input():
    tail = (
        "Stream #9:0: Video: png, rgba(pc, gbr/unknown/unknown), 1920x1080\n"
        "[in#9] Error opening input: Invalid data found when processing input\n"
        "Error opening input file /Users/x/workdir/s_abc/cache/st_77b1820536bc13c5.png\n"
    )
    msg = _render_failure_message(tail)
    assert "overlay" in msg.lower()
    assert "your media is fine" in msg.lower()
    assert "corrupt frames" not in msg


def test_blames_the_overlay_cache_for_a_text_png():
    tail = "Error opening input file /workdir/s_x/cache/text_abcdef0123456789.png"
    msg = _render_failure_message(tail)
    assert "overlay" in msg.lower()


def test_blames_the_overlay_cache_for_an_animated_sticker_png():
    tail = "Error opening input file /workdir/s_x/cache/sa_abcdef0123456789.png"
    msg = _render_failure_message(tail)
    assert "overlay" in msg.lower()


def test_blames_the_overlay_cache_for_a_mask_png():
    tail = "Error opening input file /workdir/s_x/cache/mask_c1_circle_4_1080x1920.png"
    msg = _render_failure_message(tail)
    assert "overlay" in msg.lower()


def test_windows_backslash_path_is_still_recognized():
    tail = r"Error opening input file C:\Users\x\workdir\s_abc\cache\st_deadbeefcafef00d.png"
    msg = _render_failure_message(tail)
    assert "overlay" in msg.lower()
