"""render/frame_map.py against the REAL compositor (instant preview §6 R4, §13).

Three layers:

* every golden case (tests/goldens/frame_map/*.json — bar-coded sources
  rendered by ``render_preview`` AND the export's single pass, decoded frame
  by frame) is reproduced by the model, and the stored model JSON (the RLE,
  seams and sound placement the TypeScript port is pinned to) is current;
* a subset marked ``live`` is re-rendered NOW through both render paths and
  decoded again — the goldens cannot go stale against the compositor;
* unit tests for the arithmetic the model rests on (µs parsing, rescale
  rounding, the muxer's default time base, MSE ticks, RLE).

Regenerate the goldens with ``.venv/bin/python tests/gen_frame_map_goldens.py``.
"""
from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

import frame_map_golden_lib as lib
from gen_frame_map_goldens import split_screen_transitions, with_paths
from video_ai_editor.edl import timebase as tb
from video_ai_editor.edl.schema import EDL, Canvas, Clip, Transition, empty_edl
from video_ai_editor.render import compositor, render_preview, segments
from video_ai_editor.render.frame_map import (
    KIND_BLEND, KIND_GAP, SourceInfo, build_program_map, default_time_base, ffmpeg_us,
    frame_map_json, rescale, rle_frames, round_half_away, select_frames, ticks_per_frame,
    to_rle,
)

GOLDENS = lib.load_goldens()
BY_NAME = {c["name"]: c for c in GOLDENS}
LIVE = [c["name"] for c in GOLDENS if c["live"]]


def _edl(case: dict) -> EDL:
    return EDL.model_validate(case["edl"])


def _sources(case: dict) -> tuple[dict[str, SourceInfo], dict[str, int]]:
    infos = {k: SourceInfo.from_json(v) for k, v in case["sources"].items()}
    return infos, {k: v["sid"] for k, v in case["sources"].items()}


# ---------------------------------------------------------------- goldens

def test_goldens_cover_the_matrix():
    groups = {c["group"] for c in GOLDENS}
    assert groups == {"rates", "structure", "transitions", "segments", "fuzz"}
    rates = {c["name"] for c in GOLDENS if c["group"] == "rates"}
    for r in lib.PROJECT_RATES:
        for s in lib.PROJECT_RATES:
            assert f"rates_p{lib.rate_name(r)}_s{lib.rate_name(s)}" in rates
    assert sum(c["total"] for c in GOLDENS) > 20000
    # Every rule the model encodes is exercised somewhere.
    runs = [r for c in GOLDENS for r in c["model"]["runs"]]
    assert any(r["kind"] == KIND_BLEND and r["nested"] for r in runs)   # 3 clips at once
    assert any(r["kind"] == KIND_GAP for r in runs)
    assert any(r.get("a", {}).get("step") == -1 for r in runs)         # reverse
    # Freeze: a 0.5 s source played to 0.9 s holds its last frame (tpad).
    frz = rle_frames(BY_NAME["freeze_p30"]["model"]["runs"])
    assert sum(1 for f in frz if f["src"] == "short30" and f["frame"] == 14) >= 12
    assert any("d" in r.get("a", {}) for r in runs)                    # non-linear conform


def test_every_golden_was_identical_through_preview_and_export():
    """The preview (chunk cache, segmented long clips, stream-copy assembly)
    and the export's single pass showed the same frame at every k."""
    assert [c["name"] for c in GOLDENS if not c.get("preview_matches_export")] == []


@pytest.mark.parametrize("name", sorted(BY_NAME))
def test_model_reproduces_the_render(name):
    case = BY_NAME[name]
    edl = _edl(case)
    infos, sids = _sources(case)
    exp = lib.expected_frames(edl, infos, sids)
    errs = lib.compare(lib.expand_measured(case["measured"]), exp)
    assert errs == [], "\n".join(errs[:15])
    stored = case["model"]
    now = lib.model_json(edl, infos)
    assert json.loads(json.dumps(now)) == stored, "stored model JSON is stale — regenerate goldens"


@pytest.mark.parametrize("name", sorted(BY_NAME))
def test_rle_round_trips(name):
    case = BY_NAME[name]
    edl = _edl(case)
    infos, _ = _sources(case)
    pm = build_program_map(edl, infos)
    frames = rle_frames(to_rle(pm))
    assert len(frames) == pm.total
    for k, f in enumerate(frames):
        assert f["kind"] == pm.kind[k]
        assert f["frame"] == pm.frame[k]
        if pm.kind[k] == KIND_BLEND:
            assert f["b_frame"] == pm.b_frame[k]
            assert f["p"] == [pm.p_num[k], pm.p_den[k]]
            assert f["nested"] == pm.nested[k]


