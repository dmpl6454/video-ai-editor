"""Voice effects (CapCut's voice changer) — ONE preset table, read by the
schema (which ids `AudioProps.voice_effect` may hold), the renderers (the v1
and PiP chains through `compositor._audio_props_filters`, the music / voice-over
/ audio lanes through `audio_mix._audio_clip_filter` — both call
`render/audio_mix.voice_filters`), the editing engine
(`agent/dispatch.set_voice_effect`), the agent tool schema, the Prompt
Editor's validator and grammar, and the Inspector (`GET /api/voice/presets`,
`api/voice_routes.py`). The instant-preview engine (`lib/voice/voiceFx.ts`)
reads `lib/voice/voiceFxTable.json`, which is THIS table dumped by
`tests/gen_voice_fx_goldens.py` and pinned equal to it by
`tests/test_voice_effects.py` — the browser keeps no hand-written copy.

A preset is an ordered list of STAGES. Each stage has one render in ffmpeg
(`stage_filters`) and one in the preview engine (`voiceFx.ts`), and names
the parameters the preset's `intensity` scales, with the value each one takes
at intensity 0 (its NEUTRAL): a parameter at intensity i is
`neutral + (full - neutral) * i`. Intensity 1 (the default, omitted from the
JSON) is the preset as designed; 0 is the dry sound.

  pitch    shift by `semitones`, the duration kept. The render binary has no
           rubberband (tests/test_c5_render_audio.py), so it is varispeed
           (`asetrate` + `aresample`) and a WSOLA stretch (`atempo`) by the
           inverse ratio: pitch exact to ≈1 cent, timing within
           `KEEP_PITCH_MAX_OFFSET_MS` like keep-pitch speed (measured,
           tests/test_voice_effects_render.py). An upward shift stretches
           first and resamples after (the WSOLA jitter is then compressed by
           the ratio: −7.8…+11.9 ms at +9 st against −12.6…+20.0 the other
           way round); a downward shift resamples first (−4.8…+15.8 ms at
           −8 st against −8.5…+27.5).
  biquad   an RBJ low/high-pass or peaking EQ at `f` Hz, Q `q` (peaking: gain
           `gain_db`), blended with the dry signal by `mix` (ffmpeg's own
           `m=`); `passes` repeats it for a steeper slope.
  echo     `aecho`: out = out_gain · (in_gain · x + Σ decay_j · x(t − d_j)),
           feed-forward taps (no feedback — so the preview's taps are exact).
  drive    soft saturation `tanh(k·x) / tanh(k)` (unity at full scale; k → 0
           is the identity, so k scales).
  ring     amplitude modulation `x · (1 − depth + depth · sin(2π f t))`
           (depth 1 is a ring modulator: the robot buzz).
  vibrato  ffmpeg `vibrato` (a 5 ms modulated delay line) at `f` Hz, depth `d`.
  reverb   convolution (`afir`) with a synthesized impulse response: `dry` at
           sample 0 plus a decaying noise tail (`tail` gain, decaying 60 dB
           in `rt60` seconds after `predelay_ms`), decorrelated per channel.
           The IR is generated in the graph (`aevalsrc`) from a closed form
           the preview engine evaluates identically (`reverb_ir`).
  gain     a static level in dB (make-up after a band-limit).

WHERE THE EFFECT SITS (both chains): after the clip's retime (speed, a
curve's intermediate, a reverse's intermediate) and its channel mode, before
its gain, volume automation, fades and mute, and before the cut to the
clip's exact length. So an echo's 250 ms is 250 ms on the timeline whatever
the clip's speed, a chipmunk on a 2x varispeed clip adds its shift to the
varispeed's, a fade fades the effect's tail with the voice, the saturation
stages see the clip's own level (not the fader's), and a pitch stage's few
samples of rounding never change the clip's length.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

#: Intensity bounds (a fraction of the preset as designed).
INTENSITY_RANGE: tuple[float, float] = (0.0, 1.0)
DEFAULT_INTENSITY = 1.0
#: Below this a pitch shift is not rendered at all (a hundredth of a semitone).
PITCH_EPS_SEMITONES = 0.01
#: Length of the Hall impulse response, seconds.
REVERB_IR_SECONDS = 2.0
SR = 48000


@dataclass(frozen=True)
class Stage:
    kind: str
    params: dict[str, Any]
    #: parameter → its value at intensity 0 (see the module docstring).
    scale: dict[str, float] = field(default_factory=dict)

    def at(self, intensity: float) -> dict[str, Any]:
        """This stage's parameters at `intensity`."""
        out = dict(self.params)
        for k, neutral in self.scale.items():
            full = self.params[k]
            if isinstance(full, (list, tuple)):
                out[k] = [neutral + (float(v) - neutral) * intensity for v in full]
            else:
                out[k] = neutral + (float(full) - neutral) * intensity
        return out


