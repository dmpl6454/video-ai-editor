"""What the ffmpeg on PATH can do, for tests that force a code path meant for
another version and must not hand THIS binary an option it does not have.

CI run 36601831900 measured both ends of the graph-file option: ffmpeg 9.0.1
answers "Unrecognized option 'filter_complex_script'" (deprecated in 7.0,
gone after 8) and 6.1.1 answers the same for '/filter_complex' (new in 7.0).
"""
from __future__ import annotations

from video_ai_editor.render import compositor

# The cached reader itself, taken at import: tests patch the module attribute
# `compositor._ffmpeg_major` to force a path, and this must keep telling the
# truth about the binary.
_read_major = compositor._ffmpeg_major


def real_major() -> int | None:
    """Major version of the ffmpeg on PATH; None for a git build."""
    return _read_major()


def binary_reads(opt: str) -> bool:
    """Whether the ffmpeg on PATH has the graph-file option `opt`. An
    unreadable version is treated as a recent build."""
    major = real_major()
    if opt == "-filter_complex_script":
        return major is not None and major <= 8
    if opt == "-/filter_complex":
        return major is None or major >= 7
    raise ValueError(f"not a graph-file option: {opt}")