def test_goldens_discriminate_a_naive_rule():
    """A plausible-but-wrong rule (nearest source frame to the output
    instant, no seek pre-roll / tick arithmetic) disagrees with the renders —
    the tables pin the real rule, not just "roughly right"."""
    wrong = 0
    for case in GOLDENS:
        if case["group"] != "rates":
            continue
        edl = _edl(case)
        infos, sids = _sources(case)
        r = tb.rate_of(edl.canvas.fps)
        pm = build_program_map(edl, infos)
        for k in range(pm.total):
            if pm.kind[k] != 0:
                continue
            c = pm.clips[pm.clip[k]]
            if c.reverse or not isinstance(c.speed, (int, float, type(None))):
                continue
            s = infos[c.src].rate
            j = k - pm.clip_start[pm.clip[k]]
            naive = int(round(float(c.in_) * float(s) + j * (c.speed or 1) * float(s / r)))
            if naive != pm.frame[k]:
                wrong += 1
    assert wrong > 100


# ---------------------------------------------------------------- live renders

@pytest.fixture(scope="module")
def live_sources(tmp_path_factory) -> dict[str, str]:
    needed = {k for n in LIVE for k in BY_NAME[n]["sources"]} | {lib.MUSIC_SRC}
    d = tmp_path_factory.mktemp("fm_sources")
    out = {}
    for spec in lib.source_specs():
        if spec.key in needed:
            p = lib.make_bar_source(d / f"{spec.key}.mp4", spec)
            out[spec.key] = str(p)
            probed = lib.probe_source(p)
            golden = BY_NAME[next(n for n in LIVE if spec.key in BY_NAME[n]["sources"])] \
                if any(spec.key in BY_NAME[n]["sources"] for n in LIVE) else None
            if golden:
                # The regenerated source is the one the golden was made from.
                assert probed.to_json() == {k: v for k, v in golden["sources"][spec.key].items()
                                            if k != "sid"}
    return out


@pytest.mark.parametrize("name", LIVE)
def test_live_render_matches_golden_and_model(name, live_sources, tmp_path):
    case = BY_NAME[name]
    edl = _edl(case)
    infos, sids = _sources(case)
    real = with_paths(edl, live_sources)
    with split_screen_transitions():
        prev = render_preview(real, tmp_path / "s").path
        exp = compositor._render(real, tmp_path / "export.mp4", height=lib.H, fps=real.canvas.fps,
                                 preview=False, cache_dir=tmp_path / "s" / "cache", chunked=False)
    model = lib.expected_frames(edl, infos, sids)
    golden = lib.expand_measured(case["measured"])
    for label, path in (("preview", prev), ("export", exp)):
        m = lib.measure(path)
        errs = lib.compare(m, model)
        assert errs == [], f"{label} vs model:\n" + "\n".join(errs[:12])
        errs = lib.compare(m, golden, check_p=False)
        assert errs == [], f"{label} vs golden:\n" + "\n".join(errs[:12])


# ---------------------------------------------------------------- arithmetic

def test_mse_ticks_per_frame_is_an_integer_for_every_standard_rate():
    got = {float(r): ticks_per_frame(r) for r in tb.STANDARD_RATES}
    assert list(got.values()) == [10010, 10000, 9600, 8008, 8000, 5000, 4800, 4004, 4000]
    assert ticks_per_frame(Fraction(7)) is None          # R1 refusal
    assert ticks_per_frame(29.97) == 8008                  # a stored float snaps


def test_ffmpeg_us_is_the_parsed_percent_6f():
    assert ffmpeg_us(13.596917) == 13596917
    assert ffmpeg_us(0.0078125) == 7812                    # half-even, like Python formatting
    assert ffmpeg_us(1 / 3) == 333333
    assert ffmpeg_us(-0.5) == -500000


def test_rescale_rounds_half_away_from_zero():
    assert round_half_away(Fraction(1, 2)) == 1
    assert round_half_away(Fraction(-1, 2)) == -1
    assert round_half_away(Fraction(5, 2)) == 3
    assert rescale(256, Fraction(1, 15360), Fraction(1, 60)) == 1
    assert rescale(-16683, Fraction(1, 1_000_000), Fraction(1, 15360)) == -256


@pytest.mark.parametrize("rate", list(tb.STANDARD_RATES))
def test_default_time_base_is_what_the_muxer_writes(rate, tmp_path):
    """The reversed intermediate (.mov, libx264 at the project rate) gets the
    muxer's default time base — the model derives it instead of probing."""
    out = tmp_path / "t.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"color=s=16x16:r={tb.ffmpeg_rate(rate)}:d=0.2",
                    "-c:v", "libx264", "-preset", "ultrafast", "-r", tb.ffmpeg_rate(rate),
                    str(out)], check=True, capture_output=True)
    probed = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                             "stream=time_base", "-of", "csv=p=0", str(out)],
                            check=True, capture_output=True, text=True).stdout.strip()
    assert Fraction(probed) == default_time_base(rate)


def test_select_frames_conforms_holds_and_freezes():
    src = SourceInfo.cfr(25, 10)
    # 25 → 30: every fifth output frame repeats a source frame.
    got = select_frames(src, seek_us=None, dur_us=10_000_000, n=12, fps=30)
    assert got == [0, 1, 2, 2, 3, 4, 5, 6, 7, 7, 8, 9]
    # A source that runs out freezes on its last frame (tpad clone).
    assert select_frames(src, seek_us=None, dur_us=10_000_000, n=14, fps=25)[-4:] == [9, 9, 9, 9]
    # 2x at the same rate steps by two; the trim duration bounds decoding.
    assert select_frames(src, seek_us=None, dur_us=10_000_000, n=5, fps=25, speed=2.0) == [0, 2, 4, 6, 8]
    assert select_frames(src, seek_us=None, dur_us=120_000, n=5, fps=25) == [0, 1, 2, 2, 2]


