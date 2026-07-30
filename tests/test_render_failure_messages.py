"""Render-failure messages must name the real cause (RC-CLASSIFY).

Every one of these samples is VERBATIM captured ffmpeg output — either produced
by driving the real compositor into the failure, or transcribed from a tester's
screenshot — and each is fed at the width production actually uses. That matters
twice over:

  * `main.py` displays `msg[-400:]` but must CLASSIFY on the whole string,
    because the decisive ffmpeg line sits behind the per-stream teardown
    summaries and `Conversion failed!`. Classifying on the tail alone is why
    almost every real failure fell through to "corrupt frames or an unusual
    codec" and sent users hunting through healthy footage.
  * the overlay-cache branch must only fire when an overlay PNG is named on an
    ERROR line. ffmpeg lists every input it opens, so a timeline that merely HAS
    text or stickers mentions `st_<hash>.png` in stderr on every single render —
    matching anywhere would blame a corrupt overlay for unrelated failures.
"""
from __future__ import annotations

from video_ai_editor.main import _render_failure_message


def classify(full: str) -> str:
    """Call it exactly the way the routes do: tail for display, full to classify."""
    return _render_failure_message(full[-400:], full)


# Transcribed from the tester's screenshot (img_p/11.png). Note the encode
# SUCCEEDED — moov atom written, muxing overhead reported — and the process was
# then terminated. The app told the user their media had "corrupt frames".
KILLED = """ffmpeg render failed (rc=255):
frame= 2650 fps=188 q=35.0 Lsize=  6986KiB time=00:01:28.26 bitrate= 648.3kbits/s speed=6.26x
[out#0/mp4 @ 0000027aef92c680] video:5412KiB audio:1481KiB subtitle:0KiB other streams:0KiB global headers:0KiB muxing overhead: 1.338228%
[mp4 @ 0000027aef92c680] second pass: moving the moov atom to the beginning of the file
[aac @ 0000027af1b4f700] Qavg: 56407.660
Exiting normally, received signal 15.
"""

# Real capture: 4:5 canvas, portrait source, odd 675 preview height.
PAD_TOO_SMALL = """ffmpeg render failed (rc=234):
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from '/tmp/port.mp4':
  Duration: 00:00:04.00, start: 0.000000, bitrate: 210 kb/s
  Stream #0:0[0x1](und): Video: h264 (High), yuv420p, 1080x1920, 30 fps
[Parsed_pad_7 @ 0x14a704e10] Padded dimensions cannot be smaller than input dimensions.
[fc#0 @ 0x600000c8c000] Error reinitializing filters!
[vost#0:0/h264_videotoolbox @ 0x14a7051c0] Task finished with error code: -22 (Invalid argument)
[out#0/mp4 @ 0x600001708000] video:0KiB audio:0KiB subtitle:0KiB other streams:0KiB global headers:0KiB muxing overhead: unknown
Nothing was written into output file, because at least one of its streams received no packets.
frame=    0 fps=0.0 q=0.0 Lsize=       0KiB time=N/A bitrate=N/A speed=N/A
Conversion failed!
"""

# Real capture: 4:5 canvas with a leading gap — `color=` filler vs padded clips.
CONCAT_MISMATCH = """ffmpeg render failed (rc=234):
[Parsed_concat_7 @ 0x11de068c0] Input link in0:v0 parameters (size 540x674, SAR 1:1) do not match the corresponding output link in0:v0 parameters (540x675, SAR 1:1)
[fc#0 @ 0x600002a90000] Error reinitializing filters!
[vost#0:0/h264_videotoolbox @ 0x11de07000] Task finished with error code: -22 (Invalid argument)
Nothing was written into output file, because at least one of its streams received no packets.
Conversion failed!
"""

# The libx264 (Windows) leg of the same odd-height bug.
ODD_HEIGHT_X264 = """ffmpeg render failed (rc=234):
[libx264 @ 0x13f7060c0] height not divisible by 2 (540x675)
[vost#0:0/libx264 @ 0x13f705e00] Task finished with error code: -22 (Invalid argument)
Nothing was written into output file, because at least one of its streams received no packets.
Conversion failed!
"""

# A torn overlay PNG — the overlay branch SHOULD fire here.
TORN_OVERLAY = """ffmpeg render failed (rc=1):
Input #12, png_pipe, from '/w/s_ab/cache/text/st_9f2c1ab34de05771.png':
[png_pipe @ 0x6000012f0000] Invalid data found when processing input
[in#12 @ 0x600003a1c000] Error opening input: Invalid data found when processing input
Error opening input file /w/s_ab/cache/text/st_9f2c1ab34de05771.png.
Conversion failed!
"""

