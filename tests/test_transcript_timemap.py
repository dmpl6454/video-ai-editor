"""Transcript times are SOURCE seconds; the timeline is not — agent/timemap.

Proven by execution on a real 36s clip (Piper narration with fillers,
faster-whisper transcript): `remove_fillers` fed source-second word times
straight to `cut_range`, which takes timeline seconds. Invisible on a fresh
timeline (the two clocks coincide); run AFTER `remove_silences` it removed 1 of
4 fillers and cut two ranges of real speech (source 9.65–10.96 and 18.66–19.17).
`auto_caption` laid cues at source times too: after cuts the last cue sat at
33.9s on a 24.2s timeline, `recompute_duration` grew the project to the overlay
end, and the render came out 34s long — ~10s of it black — with the removed
fillers still in the caption text.

These tests reproduce that pipeline on synthetic media (ffmpeg lavfi, as the
other composite tests do) with a hand-written ingest.json transcript, so no
Whisper model is loaded: `auto_caption`'s `transcribe()` is monkeypatched to
return the same transcript. Assertions are on SOURCE ranges that survive on
v1, on cue times against the v1 extent, and on cue text — never on summaries.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import video_ai_editor.ingest.transcribe as T
from video_ai_editor.agent import dispatch as D
from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.agent.timemap import (
    clamp_to_extent,
    clip_source_to_timeline,
    clip_timeline_to_source,
    map_segments_to_timeline,
    map_words_to_timeline,
    source_range_to_timeline,
    source_to_timeline,
    timeline_to_source,
)
from video_ai_editor.edl.schema import EDL, Canvas, Clip, TextClip, Track
from video_ai_editor.edl.snapshot import EDLStore
from video_ai_editor.ingest.caption_format import cues_from_segments

# --------------------------------------------------------------- fixtures

CLIP_DUR = 12.0
# Tone = "speech"; the two silent gaps are what remove_silences will find.
# (keep_pad=0.1 default → removed ≈ [3.1,4.9] and [8.1,9.9] in source time.)
TONE_WINDOWS = ((0.0, 3.0), (5.0, 8.0), (10.0, 12.0))
PAD = 0.05

# Word times in SOURCE seconds, all inside the tone windows. Three fillers,
# each AFTER at least one silence, so the pre-fix source-as-timeline bug would
# have cut the wrong footage for two of them and only got the first by luck.
WORDS = [
    ("so", 0.20, 0.50), ("um", 0.60, 0.90), ("hello", 1.00, 1.50), ("there", 1.60, 2.10),
    ("friends", 2.20, 2.80),
    ("uh", 5.50, 5.80), ("today", 6.00, 6.50), ("we", 6.60, 6.80), ("start", 7.00, 7.60),
    ("um", 10.50, 10.80), ("goodbye", 11.00, 11.60),
]
FILLERS = ("um", "uh")


def _speech_clip(tmp_path: Path, name: str = "talk") -> Path:
    """`uploads/<name>/<name>.normalized.mp4`, the layout ingest_upload writes,
    so an ingest.json beside it is what _current_v1_ingest_json resolves."""
    d = tmp_path / "uploads" / name
    d.mkdir(parents=True, exist_ok=True)
    src = d / f"{name}.normalized.mp4"
    gate = "+".join(f"between(t\\,{a}\\,{b})" for a, b in TONE_WINDOWS)
    expr = f"0.6*sin(440*2*PI*t)*({gate})"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=c=blue:s=320x180:d={CLIP_DUR}:r=30",
         "-f", "lavfi", "-i", f"aevalsrc='{expr}':s=48000:d={CLIP_DUR}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(src)],
        check=True, capture_output=True,
    )
    return src


def _transcript(words=WORDS, duration: float = CLIP_DUR) -> dict:
    text = " ".join(w for w, _, _ in words)
    return {
        "language": "en", "duration": duration,
        "segments": [{"id": 0, "start": words[0][1], "end": words[-1][2], "text": text,
                      "words": [{"word": w, "start": s, "end": e, "prob": 1.0}
                                for w, s, e in words]}],
    }


def _write_ingest(src: Path, transcript: dict | None = None) -> None:
    (src.parent / "ingest.json").write_text(
        json.dumps({"transcript": transcript or _transcript()}), encoding="utf-8")


def _store(tmp_path: Path, src: Path | None = None) -> EDLStore:
    store = EDLStore(tmp_path / "sess")
    src = src or _speech_clip(tmp_path)
    dispatch(store, "add_clip", {"track": "v1", "src": str(src),
                                 "in": 0, "out": CLIP_DUR, "start": 0})
    return store


def _surviving_source(store: EDLStore, track: str = "v1") -> list[tuple[float, float]]:
    """Source ranges still on the track, in timeline order."""
    t = store.edl.get_track(track)
    return [(round(c.in_, 4), round(c.out, 4))
            for c in sorted(t.clips, key=lambda c: c.start) if isinstance(c, Clip)]


def _on_timeline(ranges, s: float, e: float, tol: float = 1e-3) -> bool:
    """Is any part of source [s, e] (beyond `tol`) still present?"""
    return any(min(e, hi) - max(s, lo) > tol for lo, hi in ranges)


def _covered(ranges, s: float, e: float, tol: float = 1e-3) -> bool:
    """Is source [s, e] entirely inside ONE surviving range?"""
    return any(lo - tol <= s and e <= hi + tol for lo, hi in ranges)


def _captions(store: EDLStore) -> list[TextClip]:
    cap = store.edl.get_track("captions")
    return [c for c in (cap.clips if cap else []) if isinstance(c, TextClip)]


def _cue_tuples(store: EDLStore) -> list[tuple[float, float, str]]:
    return [(c.start, c.end, c.text) for c in _captions(store)]


@pytest.fixture(autouse=True)
def _desktop_path_posture():
    """Run under the shipped desktop posture: filesystem allowlist OFF.

    `api/auth.install()` arms `config._FORCED_RESTRICT` at app boot whenever the
    developer's REAL `~/Library/Application Support/Video AI Editor/settings.json`
    says `lan_enabled: true` (see api/pairing.sync_path_restriction), and nothing
    in the process ever disarms it — so on a Mac that has paired a phone, every
    test after the first one that imports the app rejects pytest's tmp paths
    with "read path … is outside the allowed roots". These tests only do EDL
    math on synthetic media under tmp_path; they must not depend on which
    machine runs them. Same reset `tests/lan_fixtures.reset_pairing_state`
    applies, restored afterwards so this file leaks nothing either way.
    """
    from video_ai_editor import config
    before = config._FORCED_RESTRICT
    config.enable_path_restriction(False)
    try:
        yield
    finally:
        config.enable_path_restriction(before)


class _FakeTranscript:
    """What auto_caption needs from ingest.transcribe.Transcript."""

    def __init__(self, data: dict):
        self._d = data
        self.language = data["language"]
        self.duration = data["duration"]

    def model_dump(self) -> dict:
        return json.loads(json.dumps(self._d))


@pytest.fixture
def fake_whisper(monkeypatch):
    """auto_caption imports `transcribe` from ingest.transcribe at call time,
    so patching the module attribute keeps large-v3 out of the test."""
    plan = {"transcript": _transcript()}

    def fake_transcribe(path, language=None, model_size=None, backend=None,
                        task="transcribe", on_progress=None, should_cancel=None):
        return _FakeTranscript(plan["transcript"])

    monkeypatch.setattr(T, "transcribe", fake_transcribe)
    return plan


# ------------------------------------------------------- mapper unit tests

def _edl(*clips: Clip) -> EDL:
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30),
              tracks=[Track(id="v1", type="video", clips=list(clips))])
    edl.recompute_duration()
    return edl


def test_per_clip_conversions_are_inverses_and_speed_aware():
    c = Clip(src="/x/a.mp4", in_=4.0, out=12.0, start=1.0, speed=2.0)
    # source 6.0 is 2s into the slice; at 2x that is 1s of timeline after start.
    assert clip_source_to_timeline(c, 6.0) == pytest.approx(2.0)
    assert clip_timeline_to_source(c, 2.0) == pytest.approx(6.0)
    for t in (4.0, 5.3, 11.999):
        assert clip_timeline_to_source(c, clip_source_to_timeline(c, t)) == pytest.approx(t)


def test_fresh_clip_mapping_is_bit_exact_identity():
    """The identity case must be EXACT (not approx): the caption tools promise
    byte-for-byte unchanged output on an uncut timeline, and that rests on
    `0.0 + (t - 0.0) / 1.0 == t` holding in IEEE arithmetic."""
    c = Clip(src="/x/a.mp4", in_=0.0, out=36.0, start=0.0)
    for t in (0.0, 0.1, 9.65, 18.66, 33.9, 35.999999):
        assert clip_source_to_timeline(c, t) == t


def test_source_to_timeline_is_none_for_cut_away_footage():
    # source [0,3) at timeline [0,3), source [5,8) at timeline [3,6): [3,5) removed
    edl = _edl(Clip(src="/x/a.mp4", in_=0, out=3, start=0),
               Clip(src="/x/a.mp4", in_=5, out=8, start=3))
    assert source_to_timeline(edl, "v1", 1.0) == 1.0
    assert source_to_timeline(edl, "v1", 4.0) is None
    assert source_to_timeline(edl, "v1", 6.0) == pytest.approx(4.0)
    # the exact end of what the timeline shows of the file answers with the
    # extent, not None (and ONLY that instant — see the true_end test below)
    assert source_to_timeline(edl, "v1", 8.0) == pytest.approx(6.0)
    assert source_to_timeline(edl, "v1", 9.0) is None
    assert source_to_timeline(edl, "v1", 1.0, src="/x/other.mp4") is None


def test_timeline_to_source_finds_clip_and_gaps():
    edl = _edl(Clip(id="c_a", src="/x/a.mp4", in_=0, out=3, start=0),
               Clip(id="c_b", src="/x/a.mp4", in_=5, out=8, start=3))
    c, t = timeline_to_source(edl, "v1", 4.0)
    assert (c.id, t) == ("c_b", pytest.approx(6.0))
    assert timeline_to_source(edl, "v1", 6.0) is None      # past the end
    assert timeline_to_source(edl, "v1", 2.999)[0].id == "c_a"
    assert timeline_to_source(edl, "v1", 3.0)[0].id == "c_b"   # seam → the clip that begins


def test_source_range_straddling_a_cut_is_clipped_not_stretched():
    edl = _edl(Clip(src="/x/a.mp4", in_=0, out=3, start=0),
               Clip(src="/x/a.mp4", in_=5, out=8, start=3))
    assert source_range_to_timeline(edl, "v1", 2.5, 3.5) == [(2.5, 3.0)]
    assert source_range_to_timeline(edl, "v1", 4.5, 5.5) == [(3.0, 3.5)]
    # spans the removed middle → one piece per surviving side
    assert source_range_to_timeline(edl, "v1", 2.0, 6.0) == [(2.0, 3.0), (3.0, 4.0)]
    assert source_range_to_timeline(edl, "v1", 3.0, 5.0) == []
    # ends exactly at a cut edge → zero overlap → gone, not a zero-length stub
    assert source_range_to_timeline(edl, "v1", 2.0, 3.0) == [(2.0, 3.0)]
    assert source_range_to_timeline(edl, "v1", 3.0, 3.0) == []


def test_words_drop_clip_and_retime_per_clip():
    edl = _edl(Clip(src="/x/a.mp4", in_=0, out=3, start=0),
               Clip(src="/x/a.mp4", in_=5, out=8, start=3, speed=2.0))
    words = [{"word": "a", "start": 1.0, "end": 1.5},
             {"word": "cut", "start": 3.5, "end": 4.5},
             {"word": "edge", "start": 4.8, "end": 5.4},
             {"word": "fast", "start": 6.0, "end": 7.0, "prob": 0.9}]
    out = map_words_to_timeline(edl, "v1", words)
    assert [w["word"] for w in out] == ["a", "edge", "fast"]
    assert (out[0]["start"], out[0]["end"]) == (1.0, 1.5)
    assert (out[1]["start"], out[1]["end"]) == (pytest.approx(3.0), pytest.approx(3.2))
    assert (out[2]["start"], out[2]["end"]) == (pytest.approx(3.5), pytest.approx(4.0))
    assert out[2]["prob"] == 0.9                       # other keys pass through
    assert words[3]["start"] == 6.0                    # input never mutated


def test_words_map_to_every_occurrence_of_a_reused_region_in_order():
    edl = _edl(Clip(src="/x/a.mp4", in_=0, out=4, start=0),
               Clip(src="/x/a.mp4", in_=0, out=4, start=4))
    out = map_words_to_timeline(edl, "v1", [{"word": "x", "start": 1.0, "end": 1.5}])
    assert [(w["start"], w["end"]) for w in out] == [(1.0, 1.5), (5.0, 5.5)]


def test_words_only_map_through_clips_of_their_own_source():
    edl = _edl(Clip(src="/x/a.mp4", in_=0, out=4, start=0),
               Clip(src="/x/b.mp4", in_=0, out=4, start=4))
    words = [{"word": "x", "start": 1.0, "end": 1.5}]
    assert [(w["start"], w["end"]) for w in map_words_to_timeline(edl, "v1", words, src="/x/a.mp4")] == [(1.0, 1.5)]
    assert [(w["start"], w["end"]) for w in map_words_to_timeline(edl, "v1", words, src="/x/b.mp4")] == [(5.0, 5.5)]
    # no scope: every clip carries it (production always scopes — see the
    # imported-srt tests below for why)
    assert len(map_words_to_timeline(edl, "v1", words)) == 2


def test_a_derived_file_maps_through_its_origin(tmp_path: Path):
    """A reframe/denoise render in the cache plays the upload's source
    seconds; with its `.origin` sidecar the transcript follows it (before:
    the first src-rewriting step of a plan lost the transcript)."""
    from video_ai_editor.agent.media_origin import is_derived, origin_of, record_origin
    upload = tmp_path / "uploads" / "talk" / "talk.normalized.mp4"
    derived = tmp_path / "cache" / "reframe_abc.mp4"
    denoised = tmp_path / "cache" / "denoise" / "denoise_def.mp4"
    for f in (upload, derived, denoised):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    assert record_origin(derived, upload) is not None and is_derived(derived)
    record_origin(denoised, derived)                                   # a chain collapses to the upload
    assert origin_of(denoised) == str(upload) == origin_of(derived) and origin_of(upload) == str(upload)
    assert record_origin(upload, upload) is None                       # a tool returning its input records nothing
    edl = _edl(Clip(src=str(denoised), in_=0, out=4, start=0), Clip(src="/x/b.mp4", in_=0, out=4, start=4))
    words = [{"word": "x", "start": 1.0, "end": 1.5}]
    assert [(w["start"], w["end"]) for w in map_words_to_timeline(edl, "v1", words, src=str(upload))] == [(1.0, 1.5)]
    assert map_words_to_timeline(edl, "v1", words, src="/x/other.mp4") == []


def test_segments_split_by_a_cut_lose_the_removed_words_from_their_text():
    edl = _edl(Clip(src="/x/a.mp4", in_=0, out=3, start=0),
               Clip(src="/x/a.mp4", in_=5, out=8, start=3))
    seg = {"id": 7, "start": 0.0, "end": 8.0, "text": "keep um gone also kept",
           "words": [{"word": " keep", "start": 1.0, "end": 1.5},
                     {"word": " um", "start": 3.5, "end": 3.8},
                     {"word": " gone", "start": 4.0, "end": 4.5},
                     {"word": " also", "start": 6.0, "end": 6.4},
                     {"word": " kept", "start": 6.5, "end": 7.0}]}
    pieces = map_segments_to_timeline(edl, "v1", [seg])
    assert [p["text"] for p in pieces] == ["keep", "also kept"]
    assert [p["id"] for p in pieces] == [7, 7]
    assert (pieces[0]["start"], pieces[0]["end"]) == (0.0, 3.0)   # segment span, clipped
    assert (pieces[1]["start"], pieces[1]["end"]) == (pytest.approx(3.0), pytest.approx(6.0))
    assert [w["word"] for w in pieces[1]["words"]] == [" also", " kept"]


def test_wordless_segment_drops_the_evenly_spaced_tokens_in_the_cut():
    """A translated segment has no words (see _translate_segments_to); the
    tokens are assumed evenly spaced, the same model cues_from_segments uses."""
    edl = _edl(Clip(src="/x/a.mp4", in_=0, out=2, start=0),
               Clip(src="/x/a.mp4", in_=3, out=4, start=2))
    seg = {"id": 0, "start": 0.0, "end": 4.0, "text": "one two three four"}
    pieces = map_segments_to_timeline(edl, "v1", [seg])
    assert [(p["start"], p["end"], p["text"]) for p in pieces] == [
        (0.0, 2.0, "one two"), (2.0, 3.0, "four")]
    assert all("words" not in p for p in pieces)      # still wordless → same fallback


def test_uncut_timeline_returns_segments_unchanged():
    edl = _edl(Clip(src="/x/a.mp4", in_=0, out=12, start=0))
    segs = _transcript()["segments"] + [{"id": 1, "start": 0.3, "end": 2.2, "text": "a  b"}]
    assert map_segments_to_timeline(edl, "v1", segs) == segs


def test_clamp_to_extent():
    assert clamp_to_extent(1.0, 2.0, 8.0) == (1.0, 2.0)     # untouched inside
    assert clamp_to_extent(7.0, 9.5, 8.0) == (7.0, 8.0)     # trailing hold trimmed
    assert clamp_to_extent(8.0, 9.0, 8.0) is None            # starts at/after the end
    assert clamp_to_extent(9.0, 9.5, 8.0) is None


# -------------------------------------- (a) remove_silences → remove_fillers

def test_remove_fillers_after_remove_silences_removes_exactly_the_fillers(tmp_path: Path):
    store = _store(tmp_path)
    _write_ingest(Path(store.edl.get_track("v1").clips[0].src))

    r1 = dispatch(store, "remove_silences", {})
    assert r1["cuts"] == 2, r1
    after_silences = _surviving_source(store)
    # Sanity on the fixture: every word is still on the timeline, the two
    # silent gaps are not.
    for _, s, e in WORDS:
        assert _covered(after_silences, s, e), (after_silences, s, e)
    assert not _on_timeline(after_silences, 3.2, 4.8)
    assert not _on_timeline(after_silences, 8.2, 9.8)

    r2 = dispatch(store, "remove_fillers", {"words": list(FILLERS), "pad": PAD})
    assert r2["cuts"] == 3 and r2["words"] == 3 and r2["already_removed"] == 0, r2
    after = _surviving_source(store)

    # The fillers' SOURCE ranges (padded) are gone…
    for w, s, e in WORDS:
        if w in FILLERS:
            assert not _on_timeline(after, s - PAD, e + PAD), (w, s, e, after)
    # …and every non-filler word is still intact, each inside one clip. This is
    # the assertion the pre-fix code failed: fed source times as timeline
    # times after the silences moved everything left, it cut real speech.
    for w, s, e in WORDS:
        if w not in FILLERS:
            assert _covered(after, s, e), (w, s, e, after)
    # Nothing ELSE was removed: what survives is the pre-filler timeline minus
    # exactly the three padded filler ranges.
    removed_by_fillers = sum(hi - lo for lo, hi in after_silences) - sum(hi - lo for lo, hi in after)
    assert removed_by_fillers == pytest.approx(3 * (0.3 + 2 * PAD), abs=2e-3)

    # One undo step for the pass, as before.
    dispatch(store, "undo", {})
    assert _surviving_source(store) == after_silences


def test_remove_fillers_skips_words_already_cut_and_merges_overlaps(tmp_path: Path):
    store = _store(tmp_path)
    src = Path(store.edl.get_track("v1").clips[0].src)
    # "um uh" back to back: padded ranges overlap → one merged cut, not two
    # overlapping cuts that would take a slice of "then" as well.
    words = [("um", 1.00, 1.30), ("uh", 1.32, 1.60), ("then", 1.70, 2.20),
             ("like", 6.00, 6.30), ("done", 7.00, 7.50)]
    _write_ingest(src, _transcript(words))
    # Cut source [5,8) first — the "like" is gone before remove_fillers runs.
    dispatch(store, "cut_range", {"track": "v1", "start": 5.0, "end": 8.0})

    r = dispatch(store, "remove_fillers", {"words": ["um", "uh", "like"], "pad": PAD})
    assert (r["cuts"], r["words"], r["already_removed"]) == (1, 2, 1), r
    after = _surviving_source(store)
    assert not _on_timeline(after, 1.0 - PAD, 1.6 + PAD)
    assert _covered(after, 1.70, 2.20) and _covered(after, 0.0, 0.9)
    assert after == [(0.0, 0.95), (1.65, 5.0), (8.0, 12.0)]


def test_remove_fillers_cuts_both_occurrences_of_a_reused_region(tmp_path: Path):
    store = _store(tmp_path)
    src = Path(store.edl.get_track("v1").clips[0].src)
    _write_ingest(src, _transcript([("um", 1.0, 1.3), ("word", 2.0, 2.5)]))
    dispatch(store, "duplicate_clip", {"clip_id": store.edl.get_track("v1").clips[0].id})
    assert len(store.edl.get_track("v1").clips) == 2

    r = dispatch(store, "remove_fillers", {"words": ["um"], "pad": 0.0})
    assert r["cuts"] == 2
    after = _surviving_source(store)
    assert after == [(0.0, 1.0), (1.3, 12.0), (0.0, 1.0), (1.3, 12.0)]


# -------------------------------------------------- (d) a 2x-speed clip

def test_remove_fillers_on_a_2x_clip_cuts_the_source_word_not_twice_as_deep(tmp_path: Path):
    d = tmp_path / "fast"
    d.mkdir()
    src = d / "a.mp4"                       # never opened: cut_range is pure EDL math
    store = EDLStore(tmp_path / "sess")
    store.edl.get_track("v1").clips.append(
        Clip(id="c_fast", src=str(src), in_=0, out=12, start=0.0, speed=2.0))
    store.commit("seed", {}, "seed")
    _write_ingest(src, _transcript([("hi", 1.0, 1.4), ("um", 5.5, 5.8), ("bye", 9.0, 9.5)]))

    r = dispatch(store, "remove_fillers", {"words": ["um"], "pad": 0.0})
    assert r["cuts"] == 1
    # Source [5.5,5.8] plays at timeline [2.75,2.9] on a 2x clip; the pre-fix
    # cut_range(5.5,5.8) would have removed source [11,11.6] — "bye" territory.
    assert _surviving_source(store) == [(0.0, 5.5), (5.8, 12.0)]


def test_add_caption_track_on_a_2x_clip_lays_cues_at_timeline_time(tmp_path: Path):
    d = tmp_path / "fast"
    d.mkdir()
    src = d / "a.mp4"
    store = EDLStore(tmp_path / "sess")
    store.edl.get_track("v1").clips.append(
        Clip(id="c_fast", src=str(src), in_=0, out=12, start=0.0, speed=2.0))
    store.commit("seed", {}, "seed")
    _write_ingest(src, _transcript([("hi", 1.0, 1.4), ("there", 5.5, 5.8)]))

    dispatch(store, "add_caption_track", {"style": "word_emphasis", "chunk_size": 1})
    assert [(c.start, c.end, c.text) for c in _captions(store)] == [
        (pytest.approx(0.5), pytest.approx(0.7), "HI"),
        (pytest.approx(2.75), pytest.approx(2.9), "THERE")]
    assert max(c.end for c in _captions(store)) <= store.edl.video_extent()


# --------------------------------------------- (b) auto_caption after cuts

def _cut_both_silences(store: EDLStore) -> None:
    """Deterministic stand-in for remove_silences: the same two source gaps."""
    dispatch(store, "cut_range", {"track": "v1", "start": 8.0, "end": 10.0})
    dispatch(store, "cut_range", {"track": "v1", "start": 3.0, "end": 5.0})
    assert store.edl.video_extent() == pytest.approx(8.0)


def test_auto_caption_after_cuts_stays_inside_v1_and_drops_removed_words(tmp_path: Path, fake_whisper):
    store = _store(tmp_path)
    _cut_both_silences(store)
    # Put a spoken word INSIDE each removed gap so the transcript (which
    # Whisper produces from the whole FILE) has content the timeline no
    # longer shows — exactly the real-clip situation.
    words = sorted(WORDS + [("cutword", 3.5, 4.5), ("um", 8.5, 9.0)], key=lambda w: w[1])
    fake_whisper["transcript"] = _transcript(words)
    _write_ingest(Path(store.edl.get_track("v1").clips[0].src), _transcript(words))
    r0 = dispatch(store, "remove_fillers", {"words": list(FILLERS), "pad": PAD})
    assert (r0["words"], r0["already_removed"]) == (3, 1), r0
    # (the fillers left on the timeline are gone too; the transcript still has them)

    r = dispatch(store, "auto_caption", {"style": "ig_chunky"})
    cues = _captions(store)
    extent = store.edl.video_extent()
    assert r["cues"] == len(cues) >= 2
    assert all(0.0 <= c.start < c.end <= extent + 1e-9 for c in cues), [(c.start, c.end) for c in cues]
    joined = " ".join(c.text.replace("\n", " ") for c in cues).split()
    assert "cutword" not in joined and "um" not in joined and "uh" not in joined, joined
    assert {"hello", "there", "today", "goodbye"} <= set(joined)
    # The project did not grow past the video: this is the 34s-render defect.
    assert store.edl.duration == pytest.approx(extent)
    # A cue count that makes sense for ~8 surviving words: not one per word,
    # not one for everything.
    assert 2 <= len(cues) <= len(words)


def test_auto_caption_word_emphasis_after_cuts_also_stays_inside_v1(tmp_path: Path, fake_whisper):
    store = _store(tmp_path)
    _cut_both_silences(store)
    fake_whisper["transcript"] = _transcript(WORDS + [("tail", 11.7, 12.6)])
    dispatch(store, "auto_caption", {"style": "word_emphasis", "chunk_size": 1})
    cues = _captions(store)
    extent = store.edl.video_extent()
    assert [c.text for c in cues] == [w.upper() for w, _, _ in WORDS] + ["TAIL"]
    assert all(c.end <= extent + 1e-9 for c in cues)
    # source "um" 10.5–10.8 → after removing [3,5) and [8,10) → timeline 6.5–6.8
    um = [c for c in cues if c.text == "UM"][-1]
    assert (um.start, um.end) == (pytest.approx(6.5), pytest.approx(6.8))
    # a word running past the end of the file is clipped at the extent
    assert cues[-1].end == pytest.approx(extent)


def test_auto_caption_persists_the_source_time_transcript_not_the_mapped_one(tmp_path: Path, fake_whisper):
    """ingest.json is the record of the FILE (get_transcript, export_srt,
    find_moments index it by source time); only the caption clips follow the
    cuts. A later auto_caption after MORE cuts must still start from the
    whole-file transcript."""
    store = _store(tmp_path)
    src = Path(store.edl.get_track("v1").clips[0].src)
    _cut_both_silences(store)
    dispatch(store, "auto_caption", {})
    persisted = json.loads((src.parent / "ingest.json").read_text())["transcript"]
    assert persisted["segments"] == _transcript()["segments"]


# ------------------------------------ (c) uncut timeline: identical output

def test_uncut_auto_caption_output_is_identical_to_the_raw_cue_build(tmp_path: Path, fake_whisper):
    """No cuts → the caption track must be EXACTLY (==, not approx) what
    cues_from_segments builds from the raw transcript, which is what the tool
    laid down before the mapping existed."""
    store = _store(tmp_path)
    dispatch(store, "auto_caption", {"style": "ig_chunky", "max_chars": 42, "max_cps": 17.0})
    expected = [(c.start, c.end, c.text)
                for c in cues_from_segments(_transcript()["segments"], max_chars=42, max_cps=17.0)]
    assert expected and _cue_tuples(store) == expected

    dispatch(store, "auto_caption", {"style": "word_emphasis", "chunk_size": 2})
    ws = _transcript()["segments"][0]["words"]
    expected_we = [(ws[i]["start"], ws[i + 1 if i + 1 < len(ws) else i]["end"],
                    " ".join(w["word"] for w in ws[i:i + 2]).upper())
                   for i in range(0, len(ws), 2)]
    assert _cue_tuples(store) == expected_we


def test_uncut_add_caption_track_output_is_identical_to_the_raw_segments(tmp_path: Path):
    store = _store(tmp_path)
    src = Path(store.edl.get_track("v1").clips[0].src)
    # Two segments, one wordless (an imported/translated shape), with a
    # segment span wider than its words — the span is what `default` uses.
    tx = _transcript()
    tx["segments"][0]["start"] = 0.05
    tx["segments"].append({"id": 1, "start": 6.9, "end": 11.95, "text": "no  word timing here"})
    _write_ingest(src, tx)

    dispatch(store, "add_caption_track", {"style": "default"})
    assert _cue_tuples(store) == [(0.05, 11.6, tx["segments"][0]["text"]),
                                  (6.9, 11.95, "no  word timing here")]

    dispatch(store, "add_caption_track", {"style": "word_emphasis", "chunk_size": 3})
    ws = tx["segments"][0]["words"]
    expected = [(ws[i]["start"], ws[min(i + 2, len(ws) - 1)]["end"],
                 " ".join(w["word"] for w in ws[i:i + 3]).upper())
                for i in range(0, len(ws), 3)] + [(6.9, 11.95, "no  word timing here")]
    assert _cue_tuples(store) == expected


# ------------------------------------------------- (e) two sources on v1

def test_two_sources_on_v1_only_the_transcribed_files_clips_carry_its_words(tmp_path: Path):
    a = _speech_clip(tmp_path, "a")
    b = _speech_clip(tmp_path, "b")
    store = _store(tmp_path, a)
    dispatch(store, "add_clip", {"track": "v1", "src": str(b), "in": 0, "out": CLIP_DUR,
                                 "start": CLIP_DUR})
    # The transcript belongs to `a` (its ingest.json); `b` has none.
    _write_ingest(a, _transcript([("um", 1.0, 1.3), ("hello", 2.0, 2.5)]))

    dispatch(store, "add_caption_track", {"style": "word_emphasis", "chunk_size": 1})
    assert _cue_tuples(store) == [(1.0, 1.3, "UM"), (2.0, 2.5, "HELLO")]   # not also at 13.0

    r = dispatch(store, "remove_fillers", {"words": ["um"], "pad": 0.0})
    assert r["cuts"] == 1
    v1 = sorted(store.edl.get_track("v1").clips, key=lambda c: c.start)
    assert [(Path(c.src).name, c.in_, c.out) for c in v1] == [
        ("a.normalized.mp4", 0.0, 1.0), ("a.normalized.mp4", 1.3, 12.0),
        ("b.normalized.mp4", 0.0, 12.0)]           # b untouched


def test_auto_caption_with_two_sources_maps_only_through_the_transcribed_clip(tmp_path: Path, fake_whisper):
    a = _speech_clip(tmp_path, "a")
    b = _speech_clip(tmp_path, "b")
    store = _store(tmp_path, a)
    dispatch(store, "add_clip", {"track": "v1", "src": str(b), "in": 0, "out": CLIP_DUR,
                                 "start": CLIP_DUR})
    fake_whisper["transcript"] = _transcript([("hello", 2.0, 2.5), ("world", 2.6, 3.0)])
    dispatch(store, "auto_caption", {"style": "word_emphasis", "chunk_size": 1})
    assert [(c.start, c.text) for c in _captions(store)] == [(2.0, "HELLO"), (2.6, "WORLD")]


# -------------------------------------------- remove_silences shares the math

def test_remove_silences_still_converts_through_the_shared_mapper(tmp_path: Path, monkeypatch):
    """The refactor onto timemap must keep test_speed_effective_duration's
    contract: a silence at SOURCE [2,4) in a 2x clip is cut at TIMELINE [1,2)."""
    store = EDLStore(tmp_path / "sess")
    v1 = store.edl.get_track("v1")
    v1.clips.append(Clip(id="c_A", src="/x/a.mp4", in_=0, out=10, start=0.0, speed=2.0))
    store.commit("seed", {}, "seed")

    class _Proc:
        returncode, stdout = 0, ""
        stderr = "silence_start: 2.0\nsilence_end: 4.0\n"

    real_run = subprocess.run

    def fake_run(argv, *a, **kw):
        if "silencedetect" in " ".join(map(str, argv)):
            return _Proc()
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", fake_run)
    dispatch(store, "remove_silences", {"track": "v1", "keep_pad": 0.0, "min_dur": 0.5})
    assert _surviving_source(store) == [(0.0, 2.0), (4.0, 10.0)]


# ------------------------------------------ review findings, second pass

def _stub_silencedetect(monkeypatch, stderr: str) -> None:
    """Make every ffmpeg silencedetect call report `stderr`; nothing else
    is intercepted, so the lavfi clip synthesis still runs for real."""
    class _Proc:
        returncode, stdout = 0, ""

    _Proc.stderr = stderr
    real_run = subprocess.run

    def fake_run(argv, *a, **kw):
        if "silencedetect" in " ".join(map(str, argv)):
            return _Proc()
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_remove_fillers_with_a_leading_gap_on_v1_removes_the_right_source(tmp_path: Path):
    """cut_range ends with _ripple_close_gap, which re-packs v1 from timeline
    0 — so the FIRST cut closes a leading gap and shifts every earlier clip
    left. Timeline ranges precomputed against the pre-cut layout are then
    stale by the gap width; cutting back-to-front does not help. Found by
    the review with add_clip start=2.0: only the last filler was removed and
    two ranges of real speech went instead. Each cut must be re-mapped from
    SOURCE through the live EDL."""
    src = _speech_clip(tmp_path)
    store = EDLStore(tmp_path / "sess")
    dispatch(store, "add_clip", {"track": "v1", "src": str(src),
                                 "in": 0, "out": CLIP_DUR, "start": 2.0})
    assert store.edl.get_track("v1").clips[0].start == 2.0
    _write_ingest(src, _transcript([("um", 0.6, 0.9), ("mid", 3.0, 3.3),
                                    ("uh", 5.5, 5.8), ("um", 10.5, 10.8)]))

    r = dispatch(store, "remove_fillers", {"words": list(FILLERS), "pad": PAD})
    assert (r["cuts"], r["words"], r["already_removed"]) == (3, 3, 0), r
    assert _surviving_source(store) == [
        (0.0, 0.55), (0.95, 5.45), (5.85, 10.45), (10.85, 12.0)]


def test_remove_silences_with_a_leading_gap_on_v1_removes_the_right_source(tmp_path: Path, monkeypatch):
    """Same ripple defect in remove_silences (pre-existing at HEAD): with the
    clip at start=2.0 the detected silence at source [3,5) was cut 2s too
    deep, at source [5,7)."""
    src = _speech_clip(tmp_path)
    store = EDLStore(tmp_path / "sess")
    dispatch(store, "add_clip", {"track": "v1", "src": str(src),
                                 "in": 0, "out": CLIP_DUR, "start": 2.0})
    _stub_silencedetect(monkeypatch,
                        "silence_start: 3.0\nsilence_end: 5.0\n"
                        "silence_start: 8.0\nsilence_end: 10.0\n")
    r = dispatch(store, "remove_silences", {"keep_pad": 0.0, "min_dur": 0.5})
    assert r["cuts"] == 2
    assert _surviving_source(store) == [(0.0, 3.0), (5.0, 8.0), (10.0, 12.0)]


def test_remove_fillers_presence_is_decided_on_the_unpadded_word(tmp_path: Path):
    """A filler whose spoken range is already gone must be reported as
    already removed and leave the timeline alone — even when its PAD would
    still touch surviving footage. Deciding on the padded range cut 2*pad of
    the neighbouring words and claimed a filler was removed."""
    store = _store(tmp_path)
    src = Path(store.edl.get_track("v1").clips[0].src)
    _write_ingest(src, _transcript([("hi", 0.2, 0.6), ("um", 1.0, 1.3), ("there", 1.4, 1.9)]))
    dispatch(store, "cut_range", {"track": "v1", "start": 1.0, "end": 1.3})
    before = _surviving_source(store)
    assert before == [(0.0, 1.0), (1.3, 12.0)]

    r = dispatch(store, "remove_fillers", {"words": ["um"], "pad": 0.1})
    assert (r["cuts"], r["words"], r["already_removed"]) == (0, 0, 1), r
    assert _surviving_source(store) == before


def test_remove_fillers_second_pass_with_a_bigger_pad_does_not_eat_the_neighbour(tmp_path: Path):
    store = _store(tmp_path)
    src = Path(store.edl.get_track("v1").clips[0].src)
    _write_ingest(src, _transcript([("um", 1.0, 1.3), ("uh", 1.5, 1.8), ("word", 2.0, 2.5)]))
    r1 = dispatch(store, "remove_fillers", {"words": ["um"], "pad": 0.05})
    assert r1["cuts"] == 1
    after_first = _surviving_source(store)
    assert after_first == [(0.0, 0.95), (1.35, 12.0)]

    r2 = dispatch(store, "remove_fillers", {"words": ["um"], "pad": 0.3})
    assert (r2["cuts"], r2["already_removed"]) == (0, 1), r2
    assert _surviving_source(store) == after_first          # "uh" intact


def _write_srt(path: Path, cues: list[tuple[float, float, str]]) -> Path:
    def ts(t: float) -> str:
        ms = int(round(t * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    path.write_text("\n".join(f"{i}\n{ts(s)} --> {ts(e)}\n{txt}\n"
                              for i, (s, e, txt) in enumerate(cues, 1)), encoding="utf-8")
    return path


def test_imported_srt_is_single_origin_on_a_two_source_timeline(tmp_path: Path):
    """An imported subtitle file is a transcript of ONE file — the first v1
    clip's, exactly like ingest.json — not of every clip on the track. With
    origin=None the mapper fanned it out through b too: remove_fillers cut
    b's footage and add_caption_track laid every cue twice, both regressions
    against HEAD on an UNCUT timeline."""
    a = _speech_clip(tmp_path, "a")
    b = _speech_clip(tmp_path, "b")
    store = _store(tmp_path, a)
    dispatch(store, "add_clip", {"track": "v1", "src": str(b), "in": 0, "out": CLIP_DUR,
                                 "start": CLIP_DUR})
    srt = _write_srt(tmp_path / "in.srt", [(1.0, 1.3, "um"), (2.0, 2.5, "hello")])
    dispatch(store, "import_srt", {"path": str(srt)})

    dispatch(store, "add_caption_track", {})
    assert _cue_tuples(store) == [(1.0, 1.3, "um"), (2.0, 2.5, "hello")]    # once, not at 13.0 too

    r = dispatch(store, "remove_fillers", {"words": ["um"], "pad": 0.0})
    assert r["cuts"] == 1
    v1 = sorted(store.edl.get_track("v1").clips, key=lambda c: c.start)
    assert [(Path(c.src).name, c.in_, c.out) for c in v1] == [
        ("a.normalized.mp4", 0.0, 1.0), ("a.normalized.mp4", 1.3, 12.0),
        ("b.normalized.mp4", 0.0, 12.0)]


def test_imported_srt_on_a_duplicated_clip_lays_cues_once_per_occurrence(tmp_path: Path):
    """The same file placed twice DOES carry its captions twice — that is the
    reuse rule — but only because both clips play the transcript's file."""
    store = _store(tmp_path)
    srt = _write_srt(tmp_path / "in.srt", [(2.0, 2.5, "hello")])
    dispatch(store, "import_srt", {"path": str(srt)})
    dispatch(store, "duplicate_clip", {"clip_id": store.edl.get_track("v1").clips[0].id})
    dispatch(store, "add_caption_track", {})
    assert _cue_tuples(store) == [(2.0, 2.5, "hello"), (14.0, 14.5, "hello")]


