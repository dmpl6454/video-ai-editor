"""Voice effects (wave E, F3 — CapCut's voice changer), measured on REAL renders.

Every claim below is measured on the decoded sound of a render: the
preview's sound graph (`compositor._audio_only_graph`, the same per-clip
chains the export uses), the one-pass export (`render_export`, an mp4 decoded
back) and the chunked preview (`render_preview`, one chunk per clip).

* pitch presets (Chipmunk, Deep, Monster): the pitch ratio by autocorrelation
  within 5 cents of 2^(st/12); the clip's length EXACT to the sample; every
  transient of a click track within `KEEP_PITCH_MAX_OFFSET_MS` of where the
  dry clip has it (the render binary has no rubberband: asetrate + atempo);
* Echo: the repeats found by cross-correlation at exactly 250/500/750 ms,
  at the table's gains;
* Hall: an impulse comes out as the impulse response the table describes
  (dry gain at sample 0, the tail falling 60 dB in rt60 by Schroeder
  integration) — the IR the preview engine convolves with;
* the band-limit presets by band energies of white noise; Robot by its
  ring-modulation sidebands; Vibrato / Underwater by the instantaneous
  frequency of a sine;
* where it sits: with varispeed and keep-pitch speed, a fade, a channel mode,
  on v1, an overlay (PiP) and the audio lanes, the tail cut at the clip's end;
* intensity scales the preset (Chipmunk at 50 % is +4.5 st);
* a clip with NO effect renders byte-identical filter text (no new salt);
* .vae save/open keeps the effect; an unknown id is refused on load.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from video_ai_editor import platformutil as _pu  # noqa: E402
from video_ai_editor.edl import timebase as tb  # noqa: E402
from video_ai_editor.edl import voice_effects as V  # noqa: E402
from video_ai_editor.edl.schema import EDL, AudioProps, Canvas, Clip, Keyframe, empty_edl  # noqa: E402
from video_ai_editor.render import audio_mix, compositor, render_export, render_preview  # noqa: E402

SR = 48000
FPS = 30
PITCH_CENTS = 5.0


# ------------------------------------------------------------------ sources

def _ff(args: list[str]) -> None:
    subprocess.run([_pu.FFMPEG, "-y", "-v", "error", *args], check=True, capture_output=True)


def _src(path: Path, expr: str, seconds: float, *, right: str | None = None) -> Path:
    """A mov: grey picture at 30 fps + float PCM sound (`expr` left, `right`
    or the same right) — lossless, so every measurement is the filter's."""
    if not path.exists():
        _ff(["-f", "lavfi", "-i", f"color=c=gray:s=64x36:r={FPS}:d={seconds}",
             "-f", "lavfi", "-i", f"aevalsrc=exprs='{expr}|{right or expr}':s={SR}:d={seconds}",
             "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "pcm_f32le", "-shortest", str(path)])
    return path


#: A steady voiced sound: 140 Hz with 1/k harmonics to ~3 kHz.
VOICE = "+".join(f"{0.25 / k:.4f}*sin(2*PI*{140 * k}*t)" for k in range(1, 22))
#: A 4 ms 1.5 kHz burst every 250 ms (the first at 0.1 s).
CLICKS = "0.8*gte(t\\,0.1)*lt(mod(t-0.1\\,0.25)\\,0.004)*sin(2*PI*1500*t)"


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("vfx")
    return {
        "voice": _src(d / "voice.mov", VOICE, 6),
        "clicks": _src(d / "clicks.mov", CLICKS, 6),
        "tone": _src(d / "tone.mov", "0.4*sin(2*PI*1000*t)", 6),
        "tone220": _src(d / "tone220.mov", "0.4*sin(2*PI*220*t)", 6),
        "impulse": _src(d / "impulse.mov", "0.5*eq(n\\,4800)", 4),
        "noise": _src(d / "noise.mov", "0.25*(2*random(0)-1)", 6, right="0.25*(2*random(1)-1)"),
        "left": _src(d / "left.mov", VOICE, 6, right="0"),
        "dir": d,
    }


def _edl(*, lufs=None) -> EDL:
    e = empty_edl(Canvas(w=64, h=36, fps=FPS))
    e.canvas.loudness_lufs = lufs
    return e


def _clip(src: Path, cid: str, start: float, in_: float, out: float, **kw) -> Clip:
    audio = kw.pop("audio", None)
    c = Clip(src=str(src), start=start, id=cid, **kw)
    c.in_, c.out = in_, out
    if audio:
        c.audio = AudioProps(**audio)
    return c


def _render(e: EDL, cache: Path) -> np.ndarray:
    """The preview's sound of `e` (no mastering), (n, 2) float64."""
    from video_ai_editor.render.reverse import with_reversed_sources
    e.recompute_duration()
    sub = with_reversed_sources(audio_mix.apply_solo(e), cache, FPS)
    inputs, fc, label = compositor._audio_only_graph(sub, fps=FPS, first_input=0, apply_loudnorm=False)
    raw = subprocess.run([_pu.FFMPEG, "-v", "error", *inputs, "-filter_complex", fc, "-map", label,
                          "-f", "f32le", "-ac", "2", "-ar", str(SR), "-"], check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype="<f4").reshape(-1, 2).astype(np.float64)


def _decode(p: Path) -> np.ndarray:
    raw = subprocess.run([_pu.FFMPEG, "-v", "error", "-i", str(p), "-map", "0:a:0", "-ac", "2", "-ar", str(SR),
                          "-f", "f32le", "-"], check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype="<f4").reshape(-1, 2).astype(np.float64)


def _one(media, preset: str | None, *, src="voice", seconds=2.0, intensity=None, lane="v1", **audio) -> np.ndarray:
    """A lone clip (0 → `seconds`) with the preset, rendered: (n, 2)."""
    e = _edl()
    a = dict(audio)
    if preset:
        a["voice_effect"] = preset
    if intensity is not None:
        a["voice_intensity"] = intensity
    c = _clip(media[src], "a", 0.0, 0.0, seconds, audio=a)
    if lane == "v1":
        e.get_track("v1").clips.append(c)
    else:
        e.get_track(lane).clips.append(c)
    return _render(e, media["dir"] / "cache")


# ------------------------------------------------------------------ measurements

def _pitch(x: np.ndarray, lo=60.0, hi=900.0) -> float:
    """Fundamental by normalised autocorrelation (parabolic peak), Hz."""
    x = x - x.mean()
    n = len(x)
    f = np.fft.rfft(x, 2 * n)
    ac = np.fft.irfft(f * np.conj(f))[:n]
    ac /= ac[0]
    a, b = int(SR / hi), int(SR / lo)
    k = a + int(np.argmax(ac[a:b]))
    y0, y1, y2 = ac[k - 1], ac[k], ac[k + 1]
    return SR / (k + 0.5 * (y0 - y2) / (y0 - 2 * y1 + y2))


def _cents(f: float, want: float) -> float:
    return 1200 * np.log2(f / want)


def _onsets(x: np.ndarray, thr=0.1) -> list[int]:
    env = np.abs(x)
    out, i = [], 0
    while i < len(env):
        if env[i] > thr:
            out.append(i)
            i += int(0.1 * SR)
        else:
            i += 1
    return out


def _band_db(x: np.ndarray, lo: float, hi: float) -> float:
    s = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    f = np.fft.rfftfreq(len(x), 1 / SR)
    return 10 * np.log10(s[(f >= lo) & (f < hi)].sum() + 1e-30)


def _rms_db(x: np.ndarray) -> float:
    return 10 * np.log10(np.mean(x * x) + 1e-30)


def _inst_freq(x: np.ndarray) -> np.ndarray:
    n = len(x)
    X = np.fft.fft(x)
    h = np.zeros(n)
    h[0] = 1
    h[1:(n + 1) // 2] = 2
    if n % 2 == 0:
        h[n // 2] = 1
    phase = np.unwrap(np.angle(np.fft.ifft(X * h)))
    return np.diff(phase) * SR / (2 * np.pi)


# ------------------------------------------------------------------ the table

def test_every_preset_renders_on_every_lane(media):
    """Each preset on v1, an overlay (PiP, at 50 %), the voice-over and a
    plain audio lane at once: the graph builds, the sound is there, nothing
    is NaN, the programme is as long as v1 says."""
    for pid in V.PRESET_IDS:
        e = _edl()
        e.get_track("v1").clips.append(_clip(media["voice"], "a", 0.0, 0.0, 2.0, audio={"voice_effect": pid}))
        e.get_track("v2").clips.append(_clip(media["voice"], "p", 0.5, 0.0, 1.0,
                                             audio={"voice_effect": pid, "voice_intensity": 0.5}))
        e.get_track("vo").clips.append(_clip(media["voice"], "v", 0.2, 1.0, 2.0, audio={"voice_effect": pid}))
        e.get_track("a1").clips.append(_clip(media["voice"], "l", 1.0, 0.0, 0.8, audio={"voice_effect": pid}))
        x = _render(e, media["dir"] / "cache")
        assert x.shape == (tb.samples_for_frames(60, FPS), 2), (pid, x.shape)
        assert np.isfinite(x).all() and _rms_db(x) > -40, (pid, _rms_db(x))


@pytest.mark.parametrize("pid", ["chipmunk", "deep", "monster"])
def test_pitch_presets_shift_by_their_semitones_and_keep_the_length(media, pid):
    st = next(s.params["semitones"] for s in V.PRESET_BY_ID[pid].stages if s.kind == "pitch")
    want = 140.0 * 2 ** (st / 12)
    x = _one(media, pid, seconds=2.0)
    assert len(x) == tb.samples_for_frames(60, FPS) == 96000       # the clip's exact length
    f = _pitch(x[24000:72000, 0])
    print(pid, f"st {st:+g} want {want:.2f} Hz got {f:.2f} Hz ({_cents(f, want):+.2f} cents)")
    assert abs(_cents(f, want)) < PITCH_CENTS, (pid, f, want)
    # the dry clip is 140 Hz
    assert abs(_cents(_pitch(_one(media, None)[24000:72000, 0]), 140.0)) < 1.0


@pytest.mark.parametrize("pid", ["chipmunk", "deep", "monster"])
def test_pitch_presets_keep_transients_within_the_keep_pitch_bound(media, pid):
    """Click track (a 4 ms burst every 250 ms from 0.1 s): every burst of the
    shifted clip within KEEP_PITCH_MAX_OFFSET_MS of the dry clip's."""
    dry = _onsets(_one(media, None, src="clicks", seconds=3.0)[:, 0])
    wet = _onsets(_one(media, pid, src="clicks", seconds=3.0)[:, 0], thr=0.05)
    assert len(dry) == len(wet) == 12, (len(dry), len(wet))
    offs = [(w - d) / SR * 1000 for w, d in zip(wet, dry)]
    print(pid, "offsets ms", [round(o, 2) for o in offs])
    assert max(abs(o) for o in offs) <= audio_mix.KEEP_PITCH_MAX_OFFSET_MS, offs


def test_intensity_scales_the_shift(media):
    x = _one(media, "chipmunk", intensity=0.5)
    want = 140.0 * 2 ** (4.5 / 12)
    assert abs(_cents(_pitch(x[24000:72000, 0]), want)) < PITCH_CENTS
    # intensity 0 is the dry sound, sample for sample
    assert np.array_equal(_one(media, "chipmunk", intensity=0.0), _one(media, None))


def test_echo_repeats_at_exactly_its_delays_and_gains(media):
    x = _one(media, "echo", src="impulse", seconds=1.5)[:, 0]
    dry = _one(media, None, src="impulse", seconds=1.5)[:, 0]
    at = int(np.argmax(np.abs(dry)))
    # cross-correlation of the output with the dry impulse: the repeats
    xc = np.correlate(x, dry[at - 10:at + 11], mode="valid")
    peaks = sorted(int(i) + 10 for i in np.argsort(-np.abs(xc))[:4])
    assert [p - at for p in peaks] == [0, 12000, 24000, 36000], [p - at for p in peaks]
    st = V.PRESET_BY_ID["echo"].stages[0].params
    got = [x[at + d] / dry[at] for d in (0, 12000, 24000, 36000)]
    want = [st["out_gain"] * st["in_gain"]] + [st["out_gain"] * g for g in st["decays"]]
    print("echo gains", [round(g, 4) for g in got], "want", want)
    assert np.allclose(got, want, atol=1e-4), (got, want)


def test_hall_convolves_with_the_tables_impulse_response(media):
    x = _one(media, "reverb", src="impulse", seconds=3.0)
    dry = _one(media, None, src="impulse", seconds=3.0)[:, 0]
    at = int(np.argmax(np.abs(dry)))
    p = V.PRESET_BY_ID["reverb"].stages[0].params
    for ch in (0, 1):
        ir = np.asarray(V.reverb_ir(p, ch))
        got = x[at:at + len(ir), ch] / dry[at]
        err = float(np.abs(got - ir).max())
        assert err < 2e-5, (ch, err)
    # the tail decays 60 dB in rt60: Schroeder backward integration, T20 ×3
    tail = x[at + 1:at + len(ir), 0]
    edc = 10 * np.log10(np.cumsum(tail[::-1] ** 2)[::-1] / np.sum(tail ** 2))
    t5, t25 = np.argmax(edc < -5), np.argmax(edc < -25)
    rt60 = 3 * (t25 - t5) / SR
    print("hall rt60", round(rt60, 3), "table", p["rt60"])
    assert abs(rt60 - p["rt60"]) < 0.15 * p["rt60"]


@pytest.mark.parametrize("pid,low,mid,high", [
    # band energies of white noise, out − in (dB): <200 Hz, 900-1800 Hz, >6 kHz
    ("telephone", (-60, -18), (0, 6), (-80, -20)),
    ("megaphone", (-60, -18), (-3, 12), (-60, -8)),
    ("radio", (-40, -3), (0, 12), (-60, -15)),
    ("underwater", (0, 8), (-80, -20), (-120, -45)),
])
def test_band_limit_presets_by_band_energy(media, pid, low, mid, high):
    x = _one(media, pid, src="noise", seconds=2.0)[4800:91200, 0]
    d = _one(media, None, src="noise", seconds=2.0)[4800:91200, 0]
    bands = {"low": (20, 200), "mid": (900, 1800), "high": (6000, 20000)}
    got = {k: _band_db(x, *b) - _band_db(d, *b) for k, b in bands.items()}
    print(pid, {k: round(v, 1) for k, v in got.items()})
    for k, (lo, hi) in (("low", low), ("mid", mid), ("high", high)):
        assert lo <= got[k] <= hi, (pid, k, got[k])


def test_robot_ring_modulates(media):
    x = _one(media, "robot", src="tone", seconds=2.0)[4800:91200, 0]
    f = np.fft.rfftfreq(len(x), 1 / SR)
    s = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    fr = V.PRESET_BY_ID["robot"].stages[1].params["freq"]

    def at(hz):
        return 20 * np.log10(s[np.argmin(np.abs(f - hz))] + 1e-12)
    print("robot carrier", round(at(1000), 1), "sidebands", round(at(1000 - fr), 1), round(at(1000 + fr), 1))
    assert at(1000 - fr) - at(1000) > 30 and at(1000 + fr) - at(1000) > 30


@pytest.mark.parametrize("pid,rate,dev", [
    # deviation = f0 · 2π f · A / SR, A = d · (240 − 1) / 2 (ffmpeg vibrato's 5 ms line)
    ("vibrato", 6.0, 1000 * 2 * np.pi * 6.0 * 0.5 * 239 / 2 / SR),
    ("underwater", 2.0, 220 * 2 * np.pi * 2.0 * 0.6 * 239 / 2 / SR),
])
def test_vibrato_wobbles_the_pitch_at_its_rate(media, pid, rate, dev):
    src, f0 = ("tone", 1000.0) if pid == "vibrato" else ("tone220", 220.0)
    x = _one(media, pid, src=src, seconds=3.0)[9600:134400, 0]
    fi = _inst_freq(x)[2000:-2000]
    got_dev = (np.percentile(fi, 99) - np.percentile(fi, 1)) / 2
    spec = np.abs(np.fft.rfft(fi - fi.mean()))
    got_rate = np.fft.rfftfreq(len(fi), 1 / SR)[np.argmax(spec[1:]) + 1]
    print(pid, f"deviation {got_dev:.2f} Hz (want {dev:.2f}) rate {got_rate:.2f} Hz, mean {fi.mean():.2f}")
    assert abs(got_dev - dev) < 0.12 * dev
    assert abs(got_rate - rate) < 0.2
    assert abs(fi.mean() - f0) < 0.5


# ------------------------------------------------------------------ where it sits

@pytest.mark.parametrize("keep_pitch,ratio", [(False, 2.0), (True, 1.0)])
def test_with_speed_the_shift_adds_to_varispeed_and_keep_pitch(media, keep_pitch, ratio):
    """A 2x clip with Chipmunk: varispeed (pitch ×2) then +9 st; keep-pitch
    (pitch kept) then +9 st. The length is the clip's timeline footprint."""
    e = _edl()
    e.get_track("v1").clips.append(_clip(media["voice"], "a", 0.0, 0.0, 4.0, speed=2.0,
                                         audio={"voice_effect": "chipmunk", "keep_pitch": keep_pitch}))
    x = _render(e, media["dir"] / "cache")
    assert len(x) == 96000
    want = 140.0 * ratio * 2 ** (9 / 12)
    f = _pitch(x[24000:72000, 0], hi=1200)
    print("speed 2x keep_pitch", keep_pitch, round(f, 2), "want", round(want, 2))
    assert abs(_cents(f, want)) < PITCH_CENTS


def test_a_fade_out_fades_the_echo_and_the_tail_is_cut_at_the_clips_end(media):
    """Echo, a 0.5 s fade-out, then a silent clip: the fade shapes the
    repeats with the voice (the effect sits before the fade) and nothing of
    the echo sounds after the clip (cut to its exact length, like CapCut)."""
    e = _edl()
    e.get_track("v1").clips += [
        _clip(media["voice"], "a", 0.0, 0.0, 1.0, audio={"voice_effect": "echo", "fade_out": 0.5}),
        _clip(media["voice"], "b", 1.0, 0.0, 1.0, audio={"mute": True}),
    ]
    x = _render(e, media["dir"] / "cache")[:, 0]
    assert len(x) == 96000
    assert np.max(np.abs(x[48000:])) == 0.0
    blocks = [_rms_db(x[i:i + 2400]) for i in range(24000, 48000, 2400)]
    assert all(b2 < b1 + 0.5 for b1, b2 in zip(blocks, blocks[1:])), blocks     # falling
    assert blocks[-1] < blocks[0] - 15, blocks


def test_a_channel_mode_feeds_the_effect(media):
    """A left-only recording with "left to both" and Telephone: both sides
    carry the same processed voice."""
    x = _one(media, "telephone", src="left", channels="left")
    assert _rms_db(x[:, 1]) > -30 and np.allclose(x[:, 0], x[:, 1], atol=1e-6)


def test_overlay_and_audio_lanes_shift_too(media):
    for lane in ("v2", "vo", "a1", "music"):
        e = _edl()
        e.get_track("v1").clips.append(_clip(media["voice"], "base", 0.0, 0.0, 2.0, audio={"mute": True}))
        e.get_track(lane).clips.append(_clip(media["voice"], "x", 0.0, 0.0, 2.0, audio={"voice_effect": "deep"}))
        x = _render(e, media["dir"] / "cache")
        f = _pitch(x[24000:72000, 0])
        assert abs(_cents(f, 140 * 2 ** (-4 / 12))) < PITCH_CENTS, (lane, f)


def test_reverse_and_curve_clips_take_an_effect(media):
    e = _edl()
    e.get_track("v1").clips += [
        _clip(media["voice"], "r", 0.0, 0.0, 1.5, reverse=True, audio={"voice_effect": "chipmunk"}),
        _clip(media["voice"], "c", 1.5, 0.0, 2.0, speed={"curve": [[0.0, 1.0], [1.0, 1.0]]},
              audio={"voice_effect": "echo"}),
    ]
    x = _render(e, media["dir"] / "cache")
    f = _pitch(x[12000:60000, 0])
    assert abs(_cents(f, 140 * 2 ** (9 / 12))) < PITCH_CENTS
    assert len(x) == tb.samples_for_frames(105, FPS)


# ------------------------------------------------------------------ export, chunks

def test_export_and_the_chunked_preview_carry_the_effect(media, tmp_path):
    from video_ai_editor.edl import EDLStore
    sd = tmp_path / "s"
    sd.mkdir()
    e = _edl()
    e.get_track("v1").clips += [
        _clip(media["voice"], "a", 0.0, 0.0, 2.0, audio={"voice_effect": "chipmunk"}),
        _clip(media["voice"], "b", 2.0, 0.0, 2.0),
    ]
    e.recompute_duration()
    (sd / "edl.json").write_text(e.model_dump_json(by_alias=True))
    st = EDLStore(sd)
    for label, res in (("export", render_export(st.edl, sd)), ("preview", render_preview(st.edl, sd))):
        x = _decode(Path(res.path))
        f1, f2 = _pitch(x[24000:72000, 0]), _pitch(x[120000:168000, 0])
        print(label, round(f1, 2), round(f2, 2))
        assert abs(_cents(f1, 140 * 2 ** (9 / 12))) < PITCH_CENTS, (label, f1)
        assert abs(_cents(f2, 140.0)) < PITCH_CENTS, (label, f2)


# ------------------------------------------------------------------ model

def test_no_effect_leaves_every_chain_as_it_was(media):
    """The filter text of a clip without an effect is exactly what it was
    before the field existed (the chain builders emit nothing), and the EDL
    JSON has no new key — RENDER_BEHAVIOR_VERSION needs no bump."""
    c = _clip(media["voice"], "a", 0.0, 0.0, 2.0, audio={"gain_db": -3.0, "fade_in": 0.2})
    assert "voice_" not in c.model_dump_json(by_alias=True)
    txt = compositor._build_clip_audio_chain(c, input_label="[0:a]", label_out="[a0]", fps=FPS)
    assert "aecho" not in txt and "asetrate" not in txt and "afir" not in txt
    assert audio_mix.voice_filters(c.audio, "a0") == ""


def test_model_validates_and_normalises():
    a = AudioProps(voice_effect="Hall", voice_intensity=3)
    assert (a.voice_effect, a.voice_intensity) == ("reverb", 1.0)
    assert AudioProps(voice_effect="walkie-talkie").voice_effect == "radio"
    assert AudioProps(voice_effect="none").voice_effect is None
    # review RE: ONE forward-compatibility policy for the wave's new fields —
    # a name a newer build wrote loads as "none", logged (clip_animations'
    # rule); set_voice_effect still refuses it with a 400 at the tool boundary
    assert AudioProps(voice_effect="banana").voice_effect is None
    with pytest.raises(ValueError):
        AudioProps(voice_intensity=float("nan"))
    a = AudioProps(voice_effect="robot", voice_intensity=0.25)
    assert AudioProps.model_validate_json(a.model_dump_json()) == a


def test_a_vae_round_trip_keeps_the_effect(tmp_path, monkeypatch, media):
    from video_ai_editor import storage as _storage, storage_project as _sp
    from video_ai_editor.edl import EDLStore
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path / "wd")
    monkeypatch.setattr(_sp, "session_dir", lambda sid: tmp_path / "wd" / sid)
    sd = tmp_path / "wd" / "s1"
    sd.mkdir(parents=True)
    e = _edl()
    e.get_track("v1").clips.append(_clip(media["voice"], "a", 0.0, 0.0, 2.0,
                                         audio={"voice_effect": "megaphone", "voice_intensity": 0.4}))
    e.get_track("vo").clips.append(_clip(media["voice"], "v", 0.0, 0.0, 1.0, audio={"voice_effect": "echo"}))
    e.recompute_duration()
    (sd / "edl.json").write_text(e.model_dump_json(by_alias=True))
    dst = tmp_path / "p.vae"
    _sp.save_project("s1", dst)
    new = EDLStore(tmp_path / "wd" / _sp.load_project(dst)).edl
    a, v = new.get_clip("a")[1], new.get_clip("v")[1]
    assert (a.audio.voice_effect, a.audio.voice_intensity) == ("megaphone", 0.4)
    assert (v.audio.voice_effect, v.audio.voice_intensity) == ("echo", 1.0)
    assert EDL.model_validate_json(new.to_json()).to_json() == new.to_json()


def test_an_unknown_effect_in_a_file_loads_as_none():
    """review RE: refusing it made a project a NEWER build saved (with a
    preset this build does not know) unopenable; it loads without it."""
    e = _edl()
    e.get_track("v1").clips.append(Clip(src="x.mp4", out=1.0, id="a"))
    body = e.model_dump_json(by_alias=True).replace('"channels":"stereo"',
                                                     '"channels":"stereo","voice_effect":"banana"', 1)
    assert EDL.model_validate_json(body).get_clip("a")[1].audio.voice_effect is None


@pytest.mark.parametrize("lane", ["v1", "vo"])
def test_volume_automation_keeps_its_clock_after_a_pitch_shift(media, lane):
    """The effect sits BEFORE the volume automation, which reads the stream's
    own clock: a key step at 1.0 s must still land at 1.0 s after a pitch
    stage re-clocked and stretched the sound in front of it."""
    env = Keyframe(keyframes=[(0.0, -60.0), (1.0, -60.0), (1.0 + 1 / 48000, 0.0)], interp="step")
    x = _one(media, "chipmunk", seconds=2.0, lane=lane, gain_env=env)[:, 0]
    if lane != "v1":
        x = x[:96000]
    lvl = np.array([_rms_db(x[i:i + 480]) for i in range(0, 96000 - 480, 480)])     # 10 ms blocks
    rise = int(np.argmax(lvl > lvl.max() - 20)) * 10
    print(lane, "gain step heard at", rise, "ms")
    assert 980 <= rise <= 1020, rise


def test_the_chunk_key_changes_only_with_an_effect(media, tmp_path):
    from video_ai_editor.render.chunks import fingerprint_clip
    kw = dict(canvas_w=64, canvas_h=36, fps=30, encoder_args=["-c:v", "libx264"])
    a = _clip(media["voice"], "a", 0.0, 0.0, 2.0)
    b = a.model_copy(deep=True)
    assert fingerprint_clip(a, **kw) == fingerprint_clip(b, **kw)
    b.audio.voice_effect = "robot"
    assert fingerprint_clip(a, **kw) != fingerprint_clip(b, **kw)
    c = b.model_copy(deep=True)
    c.audio.voice_intensity = 0.5
    assert fingerprint_clip(b, **kw) != fingerprint_clip(c, **kw)


def test_a_long_clip_previewed_in_segments_carries_the_effect(media, tmp_path):
    """A 14 s clip previews as picture segments + ONE sound render of the
    whole clip (render/segments.py): the echo is in that sound, its first
    repeat 250 ms after a click (±1 ms through the preview's AAC)."""
    from video_ai_editor.edl import EDLStore
    from video_ai_editor.render import segments
    src = _src(media["dir"] / "long.mov", "0.8*eq(n\\,340800)+0.1*sin(2*PI*220*t)", 14)
    sd = tmp_path / "s"
    sd.mkdir()
    e = _edl()
    e.get_track("v1").clips.append(_clip(src, "a", 0.0, 0.0, 14.0, audio={"voice_effect": "echo"}))
    e.recompute_duration()
    (sd / "edl.json").write_text(e.model_dump_json(by_alias=True))
    st = EDLStore(sd)
    assert segments.segment_bounds(st.edl.get_clip("a")[1], FPS) is not None     # the segmented path
    x = _decode(Path(render_preview(st.edl, sd).path))[:, 0]
    click = 340800
    hp = x - np.convolve(x, np.ones(64) / 64, mode="same")                          # drop the 220 Hz bed
    a = click - 480 + int(np.argmax(np.abs(hp[click - 480:click + 480])))
    b = a + 12000 - 480 + int(np.argmax(np.abs(hp[a + 12000 - 480:a + 12000 + 480])))
    print("segmented echo repeat after", (b - a) / SR * 1000, "ms")
    assert abs((b - a) - 12000) <= 48, b - a
    assert abs(hp[b]) > 0.3 * abs(hp[a])