@dataclass(frozen=True)
class VoicePreset:
    id: str
    label: str
    hint: str
    #: The Inspector's icon: a name in lib/icons.ts ICONS (a lucide glyph).
    icon: str
    stages: tuple[Stage, ...]
    #: Other words editors (and the Prompt bar) use for it.
    aliases: tuple[str, ...] = ()


def _pitch(st: float) -> Stage:
    return Stage("pitch", {"semitones": st}, {"semitones": 0.0})


def _bq(kind: str, f: float, q: float = 0.707, *, gain_db: float = 0.0, passes: int = 1) -> Stage:
    p: dict[str, Any] = {"type": kind, "f": f, "q": q, "mix": 1.0, "passes": passes}
    scale = {"mix": 0.0}
    if kind == "peaking":
        p["gain_db"] = gain_db
        p["mix"] = 1.0
    return Stage("biquad", p, scale)


def _gain(db: float) -> Stage:
    return Stage("gain", {"db": db}, {"db": 0.0})


#: The CapCut voice changer, in its grid order. Parameters were chosen by
#: measurement against a synthesized voice (tests/test_voice_effects_render.py
#: prints each preset's pitch, spectral centroid and level).
PRESETS: tuple[VoicePreset, ...] = (
    VoicePreset("chipmunk", "Chipmunk", "High and squeaky: up 9 semitones", "voiceChipmunk",
                (_pitch(9.0),), ("helium", "squeaky", "high pitched", "high-pitched", "cartoon")),
    VoicePreset("deep", "Deep", "A lower, fuller voice: down 4 semitones", "voiceDeep",
                (_pitch(-4.0),), ("low", "lower", "baritone", "bass voice", "deeper")),
    VoicePreset("monster", "Monster", "Growling and huge: down 8 semitones with grit", "voiceMonster",
                (_pitch(-8.0), Stage("drive", {"k": 2.5}, {"k": 0.0}), _bq("lowpass", 3500.0),
                 _gain(-1.5)), ("demon", "beast", "villain", "ogre", "evil")),
    VoicePreset("robot", "Robot", "Metallic and buzzing, like a machine", "voiceRobot",
                (Stage("echo", {"in_gain": 1.0, "out_gain": 0.6, "delays_ms": [7.0, 14.0, 21.0],
                                "decays": [0.7, 0.5, 0.35]},
                       {"decays": 0.0, "out_gain": 1.0}),
                 Stage("ring", {"freq": 45.0, "depth": 1.0}, {"depth": 0.0})),
                ("robotic", "android", "cyborg", "machine", "dalek", "droid")),
    VoicePreset("echo", "Echo", "Repeats every quarter second and dies away", "voiceEcho",
                (Stage("echo", {"in_gain": 1.0, "out_gain": 0.85, "delays_ms": [250.0, 500.0, 750.0],
                                "decays": [0.5, 0.25, 0.125]},
                       {"decays": 0.0, "out_gain": 1.0}),),
                ("echoes", "echoey", "delay", "canyon")),
    VoicePreset("reverb", "Hall", "A big hall: two seconds of reverb", "voiceHall",
                (Stage("reverb", {"dry": 0.85, "tail": 0.0125, "rt60": 1.8, "predelay_ms": 25.0},
                       {"tail": 0.0, "dry": 1.0}),),
                ("hall", "reverb", "reverberation", "cathedral", "church", "big room", "concert hall", "roomy")),
    VoicePreset("telephone", "Telephone", "Thin and band-limited, like a phone call", "voicePhone",
                (_bq("highpass", 400.0, passes=2), _bq("lowpass", 3000.0, passes=2), _gain(3.0)),
                ("phone", "phone call", "cell phone", "landline")),
    VoicePreset("megaphone", "Megaphone", "Loud, honky and distorted", "voiceMegaphone",
                (_bq("highpass", 500.0, passes=2), _bq("lowpass", 4000.0),
                 _bq("peaking", 2000.0, 1.0, gain_db=6.0),
                 Stage("drive", {"k": 3.0}, {"k": 0.0}), _gain(-2.0)),
                ("bullhorn", "loudspeaker", "loud hailer", "loudhailer", "pa system")),
    VoicePreset("radio", "Radio", "An old AM radio broadcast", "voiceRadio",
                (_bq("highpass", 300.0), _bq("lowpass", 3500.0, passes=2),
                 _bq("peaking", 1200.0, 0.8, gain_db=4.0),
                 Stage("drive", {"k": 1.5}, {"k": 0.0}), _gain(1.0)),
                ("am radio", "walkie talkie", "walkie-talkie", "old radio", "vintage radio", "broadcast")),
    VoicePreset("underwater", "Underwater", "Muffled and wobbling, as if under water", "voiceUnderwater",
                (_bq("lowpass", 450.0, passes=2), Stage("vibrato", {"f": 2.0, "d": 0.6}, {"d": 0.0}),
                 _gain(4.0)),
                ("under water", "muffled", "submerged", "drowning", "muffle")),
    VoicePreset("vibrato", "Vibrato", "A wavering, singing pitch", "voiceVibrato",
                (Stage("vibrato", {"f": 6.0, "d": 0.5}, {"d": 0.0}),),
                ("wobble", "warble", "wavering", "shaky voice", "trembling", "tremble")),
)
PRESET_BY_ID: dict[str, VoicePreset] = {p.id: p for p in PRESETS}
PRESET_IDS: tuple[str, ...] = tuple(p.id for p in PRESETS)
_ALIASES: dict[str, str] = {}
for _p in PRESETS:
    for _a in (_p.id, _p.label, *_p.aliases):
        _ALIASES.setdefault(" ".join(_a.lower().replace("-", " ").replace("_", " ").split()), _p.id)