def test_source_to_timeline_only_accepts_the_true_end_of_the_shown_file():
    """The closed-end fallback exists so 'where does the end of the file
    land' answers with the extent. It must NOT fire at an interior cut edge:
    that instant was cut away and the contract says None."""
    edl = _edl(Clip(src="/x/a.mp4", in_=0, out=3, start=0),
               Clip(src="/x/a.mp4", in_=5, out=8, start=3))
    assert source_to_timeline(edl, "v1", 3.0) is None       # first removed instant
    assert source_to_timeline(edl, "v1", 8.0) == pytest.approx(6.0)
    edl2 = _edl(Clip(src="/x/a.mp4", in_=2, out=5, start=0, speed=2.0),
                Clip(src="/x/a.mp4", in_=7, out=9, start=1.5))
    assert source_to_timeline(edl2, "v1", 5.0) is None
    assert source_to_timeline(edl2, "v1", 7.0) == pytest.approx(1.5)
    assert source_to_timeline(edl2, "v1", 9.0) == pytest.approx(3.5)
    # a region on the timeline twice: its end resolves to the FIRST occurrence,
    # consistent with the point query
    edl3 = _edl(Clip(src="/x/a.mp4", in_=0, out=4, start=0),
                Clip(src="/x/a.mp4", in_=0, out=4, start=4))
    assert source_to_timeline(edl3, "v1", 4.0) == pytest.approx(4.0)


