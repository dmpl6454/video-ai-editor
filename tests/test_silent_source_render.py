"""A source with NO AUDIO STREAM must render — preview, export, every path.

`compositor.py` referenced `[<i>:a]` for every v1/PIP clip unconditionally. A
file with no audio stream cannot bind that: ffmpeg answers "Stream specifier
':a' in filtergraph description … matches no streams" → "Error binding
filtergraph inputs/outputs" and the WHOLE render dies with rc=234, so one
silent clip took every preview and export down with it. (The exact mirror of
the audio-file-on-a-video-lane failure in `test_audio_on_video_lane.py`.)

`ingest/normalize.py` injects a silent track at UPLOAD time for exactly this
reason, so the failure needs a clip that skipped normalization — `add_clip`
straight from the API/MCP, or a restored `.vae`. That is also why the older
`tests/test_silent_clip.py` — which names this very ffmpeg error in its
docstring — never caught it: its render case normalizes the fixture first, so
the clip reaching the compositor already had a silent track welded on.

Fixing the binding then exposed a second, PRE-EXISTING export killer on the
same path: `loudnorm` measures -inf LUFS on a fully silent mix, computes an
infinite gain and emits NaN samples, which the AAC encoder rejects with -22.
That one is reachable two other ways with no silent source in sight (a muted
v1, an empty timeline) and is pinned below.

Run against BOTH ffmpeg builds — the bundled static one and Homebrew's:
    VAI_FFMPEG=<static>/ffmpeg VAI_FFPROBE=<static>/ffprobe uv run pytest \
        tests/test_silent_source_render.py
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from video_ai_editor import platformutil as _pu
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, AudioProps, Canvas, Clip, Track, Transition
from video_ai_editor.render import compositor as C
from video_ai_editor.render import render_export, render_preview

pytestmark = pytest.mark.skipif(
    not (shutil.which(_pu.FFMPEG) or Path(_pu.FFMPEG).exists()),
    reason="ffmpeg not available",
)


# ---------------------------------------------------------------- fixtures

def _mk_silent(p: Path, *, dur: float = 2.0) -> str:
    """A perfectly ordinary mp4 that happens to have NO audio stream."""
    subprocess.run([_pu.FFMPEG, "-y", "-f", "lavfi",
                    "-i", f"testsrc=size=320x180:rate=30", "-t", str(dur),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)],
                   check=True, capture_output=True)
    assert _stream_types(p) == ["video"], "fixture must have no audio stream"
    return str(p)


def _mk_sound(p: Path, *, dur: float = 2.0, freq: int = 440) -> str:
    subprocess.run([_pu.FFMPEG, "-y",
                    "-f", "lavfi", "-i", f"color=c=blue:s=320x180:d={dur}:r=30",
                    "-f", "lavfi", "-i", f"sine=f={freq}:duration={dur}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(p)],
                   check=True, capture_output=True)
    return str(p)


# ---------------------------------------------------------------- measuring

def _stream_types(p: Path) -> list[str]:
    out = subprocess.run([_pu.FFPROBE, "-v", "error", "-show_entries",
                          "stream=codec_type", "-of", "json", str(p)],
                         capture_output=True, text=True, check=True).stdout
    return [s["codec_type"] for s in json.loads(out).get("streams", [])]


def _duration(p: Path) -> float:
    out = subprocess.run([_pu.FFPROBE, "-v", "error", "-show_entries",
                          "format=duration", "-of", "csv=p=0", str(p)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def _mean_db(p: Path, *, ss: float | None = None, t: float | None = None) -> float:
    """`volumedetect` mean level in dB over the whole file or one window.

    Digital silence reports -91.0 dB (the same figure the mute tests use), so
    a window is "silent" at <= -80 and "audible" well above it. The window
    form is what makes a MIXED timeline testable: it proves the silence landed
    where the silent clip is and the sound landed where the sound clip is,
    which is the alignment claim.
    """
    args = [_pu.FFMPEG, "-hide_banner"]
    if ss is not None:
        args += ["-ss", f"{ss:.3f}"]
    args += ["-i", str(p)]
    if t is not None:
        args += ["-t", f"{t:.3f}"]
    args += ["-af", "volumedetect", "-vn", "-f", "null", "-"]
    proc = subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", proc.stderr)
    assert m, f"volumedetect said nothing:\n{proc.stderr[-1500:]}"
    return float(m.group(1))


def _store(tmp_path: Path, edl: EDL) -> EDLStore:
    edl.recompute_duration()
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(tmp_path)


def _edl(*tracks: Track) -> EDL:
    # 320x180 canvas + the DEFAULT loudness target (-16 LUFS), because the
    # loudnorm-on-silence NaN is half of what these tests are about. Existing
    # audio tests pass loudness_lufs=None; doing that here would hide it.
    return EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=list(tracks))


# ---------------------------------------------------------------- the bug

@pytest.mark.parametrize("render", ["export", "preview"])
def test_silent_only_timeline_renders(tmp_path: Path, render: str):
    """The exact reproduction: one 2s silent clip on v1."""
    src = _mk_silent(tmp_path / "silent.mp4")
    edl = _edl(Track(id="v1", type="video", clips=[
        Clip(src=src, in_=0, out=2, start=0, id="c1")]))
    store = _store(tmp_path / "s", edl)
    fn = render_export if render == "export" else render_preview
    out = fn(store.edl, tmp_path / "s", height=180).path

    # A silent clip must still CONTRIBUTE an audio stream of its own length,
    # or every downstream consumer (concat, the <video> tag, the next export)
    # sees a different timeline than the EDL describes.
    assert _stream_types(out) == ["video", "audio"]
    assert abs(_duration(out) - 2.0) < 0.15, _duration(out)
    assert _mean_db(out) <= -80.0, "the substituted track is not silence"


def test_mixed_timeline_keeps_audio_aligned(tmp_path: Path):
    """Silent 0-2s then sound 2-4s: streams must match for concat, and the
    silence must occupy exactly its own slot."""
    silent = _mk_silent(tmp_path / "silent.mp4")
    sound = _mk_sound(tmp_path / "sound.mp4")
    edl = _edl(Track(id="v1", type="video", clips=[
        Clip(src=silent, in_=0, out=2, start=0, id="c1"),
        Clip(src=sound, in_=0, out=2, start=2, id="c2")]))
    out = render_export(_store(tmp_path / "s", edl).edl, tmp_path / "s",
                        height=180).path

    assert abs(_duration(out) - 4.0) < 0.2, _duration(out)
    first = _mean_db(out, ss=0.1, t=1.7)
    second = _mean_db(out, ss=2.3, t=1.5)
    assert first <= -80.0, f"first half should be silent, got {first} dB"
    assert second > -35.0, f"second half should be audible, got {second} dB"


def test_silence_is_the_clips_own_EFFECTIVE_duration(tmp_path: Path):
    """A 2s silent clip at 2x occupies 1s of timeline, so its substituted
    silence must be 1s — not 2s, which would push everything after it out of
    sync with the picture (`concat` sequences audio and video independently).
    """
    silent = _mk_silent(tmp_path / "silent.mp4")
    sound = _mk_sound(tmp_path / "sound.mp4")
    edl = _edl(Track(id="v1", type="video", clips=[
        Clip(src=silent, in_=0, out=2, start=0, id="c1", speed=2.0),
        Clip(src=sound, in_=0, out=2, start=1, id="c2")]))
    out = render_export(_store(tmp_path / "s", edl).edl, tmp_path / "s",
                        height=180).path

    assert abs(_duration(out) - 3.0) < 0.2, _duration(out)
    assert _mean_db(out, ss=0.05, t=0.8) <= -80.0, "silence ran short or long"
    assert _mean_db(out, ss=1.3, t=1.5) > -35.0, "the sound clip moved"


def test_mixed_timeline_survives_a_transition(tmp_path: Path):
    """The xfade/acrossfade assembly, i.e. the chunk cache DISABLED.

    `acrossfade` has to negotiate a silent `anullsrc` branch against a decoded
    one; a half-fix that only satisfies `concat` fails here.
    """
    silent = _mk_silent(tmp_path / "silent.mp4")
    sound = _mk_sound(tmp_path / "sound.mp4")
    edl = _edl(Track(id="v1", type="video",
                     clips=[Clip(src=silent, in_=0, out=2, start=0, id="c1"),
                            Clip(src=sound, in_=0, out=2, start=2, id="c2")],
                     transitions=[Transition(at=2.0, type="fade", duration=0.5)]))
    store = _store(tmp_path / "s", edl)
    out = render_export(store.edl, tmp_path / "s", height=180).path

    # A transition SHORTENS the timeline by its own duration (EDL.duration
    # already models this), so 4.0 - 0.5.
    assert abs(_duration(out) - 3.5) < 0.25, _duration(out)
    assert _mean_db(out, ss=2.0, t=1.4) > -35.0, "the sound clip lost its audio"


def test_silent_pip_over_a_sounding_v1(tmp_path: Path):
    """A silent PIP must neither break the graph nor mute v1.

    The PIP fold reaches its own `[<i>:a]` (compositor, not
    _build_clip_audio_chain), so it needed its own skip — and `j` has to stay
    aligned with pip.py's input list.
    """
    silent = _mk_silent(tmp_path / "silent.mp4")
    sound = _mk_sound(tmp_path / "sound.mp4")
    edl = _edl(
        Track(id="v1", type="video", clips=[
            Clip(src=sound, in_=0, out=2, start=0, id="c1")]),
        Track(id="v2", type="video", z=1, clips=[
            Clip(src=silent, in_=0, out=2, start=0, id="p1")]),
    )
    out = render_export(_store(tmp_path / "s", edl).edl, tmp_path / "s",
                        height=180).path
    assert _stream_types(out) == ["video", "audio"]
    assert _mean_db(out) > -35.0, "v1's audio was lost folding in the PIP"


def test_all_silent_pip_and_v1(tmp_path: Path):
    """Nothing audible anywhere: the PIP fold must emit no amix at all and
    loudnorm must not run."""
    silent = _mk_silent(tmp_path / "silent.mp4")
    edl = _edl(
        Track(id="v1", type="video", clips=[
            Clip(src=silent, in_=0, out=2, start=0, id="c1")]),
        Track(id="v2", type="video", z=1, clips=[
            Clip(src=silent, in_=0, out=2, start=0, id="p1")]),
    )
    out = render_export(_store(tmp_path / "s", edl).edl, tmp_path / "s",
                        height=180).path
    assert _stream_types(out) == ["video", "audio"]
    assert _mean_db(out) <= -80.0


# ---------------------------------------------------------------- chunk cache

def test_chunks_carry_audio_and_stream_copy_assembles(tmp_path: Path):
    """Chunks UNIFORMLY carry audio, which is what keeps the chunk-consuming
    graph (`[<i>:a]anull[a<i>]`) and the `-c copy` assembly bindable.

    That falls out of `render_clip_to_chunk` building its audio through the
    same `_build_clip_audio_chain`, so chunks.py needs no silent-source branch
    — but if that ever stops being true, this is the test that says so.
    """
    silent = _mk_silent(tmp_path / "silent.mp4")
    sess = tmp_path / "s"
    edl = _edl(Track(id="v1", type="video", clips=[
        Clip(src=silent, in_=0, out=2, start=0, id="c1")]))
    store = _store(sess, edl)

    first = render_preview(store.edl, sess, height=180).path
    chunks = sorted(sess.rglob("chunk_*.mp4"))
    assert chunks, "the chunk cache never engaged; this test proves nothing"
    for ch in chunks:
        assert "audio" in _stream_types(ch), f"{ch.name} has no audio stream"

    # Drop the hash-keyed preview AND the video-only cache (which lives in
    # cache/videos/, not previews/ — leaving it takes the audio-remux fast
    # path instead and this test silently proves nothing), but keep the
    # chunks: the next render is then the warm path, which packet-concats the
    # chunks instead of re-encoding.
    first.unlink()
    for stale in (sess / "cache" / "videos").glob("video_*.mp4"):
        stale.unlink()
    calls: list[int] = []
    real = C._assemble_chunks_streamcopy

    def spy(*a, **k):
        calls.append(1)
        return real(*a, **k)

    C._assemble_chunks_streamcopy = spy
    try:
        again = render_preview(store.edl, sess, height=180).path
    finally:
        C._assemble_chunks_streamcopy = real
    assert calls, "the stream-copy assembly path was not exercised"
    assert _stream_types(again) == ["video", "audio"]
    assert abs(_duration(again) - 2.0) < 0.15
    assert _mean_db(again) <= -80.0


def test_remux_fast_path_with_a_silent_v1(tmp_path: Path):
    """The audio-only remux (`-c:v copy`) builds its OWN per-clip audio chains
    and its own input list, so it fails independently of the main render."""
    silent = _mk_silent(tmp_path / "silent.mp4")
    music = _mk_sound(tmp_path / "music.mp4", dur=1.0, freq=220)
    sess = tmp_path / "s"
    edl = _edl(Track(id="v1", type="video", clips=[
        Clip(src=silent, in_=0, out=2, start=0, id="c1")]))
    store = _store(sess, edl)
    render_preview(store.edl, sess, height=180)  # caches the video-only mp4

    # Music-only edit that does NOT move the timeline end → same video
    # fingerprint → the remux fast path.
    store.edl.tracks.append(Track(id="music", type="music", clips=[
        Clip(src=music, in_=0, out=1, start=0, id="m1")]))
    store.edl.recompute_duration()
    calls: list[int] = []
    real = C._remux_with_new_audio

    def spy(*a, **k):
        calls.append(1)
        return real(*a, **k)

    C._remux_with_new_audio = spy
    try:
        out = render_preview(store.edl, sess, height=180).path
    finally:
        C._remux_with_new_audio = real
    assert calls, "the remux fast path was not exercised"
    assert _mean_db(out, ss=0.05, t=0.8) > -40.0, "the music never made it in"


# ------------------------------------------------- a clip WITH audio, unchanged

def test_a_clip_with_audio_is_untouched(tmp_path: Path):
    """Strict widening, at the level that matters: same filter string.

    `_build_clip_audio_chain` is the one function that grew a branch, so the
    proof that nothing changed for a source that HAS audio is that it still
    emits exactly the legacy chain — input label included.
    """
    sound = _mk_sound(tmp_path / "sound.mp4")
    c = Clip(src=sound, in_=0, out=2, start=0, id="c1",
             audio=AudioProps(gain_db=-6.0))
    got = C._build_clip_audio_chain(c, input_label="[3:a]", label_out="[a3]")
    assert got == ("[3:a]aresample=async=1:first_pts=0,"
                   "aformat=channel_layouts=stereo:sample_rates=48000"
                   ",volume=-6.00dB[a3]")


def test_audible_clip_measures_the_same_next_to_a_silent_one(tmp_path: Path):
    """Differential: the sound clip's own level must not move because a silent
    neighbour joined the timeline (measured, not assumed).

    `loudness_lufs=None` on purpose. loudnorm measures the WHOLE mix, so half
    the file becoming silence legitimately lifts the audible half (measured:
    -18.3 dB alone vs -13.5 dB mixed) — that is loudnorm working, and it would
    swamp the thing under test. With it off, this compares the per-clip audio
    chain against itself, which is the path the silent branch sits in.
    """
    silent = _mk_silent(tmp_path / "silent.mp4")
    sound = _mk_sound(tmp_path / "sound.mp4")

    def edl_of(*clips: Clip) -> EDL:
        return EDL(canvas=Canvas(w=320, h=180, fps=30, loudness_lufs=None),
                   tracks=[Track(id="v1", type="video", clips=list(clips))])

    alone = edl_of(Clip(src=sound, in_=0, out=2, start=0, id="c1"))
    a_out = render_export(_store(tmp_path / "a", alone).edl, tmp_path / "a",
                          height=180).path
    a_db = _mean_db(a_out, ss=0.2, t=1.5)

    mixed = edl_of(Clip(src=silent, in_=0, out=2, start=0, id="c0"),
                   Clip(src=sound, in_=0, out=2, start=2, id="c1"))
    m_out = render_export(_store(tmp_path / "m", mixed).edl, tmp_path / "m",
                          height=180).path
    m_db = _mean_db(m_out, ss=2.2, t=1.5)

    assert abs(a_db - m_db) < 0.6, f"alone {a_db} dB vs mixed {m_db} dB"


# ---------------------------------------------------- the loudnorm NaN guard

def test_loudnorm_runs_whenever_anything_is_audible(tmp_path: Path):
    sound = _mk_sound(tmp_path / "sound.mp4")
    silent = _mk_silent(tmp_path / "silent.mp4")

    audible = _edl(Track(id="v1", type="video", clips=[
        Clip(src=sound, in_=0, out=2, start=0, id="c1")]))
    assert C._audio_mix_has_real_source(audible) is True

    # A muted CLIP, a muted TRACK and a silent SOURCE are each silence.
    muted_clip = _edl(Track(id="v1", type="video", clips=[
        Clip(src=sound, in_=0, out=2, start=0, id="c1",
             audio=AudioProps(mute=True))]))
    assert C._audio_mix_has_real_source(muted_clip) is False

    muted_track = _edl(Track(id="v1", type="video", muted=True, clips=[
        Clip(src=sound, in_=0, out=2, start=0, id="c1")]))
    assert C._audio_mix_has_real_source(muted_track) is False

    no_audio = _edl(Track(id="v1", type="video", clips=[
        Clip(src=silent, in_=0, out=2, start=0, id="c1")]))
    assert C._audio_mix_has_real_source(no_audio) is False

    # Music alone still counts, on a lane v1 knows nothing about.
    music_only = _edl(
        Track(id="v1", type="video", clips=[
            Clip(src=silent, in_=0, out=2, start=0, id="c1")]),
        Track(id="music", type="music", clips=[
            Clip(src=sound, in_=0, out=2, start=0, id="m1")]),
    )
    assert C._audio_mix_has_real_source(music_only) is True


@pytest.mark.parametrize("shape", ["muted_v1", "empty_timeline"])
def test_all_silent_export_no_longer_dies_in_the_aac_encoder(tmp_path: Path,
                                                             shape: str):
    """Both PRE-EXISTING reachings of the loudnorm NaN, with no silent source
    involved. Verified failing (rc=234, "Input contains (near) NaN/+-Inf")
    before the guard."""
    if shape == "muted_v1":
        sound = _mk_sound(tmp_path / "sound.mp4")
        edl = _edl(Track(id="v1", type="video", muted=True, clips=[
            Clip(src=sound, in_=0, out=2, start=0, id="c1")]))
    else:
        edl = _edl(Track(id="v1", type="video", clips=[]))
    out = render_export(_store(tmp_path / "s", edl).edl, tmp_path / "s",
                        height=180).path
    assert _stream_types(out) == ["video", "audio"]
    assert _mean_db(out) <= -80.0


# ---------------------------------------------------------------- the probe

def test_probe_is_cached_per_path_mtime_size(tmp_path: Path):
    # The probes live in render/_probe_cache so audio_mix.py can share them
    # (importing them from compositor would be circular).
    from video_ai_editor.render import _probe_cache as PC
    silent = _mk_silent(tmp_path / "silent.mp4")
    PC.clear_caches()
    before = PC._probe_has_audio_cached.cache_info()
    assert C._source_has_audio(silent) is False
    assert C._source_has_audio(silent) is False
    assert C._source_has_audio(silent) is False
    info = PC._probe_has_audio_cached.cache_info()
    assert info.misses - before.misses == 1, info
    assert info.hits - before.hits == 2, info

    # …but the answer must not outlive the FILE. Replace it in place with one
    # that has sound (an in-place re-encode, a re-download, a restored
    # project): a path-only key would serve "silent" forever and mute it.
    _mk_sound(Path(silent))
    assert C._source_has_audio(silent) is True


def test_a_failing_probe_falls_back_to_the_pre_change_behaviour(tmp_path: Path, monkeypatch):
    """An unanswerable probe must degrade to `[i:a]`, and must not be cached.

    The fallback is deliberately the OPPOSITE of "keep the graph bindable".
    Answering "silent" would hand back a graph that binds and renders a SILENT
    export — a wrong result presented as success — and because renders are
    cached on disk under the EDL hash, that wrong answer outlives both the probe
    cache and a restart. Emitting `[i:a]` is exactly what every caller did
    before the probe existed, so a hiccup behaves as it always did and ffmpeg
    reports the real problem with the input in its own words.

    Not caching it is the other half: `lru_cache` memoizes return values, so a
    single transient ffprobe failure must not mark a file silent for the life of
    the process.
    """
    from video_ai_editor.render import _probe_cache as PC
    sound = _mk_sound(tmp_path / "sound.mp4")
    PC.clear_caches()
    import video_ai_editor.ingest.probe as probe_mod

    calls = {"n": 0}
    real = probe_mod.probe

    def boom_once(p):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("ffprobe exploded")
        return real(p)

    monkeypatch.setattr(probe_mod, "probe", boom_once)
    # First call fails -> pre-change answer, and the graph keeps `[0:a]`.
    assert C._source_has_audio(sound) is True
    chain = C._build_clip_audio_chain(
        Clip(src=sound, in_=0, out=2, start=0, id="c1"),
        input_label="[0:a]", label_out="[a0]")
    assert "[0:a]" in chain and not chain.startswith("anullsrc=")
    # The failure was NOT memoized: the next call probes again and answers
    # correctly, so one hiccup cannot mute a file for the whole process.
    assert C._source_has_audio(sound) is True
    assert calls["n"] == 2, calls

    # A file that cannot be stat'd at all also degrades to `[i:a]` rather than
    # being silently rendered as silence; ffmpeg then says "No such file".
    PC.clear_caches()
    assert C._source_has_audio(tmp_path / "does-not-exist.mp4") is True