#: Stage kinds each renderer implements (a new kind must land in both).
STAGE_KINDS = ("pitch", "biquad", "echo", "drive", "ring", "vibrato", "reverb", "gain")
assert all(s.kind in STAGE_KINDS for p in PRESETS for s in p.stages)


def preset_id(name: Any) -> str | None:
    """The canonical preset id for a user/agent spelling ("Hall" and
    "reverb" → reverb, "CHIPMUNK", "walkie-talkie" → radio), or None."""
    if not isinstance(name, str):
        return None
    key = " ".join(name.strip().lower().replace("-", " ").replace("_", " ").split())
    return _ALIASES.get(key)


def check_intensity(v: Any) -> float:
    """An intensity as stored: finite, clamped into INTENSITY_RANGE (the
    model's rule: out-of-range clamps, non-finite raises)."""
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ValueError(f"voice effect intensity must be a number, got {v!r}")
    f = float(v)
    if not math.isfinite(f):
        raise ValueError(f"voice effect intensity must be finite, got {v!r}")
    return min(INTENSITY_RANGE[1], max(INTENSITY_RANGE[0], f))


def describe(effect: Any, intensity: float = DEFAULT_INTENSITY) -> str:
    """Editor words: "Robot", "Hall at 40 %", "no voice effect"."""
    p = PRESET_BY_ID.get(effect) if isinstance(effect, str) else None
    if p is None:
        return "no voice effect"
    if abs(float(intensity) - 1.0) < 1e-9:
        return p.label
    return f"{p.label} at {round(float(intensity) * 100)} %"