def test_source_info_from_the_proxy_probe_matches_ffprobe(tmp_path):
    """The frame_map route builds SourceInfo from the proxy lane's probe (its
    packet pts table); for an import-shaped CFR master that is exactly what
    ffprobe reports — and a VFR table is kept verbatim."""
    from video_ai_editor.ingest import proxy
    spec = next(s for s in lib.source_specs() if s.key == "bar29.97_90k")
    p = lib.make_bar_source(tmp_path / "s.mp4", spec)
    got = SourceInfo.from_proxy(proxy.probe_source(p))
    assert got == lib.probe_source(p)
    assert got.pts_table is None

    class Vfr:
        rate, time_base, width, height, audio_start = Fraction(30), Fraction(1, 90000), 64, 36, 0.0
        pts = [0, 3000, 6000, 9500, 12000]
    vfr = SourceInfo.from_proxy(Vfr)
    assert vfr.pts_table == (0, 3000, 6000, 9500, 12000)
    assert [vfr.pts(i) for i in (3, 5, 6)] == [9500, 14500, 17000]


def test_frame_map_json_shape():
    case = BY_NAME["xfade_p30"]
    edl = _edl(case)
    infos, _ = _sources(case)
    d = frame_map_json(edl, infos)
    assert d["render_hash"] == edl.render_hash()
    assert d["R"] == [30, 1] and d["T"] == 8000
    assert sum(r["n"] for r in d["runs"]) == d["total"]
    assert {a["mode"] for a in d["audio"]} <= {"exact", "varispeed", "tempo", "reverse"}
    assert d["seams"] and all(s["frames"] >= 1 for s in d["seams"])


def test_transition_kinds_golden_is_current():
    """support.ts BAKEs every transition the client has no port for — the
    custom-expr and post-filter looks and their aliases. The list lives in
    render/transitions.py; this golden carries it to the vitest suite."""
    from video_ai_editor.render import transitions as tr
    custom = set(tr.CUSTOM_EXPRS) | {a for a, c in tr.ALIASES.items() if c in tr.CUSTOM_EXPRS}
    post = set(tr.POST_FILTERS) | {a for a, c in tr.ALIASES.items() if c in tr.POST_FILTERS}
    doc = {"custom": sorted(custom), "post": sorted(post)}
    path = lib.GOLDEN_DIR.parent / "transition_kinds.json"
    import os
    if os.environ.get("VAI_REGEN_GOLDENS") == "1" or not path.exists():
        path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    assert json.loads(path.read_text()) == doc


# ---------------------------------------------------------------- compositor fixes

def _fc_for(edl: EDL) -> str:
    clips = compositor._video_clips(edl)
    v1 = edl.get_track("v1")
    fc, *_ = compositor._build_filter_complex(
        clips, 320, 180, transitions=v1.transitions, fps=edl.canvas.fps,
        total_duration=edl.duration + edl.transition_overlap())
    return fc


def test_xfade_inputs_share_a_one_frame_clock():
    """Seams on an AVTB clock started a frame late whenever µs roundings put
    the boundary frame 1 µs before `offset` (xfade_p30 golden, frames 44/50);
    on a 1/R clock every pts is an exact frame number."""
    e = empty_edl(Canvas(w=320, h=180, fps=tb.fps_float(Fraction(30000, 1001))))
    v1 = e.get_track("v1")
    for i in range(3):
        c = Clip(src="/nonexistent.mp4", start=i * 1.0, id=f"x{i}")
        c.in_, c.out = 0.0, 1.0
        v1.clips.append(c)
    v1.transitions = [Transition(at=1.0, duration=0.5), Transition(at=2.0, duration=0.5)]
    e.recompute_duration()
    fc = _fc_for(e)
    assert "settb=AVTB" not in fc
    assert fc.count("settb=1001/30000") == 4


def test_segments_need_a_source_at_the_project_rate(tmp_path):
    """A 25 fps source in a 29.97 project restarts the fps phase at every
    segment boundary (the preview showed frames the export never does), so
    it is not segmented; the same clip of a 29.97 source still is."""
    def src(rate: str, name: str) -> str:
        p = tmp_path / name
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color=s=32x18:r={rate}:d=14", "-c:v", "libx264", "-preset",
                        "ultrafast", str(p)], check=True, capture_output=True)
        return str(p)
    fps = tb.fps_float(Fraction(30000, 1001))
    for path, expect in ((src("25", "a.mp4"), False), (src("30000/1001", "b.mp4"), True)):
        c = Clip(src=path, start=0.0)
        c.in_, c.out = 0.0, tb.time_of(390, fps)
        spans = segments.segment_bounds(c, fps, 6.0)
        assert (spans is not None) is expect, path
    assert segments.source_rate(str(tmp_path / "missing.mp4")) is None
