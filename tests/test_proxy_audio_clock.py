"""The proxy's sound is on the FILE clock (wave E gate RX, finding 2).

A source whose audio stream starts after the file does (audio start_time >
format start_time — camera and screen-recorder MOV/MP4) is read by every
render chain through `aresample=async=1:first_pts=0`, which pads the gap
with silence: the render plays file time t. The proxy FLAC was decoded with
plain `aresample=48000`, so its sample 0 was the audio stream's first sample,
and the client — which reads proxy sample `edit_sample(in)` for a clip's
`in` (spec R9, `audio_placements` src0) — played 100 ms of audio-late source
100 ms off the server, on every sample of the clip. `proxy.AUDIO_FILTER`
now puts proxy sample S(t) at file time t (`RECIPE_VERSION` 4).

This test needs no browser: the client plays exactly the proxy samples the
model names (tests/wk/test_wk_audio.py proves that in WK, Chromium and
Playwright WebKit), so the model over the proxy must equal the server.
"""
from __future__ import annotations

import json
import subprocess
from fractions import Fraction

import numpy as np
import pytest

from video_ai_editor.render.frame_map import SourceInfo, audio_placements, build_program_map

from tests.wk import audio_fixture as fx

R2997 = Fraction(30000, 1001)
LATE = 0.1
LATE_SAMPLES = 4800


def _starts(path) -> tuple[dict, float]:
    pr = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                                    "-of", "json", str(path)], check=True, capture_output=True).stdout)
    return ({s["codec_type"]: float(s["start_time"]) for s in pr["streams"]},
            float(pr["format"]["start_time"]))


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    root = tmp_path_factory.mktemp("proxy-clock")
    src, wd = root / "src", root / "wd"
    src.mkdir()
    wd.mkdir()
    S = {"alate": fx.offset_source(src / "alate.mov", R2997, 12, audio_late=LATE),
         "vlate": fx.offset_source(src / "vlate.mov", R2997, 12, video_late=LATE)}
    keys, info, pcm = {}, {}, {}
    for name, p in S.items():
        key, idx, inf = fx.build_proxy_audio(p, wd)
        keys[name] = key
        info[str(p)] = SourceInfo.from_json(inf)
        pcm[name] = fx.decode_counter(fx.decode_chunks_f32(wd, key, idx))
    return {"root": root, "S": {k: str(v) for k, v in S.items()}, "info": info, "pcm": pcm}


def test_the_sources_start_apart(root):
    (st_a, fmt_a), (st_v, fmt_v) = _starts(root["S"]["alate"]), _starts(root["S"]["vlate"])
    assert st_a["audio"] - fmt_a == pytest.approx(LATE, abs=1e-3) and st_a["video"] == fmt_a
    assert st_v["video"] - fmt_v == pytest.approx(LATE, abs=1e-3) and st_v["audio"] == fmt_v


def test_proxy_sample_s_is_file_time_t(root):
    """Audio late by 0.1 s: 4800 samples of silence, then the stream's first
    sample. Video late: the audio is the file's start, no pad."""
    a = root["pcm"]["alate"]
    assert (a[:LATE_SAMPLES] == fx.SILENCE).all(), a[:8]
    assert a[LATE_SAMPLES] == 0 and a[LATE_SAMPLES + 1000] == 1000
    v = root["pcm"]["vlate"]
    assert v[0] == 0 and v[48000] == 48000


def test_the_model_over_the_proxy_is_the_server_render(root):
    """Every clip (near the head, no seek; mid-file, a seek) of both sources:
    the proxy samples the model names are the samples the server plays."""
    e, fps = fx.offset_edl(root["S"]["alate"], root["S"]["vlate"], R2997)
    server = fx.decode_counter(fx.server_render(e, fps, root["root"] / "cache"))
    pm = build_program_map(e, root["info"])
    name_of = {v: k for k, v in root["S"].items()}
    places = audio_placements(e, pm, sources=root["info"])
    assert len(places) == 4
    for a in places:
        c = pm.clips[a.clip]
        proxy = root["pcm"][name_of[c.src]]
        want = np.full(a.n, fx.SILENCE, dtype=np.int64)
        for off, cnt, first, d in a.runs:
            want[off:off + cnt] = proxy[first + d * np.arange(cnt)]
        got = server[a.out0:a.out0 + a.n]
        bad = np.nonzero(got != want)[0]
        assert len(bad) == 0, (c.id, len(bad), int(got[0]), int(want[0]))


def test_old_proxies_are_never_looked_up_again():
    """The file-clock sound changes what a proxy's bytes are: the recipe (and
    so every proxy key) moved on, so a proxy built off the clock ages out."""
    from video_ai_editor.ingest import proxy as P
    assert P.RECIPE_VERSION >= 4 and P.recipe()["version"] == P.RECIPE_VERSION
    assert "first_pts=0" in P.AUDIO_FILTER