# A HEALTHY overlay listed as an input, with the real failure elsewhere. The
# overlay branch must NOT fire — this is the widening misfire to guard against.
OVERLAY_PRESENT_BUT_INNOCENT = """ffmpeg render failed (rc=234):
Input #11, png_pipe, from '/w/s_ab/cache/text/st_9f2c1ab34de05771.png':
  Stream #11:0: Video: png, rgba(pc), 1080x400, 30 fps
Input #12, png_pipe, from '/w/s_ab/cache/text/sa_1122334455667788.png':
  Stream #12:0: Video: png, rgba(pc), 200x200, 30 fps
[Parsed_concat_9 @ 0x11de068c0] Input link in0:v0 parameters (size 540x674, SAR 1:1) do not match the corresponding output link in0:v0 parameters (540x675, SAR 1:1)
[fc#0 @ 0x600002a90000] Error reinitializing filters!
Conversion failed!
"""

MISSING_SOURCE = """ffmpeg render failed (rc=1):
[in#3 @ 0x600003a1c000] Error opening input: No such file or directory
Error opening input file '/Users/x/Movies/holiday_clip.mp4'.
Conversion failed!
"""

AUDIO_ON_VIDEO_LANE = """ffmpeg render failed (rc=234):
[fc#0 @ 0x600000c8c000] Stream specifier ':v' in filtergraph description matches no streams.
Error binding filtergraph inputs/outputs.
Conversion failed!
"""

GENUINELY_BAD_MEDIA = """ffmpeg render failed (rc=1):
[h264 @ 0x148008e00] Invalid NAL unit size (2947 > 118).
[h264 @ 0x148008e00] Error splitting the input into NAL units.
[vist#0:0/h264 @ 0x148008a00] Error submitting packet to decoder: Invalid data found when processing input
Conversion failed!
"""


def test_interrupted_render_is_not_blamed_on_the_media():
    m = classify(KILLED)
    assert "interrupted" in m.lower()
    assert "corrupt" not in m.lower(), "a SIGTERM'd-but-successful encode is not corrupt media"


def test_pad_too_small_reads_as_an_app_bug():
    m = classify(PAD_TOO_SMALL)
    assert "filtergraph" in m.lower() and "bug" in m.lower()
    assert "corrupt" not in m.lower()


def test_concat_dimension_mismatch_reads_as_an_app_bug():
    assert "filtergraph" in classify(CONCAT_MISMATCH).lower()


def test_odd_height_reads_as_an_app_bug():
    assert "filtergraph" in classify(ODD_HEIGHT_X264).lower()


def test_torn_overlay_png_is_identified():
    m = classify(TORN_OVERLAY)
    assert "overlay" in m.lower() and "media is fine" in m.lower()


def test_healthy_overlay_input_does_not_hijack_the_diagnosis():
    """The misfire guard: overlays present, real cause is a size mismatch."""
    m = classify(OVERLAY_PRESENT_BUT_INNOCENT)
    assert "overlay" not in m.lower(), f"overlay branch misfired: {m}"
    assert "filtergraph" in m.lower()


def test_missing_source_file_is_identified_by_name():
    m = classify(MISSING_SOURCE)
    assert "missing" in m.lower() and "holiday_clip.mp4" in m


def test_audio_only_clip_on_video_lane_is_identified():
    assert "Music lane" in classify(AUDIO_ON_VIDEO_LANE)


def test_genuinely_bad_media_still_says_so():
    """The generic branch must survive — it is correct for real decode errors."""
    assert "corrupt" in classify(GENUINELY_BAD_MEDIA).lower()


def test_classification_uses_the_full_message_not_just_the_tail():
    """Regression guard for the actual defect.

    Pad the decisive line far enough back that a 400-char window cannot see it.
    Classifying on the tail returns the generic message; classifying on the full
    string returns the right one.
    """
    noise = "\n".join(f"  Stream #0:{i}: Video: h264, yuv420p, 1920x1080, 30 fps"
                      for i in range(40))
    full = PAD_TOO_SMALL + noise + "\nConversion failed!\n"
    assert len(full) - len(PAD_TOO_SMALL) > 400
    assert "filtergraph" in _render_failure_message(full[-400:], full).lower()
    # ...and prove the tail alone genuinely cannot tell:
    assert "corrupt" in _render_failure_message(full[-400:]).lower()