def test_clamp_keeps_zero_length_spans_and_pins_the_trailing_trim():
    # a zero-length word on an uncut timeline was laid before the mapper
    # existed; the clamp must not be the thing that drops it
    assert clamp_to_extent(2.0, 2.0, 8.0) == (2.0, 2.0)
    assert clamp_to_extent(3.0, 2.0, 8.0) is None            # inverted → gone


def test_uncut_timeline_trailing_hold_is_trimmed_to_the_footage(tmp_path: Path, fake_whisper):
    """The ONE deliberate deviation from 'byte-identical when uncut': a cue
    that would run past the end of the footage is trimmed to it. Uncut, the
    build_cues reading-speed hold past the last word used to push
    edl.duration past the video and render a black tail with a caption on it
    — the same defect as after cuts, on a smaller scale. Pinned here so the
    trade-off is explicit."""
    store = _store(tmp_path)
    words = [("start", 0.5, 1.0), ("zip", 2.0, 2.0), ("hi", 11.5, 11.9)]
    fake_whisper["transcript"] = _transcript(words)
    dispatch(store, "auto_caption", {"style": "ig_chunky"})
    cues = _cue_tuples(store)
    raw = [(c.start, c.end, c.text)
           for c in cues_from_segments(_transcript(words)["segments"], max_chars=42, max_cps=17.0)]
    assert raw[-1][1] > CLIP_DUR                           # the raw build overhangs
    assert cues[:-1] == raw[:-1]
    assert cues[-1] == (raw[-1][0], CLIP_DUR, raw[-1][2])
    assert store.edl.duration == pytest.approx(CLIP_DUR)

    dispatch(store, "auto_caption", {"style": "word_emphasis", "chunk_size": 1})
    assert _cue_tuples(store) == [(0.5, 1.0, "START"), (2.0, 2.0, "ZIP"), (11.5, 11.9, "HI")]


def test_export_srt_is_deliberately_source_timed(tmp_path: Path):
    """export_srt/vtt/ass write the transcript as the record of the FILE
    (like get_transcript and ingest.json), not the on-timeline caption track.
    Pinned so a change of that decision is a visible change."""
    store = _store(tmp_path)
    src = Path(store.edl.get_track("v1").clips[0].src)
    _write_ingest(src)
    _cut_both_silences(store)
    r = dispatch(store, "export_srt", {"path": str(tmp_path / "out.srt")})
    body = Path(r["path"]).read_text(encoding="utf-8")
    assert "00:00:00,200 --> 00:00:11,600" in body
