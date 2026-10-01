"""Measurements for the EB1 slice (tests/benchmark/test_eb1_slice.py; lane F).

Everything here reads the persisted session, the fixture truth
(tests/brain_fixtures.py, lane A) and decoded media — never the app's own
verifier — so the slice judges the edit independently:

  * timeline ↔ source through `agent/timemap` (the one clock rule: a truth
    instant is reference seconds; the dialogue lane `a1` plays the reference
    file at offset 0, so `a1` is the map back to reference seconds);
  * energy at a cut edge from the decoded source audio (10 ms RMS windows);
  * seam clicks from the decoded verify render (first-difference peaks);
  * click-vs-flash alignment from tests/timing_fixtures.av_offsets_ms;
  * the source frame per span from the bar codes (tests/frame_map_golden_lib);
  * the zoom of the picture from the width of a bar-code cell in a decoded
    EXPORT (bar_scales) — the only way to see a scale step the way a viewer does.
"""
from __future__ import annotations

import math
import re
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from video_ai_editor.agent.timemap import media_clips, source_range_to_timeline, source_to_timeline, timeline_to_source
from video_ai_editor.edl.schema import EDL, Clip, Keyframe

TH_PROMPT = "make a 45-second reel"
P2_PROMPT = "tighten this podcast like a premium podcast"
ANALYSE_TIMEOUT_S = 8 * 60
#: A cut edge sits on a trough: at most this loud over 10 ms, or within 3 dB
#: of the quietest 10 ms window ±80 ms around it (spec §4.6.2).
TROUGH_MAX_DBFS = -35.0
TROUGH_TIE_DB = 3.0
#: A seam click: the first-difference peak within ±2 ms of the seam, full scale.
SEAM_CLICK_MAX = 0.1


# --------------------------------------------------------------------------
# the app, through the bench
# --------------------------------------------------------------------------

def whisper_cpp_missing() -> str | None:
    """Why the upload path's transcriber cannot run here, or None."""
    from video_ai_editor.ingest import transcribe as _t
    if not _t._whisper_cpp_available():
        return "whisper.cpp is not installed (the upload path's transcriber)"
    if not _t._whisper_cpp_model_path("small").exists():
        return "the whisper.cpp small ggml is not on this machine (the slice never downloads)"
    return None


def transcribe(env: Any, sid: str) -> Path:
    """The app's own `transcribe` tool over the v1 source (whisper.cpp small,
    the backend an upload uses on this Mac); the transcript lands in the
    upload's ingest.json, where the analysis reads it."""
    import json
    env.dispatch(sid, "transcribe", {"model": "small"})
    p = Path(env.ingest_json(sid))
    tx = json.loads(p.read_text(encoding="utf-8")).get("transcript")
    assert isinstance(tx, dict) and (tx.get("words") or tx.get("segments")), f"no transcript in {p}"
    return p