def stages_at(effect: str | None, intensity: float) -> list[tuple[str, dict[str, Any]]]:
    """`[(kind, params)]` of `effect` at `intensity`, the stages that change
    anything (none for no effect or intensity 0)."""
    p = PRESET_BY_ID.get(effect) if isinstance(effect, str) else None
    if p is None or intensity <= 0.0:
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    for s in p.stages:
        v = s.at(float(intensity))
        if s.kind == "pitch" and abs(v["semitones"]) < PITCH_EPS_SEMITONES:
            continue
        if s.kind == "drive" and v["k"] < 1e-3:
            continue
        if s.kind == "gain" and abs(v["db"]) < 1e-3:
            continue
        out.append((s.kind, v))
    return out


# ------------------------------------------------------------------ the reverb IR
#
# The same closed form in `aevalsrc` and in voiceFx.ts (`reverbIr`): sample n
# of channel c is
#
#   dry · [n = 0] + tail · [n ≥ P] · exp(−a · (n − P)) · (2 · frac(X) − 1),
#   X = sin((n + 7919 c) · 12.9898) · 43758.5453,   a = ln(1000) / (rt60 · SR)
#
# — a hash-noise tail (no RNG state, so both sides agree sample for sample to
# the last few bits of `sin`) that falls 60 dB in rt60 seconds.

def reverb_constants(params: dict[str, Any]) -> dict[str, float]:
    return {
        "dry": float(params["dry"]),
        "tail": float(params["tail"]),
        "a": math.log(1000.0) / (float(params["rt60"]) * SR),
        "P": float(round(float(params["predelay_ms"]) * SR / 1000.0)),
        "n": float(int(round(REVERB_IR_SECONDS * SR))),
    }


def reverb_ir(params: dict[str, Any], channel: int) -> list[float]:
    """The impulse response `aevalsrc` synthesizes (Python, for tests)."""
    k = reverb_constants(params)
    out = []
    for n in range(int(k["n"])):
        x = math.sin((n + 7919 * channel) * 12.9898) * 43758.5453
        v = k["tail"] * (1.0 if n >= k["P"] else 0.0) * math.exp(-k["a"] * (n - k["P"])) * (2 * (x - math.floor(x)) - 1)
        if n == 0:
            v += k["dry"]
        out.append(v)
    return out


def payload() -> dict:
    """`GET /api/voice/presets` and `lib/voice/voiceFxTable.json`: everything
    the Inspector and the preview engine need, from here."""
    return {
        "presets": [{
            "id": p.id, "label": p.label, "hint": p.hint, "icon": p.icon, "aliases": list(p.aliases),
            "stages": [{"kind": s.kind, "params": s.params, "scale": s.scale} for s in p.stages],
        } for p in PRESETS],
        "intensity_range": list(INTENSITY_RANGE),
        "default_intensity": DEFAULT_INTENSITY,
        "pitch_eps_semitones": PITCH_EPS_SEMITONES,
        "reverb_ir_seconds": REVERB_IR_SECONDS,
        "sample_rate": SR,
    }


__all__ = [
    "INTENSITY_RANGE", "DEFAULT_INTENSITY", "PITCH_EPS_SEMITONES", "REVERB_IR_SECONDS", "Stage",
    "VoicePreset", "PRESETS", "PRESET_BY_ID", "PRESET_IDS", "STAGE_KINDS", "preset_id", "check_intensity",
    "describe", "stages_at", "reverb_constants", "reverb_ir", "payload",
]