def analyse(env: Any, sid: str, *, timeout_s: float = ANALYSE_TIMEOUT_S) -> str:
    """`POST …/brain/analyse` → poll the job → the graph id."""
    r = env.client.post(f"/api/sessions/{sid}/brain/analyse", json={"force": True},
                        headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        job = env.client.get(f"/api/jobs/{job_id}").json()
        if job["status"] == "completed":
            return str(job["result"]["graph_id"])
        assert job["status"] in ("queued", "running"), job
        time.sleep(1.0)
    raise AssertionError(f"analysis job {job_id} did not finish in {timeout_s:.0f} s")


def versions(env: Any, sid: str) -> list[dict[str, Any]]:
    r = env.client.get(f"/api/sessions/{sid}/brain/versions", headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200, r.text
    return list(r.json()["versions"])


def restore(env: Any, sid: str, vid: str) -> dict[str, Any]:
    r = env.client.post(f"/api/sessions/{sid}/brain/versions/{vid}/restore",
                        headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200, r.text
    return r.json()


def preview_of(run: Any) -> dict[str, Any] | None:
    """The card the run showed before Apply (the first `clarify` frame's preview)."""
    for e in run.clarifies:
        if isinstance(e.get("preview"), dict):
            return e["preview"]
    return None


def decisions_id_of(op: dict[str, Any]) -> str | None:
    args = op.get("args") if isinstance(op, dict) else None
    did = (args or {}).get("decisions")
    return str(did) if isinstance(did, str) else None


# --------------------------------------------------------------------------
# the timeline
# --------------------------------------------------------------------------

def v1(edl: EDL) -> list[Clip]:
    return media_clips(edl, "v1")


def seams(edl: EDL, track: str = "v1") -> list[tuple[Clip, Clip]]:
    clips = media_clips(edl, track)
    return [(a, b) for a, b in zip(clips, clips[1:])]


def frame_s(edl: EDL) -> float:
    from video_ai_editor.edl.timebase import fps_float
    return 1.0 / (fps_float(edl.canvas.fps) or 30.0)


def on_timeline(edl: EDL, src: str, t0: float, t1: float, *, track: str = "v1") -> list[tuple[float, float]]:
    return source_range_to_timeline(edl, track, t0, t1, src=src)


def ref_to_timeline(edl: EDL, ref_src: str, t: float) -> float | None:
    """A reference second → its timeline instant, through the dialogue lane
    (a1 plays the reference at offset 0) when it exists, else the main lane."""
    lane = "a1" if edl.get_track("a1") is not None else "v1"
    return source_to_timeline(edl, lane, t, src=ref_src)


def timeline_to_ref(edl: EDL, ref_src: str, t: float) -> float | None:
    lane = "a1" if edl.get_track("a1") is not None else "v1"
    hit = timeline_to_source(edl, lane, t)
    if hit is None or str(hit[0].src) != ref_src:
        return None
    return float(hit[1])


def first_kept_sentence_ok(edl: EDL, src: str, sent_t0: float, sent_t1: float, *, tol: float = 1.0) -> bool:
    """The first v1 clip opens inside the quotable sentence (within `tol`
    of its start) and plays it whole."""
    clips = v1(edl)
    if not clips or str(clips[0].src) != src:
        return False
    c = clips[0]
    return abs(float(c.in_) - sent_t0) <= tol and float(c.out) >= sent_t1 - 0.05


def source_seconds_removed(edl: EDL, src: str, spans: Iterable[tuple[float, float]], *,
                           track: str = "v1") -> list[tuple[float, float]]:
    """Which of the source `spans` (shrunk 20 ms each side) still play on `track`."""
    return [(a, b) for a, b in spans if on_timeline(edl, src, a + 0.02, b - 0.02, track=track)]


def scale_keys(c: Clip) -> Keyframe | None:
    s = getattr(c.transform, "scale", None) if c.transform is not None else None
    return s if isinstance(s, Keyframe) else None


def caption_cues(edl: EDL) -> list[tuple[float, float]]:
    """The caption cues on the timeline, merged (the app lays them on a
    `captions` lane; a `text` lane's clips with role `caption` count too)."""
    cues = sorted((float(c.start), float(c.end)) for t in edl.tracks if t.type in ("captions", "text")
                  for c in t.clips if getattr(c, "role", None) == "caption")
    out: list[tuple[float, float]] = []
    for a, b in cues:
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def caption_coverage(edl: EDL, src: str, speech: Iterable[tuple[float, float]], *, track: str = "v1") -> float:
    """Of the SPEECH that plays (the truth's voiced spans, in `src`'s
    seconds, mapped onto the timeline through `track`), the share under a caption cue."""
    cues = caption_cues(edl)
    spoken = covered = 0.0
    for s0, s1 in speech:
        for a, b in on_timeline(edl, src, s0, s1, track=track):
            spoken += b - a
            covered += sum(max(0.0, min(b, c1) - max(a, c0)) for c0, c1 in cues)
    return covered / spoken if spoken else 0.0


def punch_push(keys: Keyframe) -> tuple[float, float, float, float] | None:
    """(t0, v0, t1, v1) clip-local of a punch-in's PUSH: the LAST rising pair of keys. A piece that opens on a hidden
    seam carries flat hide keys (1.08, 1.08) before the push, so the first key above 1.0 is not the punch — the push
    is where the scale rises (v1 > v0 by more than a rounding), and it starts at t0 at the scale v0 it rises FROM."""
    rows = sorted((float(k[0]), float(k[1])) for k in keys.keyframes)
    rising = [i for i in range(len(rows) - 1) if rows[i + 1][1] > rows[i][1] + 0.001]
    if not rising:
        return None
    i = rising[-1]
    return rows[i][0], rows[i][1], rows[i + 1][0], rows[i + 1][1]


def punch_in_point(keys: Keyframe) -> tuple[float, float] | None:
    """(clip-local in-point, held scale) of a punch-in: where the push STARTS and the scale it reaches."""
    push = punch_push(keys)
    return None if push is None else (push[0], push[3])


def a1_matches_v1(edl: EDL, dialogue_src: str, offsets: dict[str, float], *, tol: float) -> list[str]:
    """Spec §6.1 by hand, on the wave's offset convention (an event at
    reference second r is at file second r + offsets[file]): under every v1
    piece whose file is an angle or the dialogue file, the a1 clip plays the
    dialogue file at the SAME reference second the piece shows — `piece.in_
    − offsets[piece] + offsets[dialogue]` — starting where the piece starts
    (later, on the frame grid, where the dialogue file has not begun yet),
    within `tol`; and every angle piece is muted."""
    a1 = media_clips(edl, "a1")
    f = frame_s(edl)
    problems: list[str] = []
    for p in v1(edl):
        src = str(p.src)
        if src != dialogue_src and src not in offsets:
            continue
        want_in = float(p.in_) - offsets.get(src, 0.0) + offsets.get(dialogue_src, 0.0)
        want_start = float(p.start)
        if want_in < 0.0:                                   # the camera rolled before the recorder
            want_start = math.ceil((want_start - want_in) / f - 1e-6) * f
            want_in += want_start - float(p.start)
        mate = next((c for c in a1 if abs(float(c.start) - want_start) <= tol and str(c.src) == dialogue_src), None)
        if mate is None:
            problems.append(f"no a1 clip at {want_start:.3f}")
        elif abs(float(mate.in_) - want_in) > tol:
            problems.append(f"a1 at {p.start:.3f} plays {float(mate.in_):.3f}, expected {want_in:.3f}")
        if src in offsets and src != dialogue_src and not (p.audio and p.audio.mute):
            problems.append(f"angle piece at {p.start:.3f} is not muted")
    return problems


# --------------------------------------------------------------------------
# decoded media
# --------------------------------------------------------------------------

def rms_db(samples: np.ndarray, rate: int, t: float, win: float = 0.010) -> float:
    n = max(1, int(win * rate))
    i = int(round(t * rate))
    seg = samples[max(0, i - n // 2): max(0, i - n // 2) + n]
    if seg.size == 0:
        return -120.0
    r = float(np.sqrt(np.mean(np.square(seg.astype(np.float64)))))
    return 20 * math.log10(max(r, 1e-6))


def trough_violations(samples: np.ndarray, rate: int, instants: Iterable[float]) -> list[tuple[float, float, float]]:
    """(t, level, local_min) for every source instant that is neither quiet
    nor within TROUGH_TIE_DB of the quietest 10 ms window ±80 ms."""
    out = []
    for t in instants:
        level = rms_db(samples, rate, t)
        local = min(rms_db(samples, rate, t + k * 0.010) for k in range(-8, 9))
        if level > TROUGH_MAX_DBFS and level > local + TROUGH_TIE_DB:
            out.append((t, level, local))
    return out


def seam_click_peaks(samples: np.ndarray, rate: int, instants: Iterable[float], *, window_s: float = 0.002) -> list[tuple[float, float]]:
    """(t, peak) of the largest first difference within ±window of each seam."""
    diff = np.abs(np.diff(samples.astype(np.float64)))
    n = int(window_s * rate)
    out = []
    for t in instants:
        i = int(round(t * rate))
        seg = diff[max(0, i - n): i + n]
        out.append((t, float(seg.max()) if seg.size else 0.0))
    return out


def angle_share(edl: EDL, ref_src: str, spans: Iterable[tuple[float, float, str]], sid_of: dict[str, str],
                step: float = 0.1) -> float:
    """Over reference `spans` (t0, t1, expected angle name), the fraction of
    sampled instants whose v1 picture is the expected angle."""
    hits = total = 0
    for t0, t1, want in spans:
        t = t0
        while t < t1:
            tl = ref_to_timeline(edl, ref_src, t)
            if tl is not None:
                shown = timeline_to_source(edl, "v1", tl)
                if shown is not None:
                    total += 1
                    hits += sid_of.get(str(shown[0].src)) == want
            t += step
    return hits / total if total else 0.0


def angle_switches(edl: EDL, ref_src: str) -> list[tuple[float, str, str]]:
    """(reference second of the seam, from-src, to-src) for every v1 seam that changes source."""
    out = []
    for a, b in seams(edl):
        if str(a.src) == str(b.src):
            continue
        t = timeline_to_ref(edl, ref_src, float(b.start) + 1e-3)       # the INCOMING piece's reference second
        if t is not None:
            out.append((t - 1e-3, str(a.src), str(b.src)))
    return out


def heard_onsets(edl: EDL, ref_src: str, truth: Any) -> list[float]:
    """Per non-backchannel truth turn: the reference second of its first
    VOICED sound that is still on the timeline (its utterances in order; a
    filler or false start the edit removed is skipped)."""
    out = []
    for t in truth.turns:
        if t.id in truth.backchannels:
            continue
        for u in sorted((u for u in truth.utts if u.turn == t.id and u.voiced_start is not None), key=lambda u: u.start):
            if ref_to_timeline(edl, ref_src, float(u.voiced_start) + 0.02) is not None:
                out.append(float(u.voiced_start))
                break
    return sorted(out)


def tighten_seams(edl: EDL, ref_src: str, min_gap_s: float = 0.4) -> list[tuple[Clip, Clip, float]]:
    """v1 seams that removed ≥ `min_gap_s` of the reference, with the gap."""
    out = []
    for a, b in seams(edl):
        ra = timeline_to_ref(edl, ref_src, float(a.start) + float(a.effective_duration) - 1e-3)
        rb = timeline_to_ref(edl, ref_src, float(b.start) + 1e-3)
        if ra is None or rb is None:
            continue
        gap = rb - ra
        if gap >= min_gap_s:
            out.append((a, b, gap))
    return out


#: A hidden jump cut changes the picture's zoom by at least this much (EX-04/UX-11: 1.0 <-> 1.08 is 0.08;
#: 1.10 -> 1.08 is not a step).
HIDE_MIN_STEP = 0.05


def kf_value(k: Keyframe, t: float) -> float:
    """A keyframe's value at clip-local `t`: held before the first key and after the last, `step` holds the
    previous key, anything else is linear (endpoints are all the seam checks read)."""
    rows = sorted((float(a), float(b)) for a, b in k.keyframes)
    if t <= rows[0][0]:
        return rows[0][1]
    for (t0, v0), (t1, v1) in zip(rows, rows[1:]):
        if t < t1:
            return v0 if k.interp == "step" else v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    return rows[-1][1]


def scale_at(c: Clip, local_t: float) -> float:
    s = getattr(c.transform, "scale", 1.0) if c.transform is not None else 1.0
    if isinstance(s, Keyframe):
        return kf_value(s, local_t)
    return float(s if s is not None else 1.0)


def hidden_by(a: Clip, b: Clip) -> str | None:
    """How a tighten seam is hidden: an angle change, or the incoming piece
    OPENING at a zoom that differs from where the outgoing piece ENDED by at
    least HIDE_MIN_STEP (a key at clip-local 0 that repeats the outgoing scale
    hides nothing)."""
    if str(a.src) != str(b.src):
        return "angle"
    if abs(scale_at(b, 0.0) - scale_at(a, float(a.effective_duration))) >= HIDE_MIN_STEP:
        return "scale_step"
    return None


def punch_keys_mismatch(c: Clip, decision: dict[str, Any], *, tol_s: float = 0.05) -> list[str]:
    """The scale keys on the punched clip against the EDP decision that placed them: the SAME interpolation
    mode, and every key the decision names present at (its source second - the clip's `in`) with the same value."""
    keys = scale_keys(c)
    if keys is None:
        return ["no scale keyframes on the clip"]
    out = []
    want_interp = decision["params"].get("interp")
    if keys.interp != want_interp:
        out.append(f"interp {keys.interp!r}, the decision says {want_interp!r}")
    have = [(float(t), float(v)) for t, v in keys.keyframes]
    for k in decision["params"].get("keys", []):
        t, v = float(k["t"]) - float(c.in_), float(k["values"]["scale"])
        if not any(abs(t - ht) <= tol_s and abs(v - hv) <= 0.005 for ht, hv in have):
            out.append(f"the decision's key (clip {t:.3f} s, scale {v}) is not on the clip: {have}")
    return out


def read_code_zoomed(frame: np.ndarray, zoom: float) -> tuple[int, int]:
    """(code, mask) of one 320x180 gray frame whose picture is zoomed by `zoom` about the centre: band b's lit cell
    sits at 160 + (20 b + 10 - 160) * zoom, its rows are TOP_ROWS mapped the same way; a cell the zoom pushed out of
    the frame is masked (unknown), not read as 0."""
    from frame_map_golden_lib import BAND_W, BANDS, H, TOP_ROWS, W
    y0 = int(max(0, round(H / 2 + (TOP_ROWS[0] - H / 2) * zoom)))
    y1 = int(min(H, round(H / 2 + (TOP_ROWS[1] - H / 2) * zoom)))
    code = mask = 0
    for b in range(BANDS):
        x0 = W / 2 + (b * BAND_W + 5 - W / 2) * zoom + 1
        x1 = W / 2 + (b * BAND_W + BAND_W - 5 - W / 2) * zoom - 1
        if x0 < 1 or x1 > W - 1 or x1 - x0 < 2:
            continue
        mask |= 1 << b
        if float(frame[y0:y1, int(x0):int(x1)].mean()) > 125.5:
            code |= 1 << b
    return code, mask


def barcode_mismatches(render: Path, edl: EDL, sid_of: dict[str, int], samples_per_span: int = 3) -> list[str]:
    """Decode the render's bar codes: at `samples_per_span` instants inside
    every v1 piece the code must name the piece's SOURCE (its sid) and the
    source FRAME the EDL says plays there (`in_ + (t - start)`, +-1 frame for
    the encoder's rounding) - the picture is the angle's, at the right time.
    A zoomed piece (a scale step, a punch-in) is read at its own zoom, cells
    the zoom pushed out of frame masked, so the bar codes keep proving the
    source when the picture is no longer the source's own 1:1 (EX-01)."""
    from frame_map_golden_lib import FRAME_BITS, decode_gray
    from video_ai_editor.edl.timebase import fps_float
    frames = decode_gray(render)
    fps = fps_float(edl.canvas.fps) or 30.0
    fmask = (1 << FRAME_BITS) - 1
    bad = []
    for c in v1(edl):
        want = sid_of.get(str(c.src))
        if want is None:
            continue
        dur = float(c.effective_duration)
        for k in range(samples_per_span):
            i = int((float(c.start) + dur * (k + 0.5) / samples_per_span) * fps)
            if i >= len(frames):
                continue
            want_frame = int(round((float(c.in_) + (i / fps - float(c.start))) * fps)) & fmask
            zooms = (scale_at(c, i / fps - float(c.start)), 1.0)
            ok = False
            for z in dict.fromkeys(zooms):
                code, mask = read_code_zoomed(frames[i], z)
                ok = any(not ((code ^ ((want << FRAME_BITS) | ((want_frame + d) & fmask))) & mask) for d in (0, -1, 1))
                if ok:
                    break
            if not ok:
                code, _ = read_code_zoomed(frames[i], zooms[0])
                bad.append(f"{i / fps:.2f}s shows sid {code >> FRAME_BITS} frame {code & fmask}, expected sid {want} frame {want_frame} "
                           f"({Path(str(c.src)).name}, zoom {zooms[0]:.2f})")
    return bad


# --------------------------------------------------------------------------
# the zoom of the picture, read from a decoded EXPORT (EX-01, EX-04, UX-11)
# --------------------------------------------------------------------------

#: Fixture bar code (tests/frame_map_golden_lib): on a 320x180 source a lit cell is 10 units wide, centred in a
#: 20-unit band; rows 24-84 carry the code. The zoom is about the frame centre, so the source row 54 sits at
#: `h/2 + (54-90)*k` output pixels (k = output pixels per source row) and a cell is `10*k*zoom` pixels wide.
BAR_ROW = 54
BAR_CELL = 10.0
#: An unzoomed piece must read 1.0 within this — else the geometry assumed here is wrong and the reading is void.
ZOOM_CALIBRATION_TOL = 0.03
EXPORT_HEIGHT = 360
#: The click/flash reading is good to a few samples (48 kHz: 0.02 ms each); "within half a frame" allows this much noise.
CLICK_TOL_MS = 0.1
#: The renderer places an audio clip with `adelay` in WHOLE milliseconds (render/audio_mix.py `delay_ms = round(rs*1000)`),
#: so where a piece starts on a frame boundary that is not a whole number of milliseconds (30 fps: 33.333 ms; 29.97: 33.367)
#: the click lands up to 0.5 ms from where the picture says (measured on the 30 fps podcast render: 0.333 ms steps, -16.625 /
#: -16.958 / -16.292 ms for one and the same half-frame). It is the renderer's rounding, not an edit's timing (the
#: half-frame bound itself is unchanged; render behaviour is frozen this wave).
CLICK_TOL_ADELAY_MS = 0.5


def click_tolerance_ms(fps: float) -> float:
    """The noise the click/flash reading may carry at a project rate: a few samples, plus adelay's whole-ms rounding
    when the frame is not a whole number of milliseconds."""
    frame_ms = 1000.0 / fps
    return CLICK_TOL_MS if abs(frame_ms - round(frame_ms)) < 1e-6 else CLICK_TOL_ADELAY_MS
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}


def zoom_of_row(row: np.ndarray, k: float) -> float | None:
    """The zoom one decoded bar-code row (0..1 gray, width w) shows: the median SUB-PIXEL width of its whole lit
    cells over `BAR_CELL * k`. None when no whole cell is in view (a flash frame, a run of zero bits)."""
    w = row.size
    lit = row > 0.5
    edge = np.diff(lit.astype(np.int8))
    ups, downs = np.where(edge == 1)[0] + 1, np.where(edge == -1)[0] + 1
    widths = []
    for u in ups:
        later = downs[downs > u]
        if later.size and u > 3 and int(later[0]) < w - 3 and 0.6 * BAR_CELL * k <= later[0] - u <= 1.4 * BAR_CELL * k:
            widths.append(float(row[max(0, u - 3): int(later[0]) + 3].sum()))
    return float(statistics.median(widths)) / (BAR_CELL * k) if widths else None


def bar_scales(video: Path) -> tuple[list[float | None], float]:
    """(the zoom of every frame of `video`, its fps): one 8-row strip per frame through ffmpeg, so a 1080x1920
    export costs 11 MB, not 2.8 GB."""
    from video_ai_editor import platformutil as _pu
    from video_ai_editor.ingest.probe import probe
    pr = probe(Path(video))
    w, h, fps = int(pr.video.width), int(pr.video.height), float(pr.fps)
    k = h / 180.0
    y0 = int(round(h / 2 + (BAR_ROW - 90) * k)) - 4
    raw = subprocess.run([_pu.FFMPEG, "-v", "error", "-i", str(video), "-map", "0:v:0", "-vf", f"crop={w}:8:0:{y0},format=gray",
                          "-fps_mode", "passthrough", "-f", "rawvideo", "-"], check=True, capture_output=True,
                         **_pu.SUBPROCESS_FLAGS).stdout
    strips = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 8, w).astype(np.float32).mean(axis=1) / 255.0
    return [zoom_of_row(r, k) for r in strips], fps


def zoom_calibration(scales: list[float | None], edl: EDL, fps: float) -> float:
    """The median zoom over the frames of every v1 piece that is unzoomed in the EDL (must read ~1.0)."""
    vals = []
    for c in v1(edl):
        if scale_keys(c) is None and abs(scale_at(c, 0.0) - 1.0) < 1e-6:
            a, b = int(round(float(c.start) * fps)) + 2, int(round((float(c.start) + float(c.effective_duration)) * fps)) - 2
            vals += [x for x in scales[a:b] if x is not None]
    return float(statistics.median(vals)) if vals else float("nan")


def seam_zoom(scales: list[float | None], fps: float, a: Clip, b: Clip, *, span: int = 4) -> tuple[float | None, float | None]:
    """(zoom just before, zoom just after) the seam a|b: the median of the readable frames in the `span` frames
    of each piece next to it."""
    i = int(round(float(b.start) * fps))
    lo = max(int(round(float(a.start) * fps)), i - span)
    hi = min(int(round((float(b.start) + float(b.effective_duration)) * fps)), i + span)
    before = [x for x in scales[lo:i] if x is not None]
    after = [x for x in scales[i:hi] if x is not None]
    return (statistics.median(before) if before else None, statistics.median(after) if after else None)


def unseen_seams(scales: list[float | None], fps: float, seams_: Iterable[tuple[Clip, Clip, float]]) -> list[tuple[float, float, Any, Any]]:
    """(timeline s, removed s, zoom before, zoom after) for every seam whose picture is the SAME angle and whose
    zoom does not change by HIDE_MIN_STEP in the decoded export. An angle change is proven by the bar codes'
    source ids (barcode_mismatches), not here."""
    out = []
    for a, b, gap in seams_:
        if str(a.src) != str(b.src):
            continue
        before, after = seam_zoom(scales, fps, a, b)
        if before is None or after is None or abs(after - before) < HIDE_MIN_STEP:
            out.append((round(float(b.start), 3), round(gap, 2), before, after))
    return out


def export(env: Any, sid: str, *, height: int = EXPORT_HEIGHT, timeout_s: float = 600.0) -> Path:
    """`POST …/export?wait=0` at `height` (the short side) → poll the job → the file."""
    r = env.client.post(f"/api/sessions/{sid}/export?wait=0", json={"height": height}, headers=SAME_ORIGIN)
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        job = env.client.get(f"/api/jobs/{job_id}").json()
        if job["status"] == "completed":
            return Path(job["result"]["path"])
        assert job["status"] in ("queued", "running"), job
        time.sleep(0.5)
    raise AssertionError(f"export job {job_id} did not finish in {timeout_s:.0f} s")


# --------------------------------------------------------------------------
# captions (UX-03, UX-09)
# --------------------------------------------------------------------------

FILLER_WORDS = frozenset({"um", "umm", "uhm", "uh", "uhh", "erm"})


def caption_rows(edl: EDL) -> list[tuple[float, float, str]]:
    """(start, end, text) of every caption cue, unmerged, in timeline order."""
    return sorted((float(c.start), float(c.end), str(getattr(c, "text", "")))
                  for t in edl.tracks if t.type in ("captions", "text") for c in t.clips if getattr(c, "role", None) == "caption")


def caption_filler_cues(edl: EDL) -> list[tuple[float, str]]:
    """Cues that put a filler word on screen."""
    return [(round(a, 2), t) for a, _b, t in caption_rows(edl) if FILLER_WORDS & set(re.findall(r"[a-z']+", t.lower()))]


def hard_seams(edl: EDL, track: str = "a1", *, jump_s: float = 0.05) -> list[float]:
    """Timeline instants where `track` CUTS: its source jumps by more than `jump_s` (or there is a hole) between
    two clips. A seam that only continues the same file, or changes the picture's angle over unbroken sound, is not one."""
    out = []
    clips = media_clips(edl, track)
    for a, b in zip(clips, clips[1:]):
        hole = abs(float(b.start) - (float(a.start) + float(a.effective_duration)))
        if abs(float(b.in_) - float(a.out)) > jump_s or hole > jump_s:
            out.append(float(b.start))
    return out


def cues_across(edl: EDL, seams_: Iterable[float], *, margin: float = 0.02) -> list[tuple[float, float, str, float]]:
    """(start, end, text, seam) of every cue that starts before a hard seam and is still up after it."""
    return [(round(a, 2), round(b, 2), t, round(sm, 2)) for a, b, t in caption_rows(edl) for sm in seams_
            if a < sm - margin and b > sm + margin]


def floating_cues(edl: EDL, playing: Iterable[tuple[float, float]], *, near_s: float = 0.35, min_share: float = 0.8) -> list[tuple[float, float, str]]:
    """Cues that are mostly NOT over speech that plays (within `near_s` of it): a cue held over a pause, or left
    up after its words were cut."""
    spans: list[list[float]] = []
    for a, b in sorted((a - near_s, b + near_s) for a, b in playing):
        if spans and a <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    out = []
    for s0, s1, t in caption_rows(edl):
        inside = sum(max(0.0, min(s1, b) - max(s0, a)) for a, b in spans)
        if inside < min_share * (s1 - s0):
            out.append((round(s0, 2), round(s1, 2), t))
    return out


def speech_on_timeline(edl: EDL, ref_src: str, spans: Iterable[tuple[float, float]], *, track: str = "a1") -> list[tuple[float, float]]:
    return [iv for s0, s1 in spans for iv in on_timeline(edl, ref_src, s0, s1, track=track)]


def speech_cover_by(edl: EDL, ref_src: str, utts: Iterable[tuple[float, float, str]], key_of: Any, *, step: float = 0.05
                    ) -> dict[str, tuple[float, float]]:
    """{group: (seconds under a cue, seconds spoken)} over the truth utterances (t0, t1, group) that still play
    (a1 maps reference seconds to the timeline); `key_of(timeline_t, group)` regroups an instant (e.g. by the
    camera showing it)."""
    cues = caption_cues(edl)
    out: dict[str, tuple[float, float]] = {}
    for t0, t1, grp in utts:
        r = t0
        while r < t1:
            tl = ref_to_timeline(edl, ref_src, r)
            if tl is not None:
                key = key_of(tl, grp)
                c, n = out.get(key, (0.0, 0.0))
                out[key] = (c + step * any(a <= tl < b for a, b in cues), n + step)
            r += step
    return out


def played_fraction(edl: EDL, src: str, t0: float, t1: float, *, track: str = "a1") -> float:
    """Which share of reference [t0, t1] of `src` still plays on `track`."""
    return sum(b - a for a, b in on_timeline(edl, src, t0, t1, track=track)) / (t1 - t0)


def turn_shares(edl: EDL, ref_src: str, truth: Any, angle_of: dict[str, str], *, step: float = 0.05
                ) -> list[tuple[str, str, float, float, int, int]]:
    """(turn id, speaker, t0, t1, samples that play, samples on the speaker's close) for every full turn of the
    truth: not a backchannel and not one of the overlap's two turns."""
    skip = set(truth.backchannels) | set(truth.overlap.turns)
    rows = []
    for t in truth.turns:
        if t.id in skip or not t.expected_angle:
            continue
        n = k = 0
        r = float(t.t0)
        while r < float(t.t1):
            tl = ref_to_timeline(edl, ref_src, r)
            shown = timeline_to_source(edl, "v1", tl) if tl is not None else None
            if shown is not None:
                n += 1
                k += angle_of.get(str(shown[0].src)) == t.expected_angle
            r += step
        rows.append((t.id, t.speaker, float(t.t0), float(t.t1), n, k))
    return rows


def hide_status(edp: dict[str, Any], edl: EDL, dialogue_src: str) -> list[tuple[str, str]]:
    """(decision id, "ok" | "no piece" | "no keys") for every jump_cut_hide the EDP decided: is there a v1 piece
    at that seam (within two project frames) and does it carry scale keys? A hide the compiler dropped shows as
    "no piece" or "no keys" (EX-08: at 29.97 both P2 hides were dropped)."""
    offs = (edp["summary"].get("dialogue") or {}).get("offsets") or {}
    tol = 2 * frame_s(edl)
    out = []
    for d in edp["decisions"]:
        if d["kind"] != "jump_cut_hide":
            continue
        t = ref_to_timeline(edl, dialogue_src, float(d["params"]["piece"][0]) - float(offs.get(d["ref"]["src"], 0.0)))
        piece = next((c for c in v1(edl) if t is not None and abs(float(c.start) - t) <= tol), None)
        out.append((d["id"], "no piece" if piece is None else ("ok" if scale_keys(piece) is not None else "no keys")))
    return out


# --------------------------------------------------------------------------
# a second run, a saved project, a cancelled read, the flag off
# --------------------------------------------------------------------------

def rerun(env: Any, sid: str, prompt: str, *, apply: str) -> dict[str, Any]:
    """POST `prompt` again over a session that already holds an edit and answer only what a plan needs to go on
    (the footage gate, the card with `apply`); ANY OTHER question is left unanswered — it IS the outcome."""
    from .harness import PromptRun
    run = PromptRun(sid=sid, prompt=prompt)
    events = env._post_stream(f"/api/sessions/{sid}/prompt", {"message": prompt, "brain": env.brain or "recipes"}, run)
    asked: list[str] = []
    card: dict[str, Any] | None = None
    for _ in range(4):
        cl = next((e for e in events if e.get("type") == "clarify"), None)
        if cl is None:
            break
        keys = [q["key"] for q in cl.get("questions", [])]
        if isinstance(cl.get("preview"), dict) and cl["preview"].get("lines"):
            card = cl["preview"]
        if "apply" in keys:
            reply = {"apply": apply}
        elif keys and all(k in ("go", "gate_analysis") for k in keys):
            reply = {q["key"]: env.answer_for(q, allow_downloads=False, answers=None) for q in cl["questions"]}
        else:
            asked = [str(q.get("question")) for q in cl.get("questions", [])]
            break
        events = env._post_stream(f"/api/sessions/{sid}/prompt/answer", {"token": cl["token"], "answers": reply}, run)
    return {"run": run, "card": card, "asked": asked, "text": " ".join(str(e.get("text", "")) for e in run.events if e.get("type") == "text_delta")}


def save_and_reopen(env: Any, sid: str) -> str:
    """`POST …/save_project` → the .vae → `POST /api/load_project` → the reopened session's id."""
    r = env.client.post(f"/api/sessions/{sid}/save_project", headers=SAME_ORIGIN)
    assert r.status_code == 200, r.text
    vae = Path(r.json()["path"])
    with vae.open("rb") as fh:
        r2 = env.client.post("/api/load_project", files={"file": (vae.name, fh, "application/zip")}, headers=SAME_ORIGIN)
    assert r2.status_code == 200, r2.text
    return str(r2.json()["id"])


def pieces(edl: EDL, track: str = "v1") -> list[tuple[float, float, float]]:
    """(start, in, out) per piece — a timeline compared across sessions that hold copies of the files."""
    return [(round(float(c.start), 3), round(float(c.in_), 3), round(float(c.out), 3)) for c in media_clips(edl, track)]


#: What a plan would say, to a person, before Apply — the routing of a phrase and the first sentence of its reply.
def phrase_signature(events: list[dict[str, Any]]) -> tuple[str, str, str, str]:
    plan = next((e["plan"] for e in reversed(events) if e.get("type") == "plan"), None) or {}
    asks = ",".join(q.get("key", "") for e in events if e.get("type") == "clarify" for q in e.get("questions", []))
    text = " ".join(str(e.get("text", "")) for e in events if e.get("type") == "text_delta")
    lead = re.sub(r"\d+", "#", text.replace("via Recipes — ", "", 1).replace("\n", " "))[:40]
    return (str(plan.get("intent")), ",".join(s["tool"] for s in plan.get("steps", [])), asks, lead)


_REEL = ("remove_silences,remove_fillers,auto_reframe,set_clip_fit,add_caption_track,apply_hook_stack,add_music,"
         "noise_reduce,apply_export_preset,audit_aesthetic")
_CUT = _REEL.replace("remove_fillers,", "remove_fillers,cut_range,")
_POD = "remove_silences,remove_fillers,add_caption_track,apply_hook_stack,add_music,set_loudness_target,audit_aesthetic"
_AUTO, _READ = "Preview — Auto edit: # changes. Nothing ", "I read that as: Auto edit. Preview — Aut"
_ASK1, _ASK2 = "I did not catch that. Which of these did", "I am not sure what you meant. Which of t"
_TIGHT = ("tighten", "remove_silences,remove_fillers", "apply", "Preview — Tighten: # change. Nothing has")
#: The 24 `edit` phrasings of tests/test_k3_prompt_corpus.py EDIT_BLOCK through the REAL /prompt route on a
#: transcribed talking-head session with brain.enabled OFF, as recorded from `git archive HEAD` (0.8.0 plus three
#: CI rounds; no `brain/` package) — intent, plan tools, the questions asked, the reply's first words (digits as #).
FLAG_OFF_080: dict[str, tuple[str, str, str, str]] = {
    "make a 45-second reel": ("auto_edit", _REEL, "apply", _AUTO),
    "make a 45 second reel": ("auto_edit", _CUT, "apply", _AUTO),
    "make me a 30-second reel": ("auto_edit", _REEL, "apply", _AUTO),
    "make this into an engaging 45-second instagram reel": ("auto_edit", _REEL, "apply", _AUTO),
    "turn this into a 30s reel": ("auto_edit", _CUT, "apply", _AUTO),
    "cut a 20 second reel out of this": ("auto_edit", _CUT, "apply", _AUTO),
    "make a 45-second reel for instagram": ("auto_edit", _REEL, "apply", _AUTO),
    "tighten this podcast": _TIGHT, "tighten the podcast": _TIGHT, "tighten this episode": _TIGHT,
    "tighten this interview": _TIGHT, "tighten this podcast like a premium podcast": _TIGHT, "tighten up this conversation": _TIGHT,
    "edit this like a premium podcast": ("auto_edit", _POD, "apply", _READ),
    "edit this podcast like a premium business podcast": ("auto_edit", _POD, "apply", _READ),
    "premium podcast": ("ask", "", "intent", _ASK1),
    "edit this like a premium business podcast, remove the boring parts": ("ask", "", "intent", _ASK2),
    "edit it like a pro podcast editor would": ("auto_edit", _POD, "apply", _READ),
    "edit this interview like a premium podcast": ("auto_edit", _POD, "apply", _READ),
    "edit this footage like a talking head reel": ("auto_edit", _REEL, "apply", _AUTO),
    "cut this like an interview": ("ask", "", "intent", _ASK1),
    "edit this episode like a premium podcast with captions": ("captions", "add_caption_track", "apply", "Preview \u2014 Captions: # changes. Nothing h"),
    "make a 45-second reel and keep the music": ("auto_edit", _REEL, "apply", _AUTO),
    "make a 45-second reel, no captions": ("auto_edit", _REEL.replace("add_caption_track,", ""), "apply", _AUTO),
}


__all__ = [n for n in dir() if not n.startswith("_")]
