"""Where the measured goldens were recorded, and whether this machine is it.

Three goldens hold numbers that came out of a real ffmpeg run on ONE machine
(the owner's Mac: arm64, ffmpeg 8.1.1) and are compared on every CI runner:

* ``tests/goldens/geometry_cases.json`` — sub-pixel marker centroids and
  8-bit gains DECODED FROM AN EXPORT, i.e. after the H.264 encoder the export
  ladder picked. On the recording Mac that is ``h264_videotoolbox`` (the
  hardware encoder); a Linux or Windows runner has none and falls to libx264.
* ``tests/goldens/keyframe_matrix.json`` — the same kind of measurement, but
  rendered under ``keyframe_matrix_lib.software_encoder()`` (libx264, crf 12).
* ``tests/goldens/voice_fx_cases.json`` — float32 samples and spectra of
  ffmpeg's audio filters; the file itself says ``"ffmpeg": "8.1.1"``.

What CI run 36599751632 (three runners) and this Mac measured, per golden:

geometry — THE ENCODER decides, the CPU and the ffmpeg major only nudge.
    Rendering with libx264 on the recording Mac (arm64, 8.1.1) reproduces the
    x86 runners' numbers, not the golden's: ``contain_portrait`` green is
    [370.276, 90.061] here with libx264, [370.276, 89.986] on ubuntu (6.1) and
    Windows (9.0.2), and [371.17, 89.701] in the golden (VideoToolbox). Over
    the 26 live cases libx264 on this Mac is at most 0.894 px and 2 grey
    levels (2/128) from the golden, with every bounding box equal. The CPU
    and the ffmpeg major move libx264's numbers by about 0.1 px
    (``scale_05_pan1_opacity`` green x: 401.905 arm64/8, 402.0 x86/6.1,
    402.036 x86/9.0.2), which is enough to cross a 0.25 px line. macOS 9.0.1
    (a VM: VideoToolbox without the hardware) failed 9 other params by up to
    0.488 px and one grey level.
keyframe matrix — libx264 everywhere, and still 0.306 to 0.366 px against a
    0.3 px line on all three runners (x86 6.1, x86 9.0.2, arm64 9.0.1): the
    ffmpeg major and the CPU cannot be told apart from three foreign points,
    so both are part of the recording environment.
voice fx — THE CPU decides: arm64 + ffmpeg 9.0.1 reproduced every float
    exactly, both x86 runners (6.1 and 9.0.2) differ in the 8th significant
    digit (at most 2e-5 Hz, 1e-6 dB, 1.13e-6 of full scale).

So each golden names the properties its sub-pixel numbers measurably depend
on, and ``foreign_reason`` compares only those. A test module asks once per
assertion class; the integer geometry, frame indices, counts and pure-Python
tables are never scoped.
"""
from __future__ import annotations

import platform
import re
from dataclasses import dataclass, field
from typing import Any

import runner_env

#: The owner's Mac, where tests/gen_*_goldens.py were run (voice_fx_cases.json
#: records "ffmpeg": "8.1.1"; the three goldens were committed together in
#: 30e076b..03e9ba4 and all compare equal on that machine and on no runner).
RECORDED_FFMPEG_MAJOR = 8
RECORDED_MACHINE = "arm64"

_MACHINES = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x86_64", "amd64": "x86_64"}


@dataclass(frozen=True)
class GoldenEnv:
    ffmpeg_major: int | None
    machine: str
    #: the H.264 encoder the measured file went through (None: no video)
    encoder: str | None = None

    def describe(self) -> str:
        major = "an unknown ffmpeg" if self.ffmpeg_major is None else f"ffmpeg {self.ffmpeg_major}"
        return f"{major} {self.machine}" + (f" ({self.encoder})" if self.encoder else "")


@dataclass(frozen=True)
class Golden:
    name: str
    recorded: GoldenEnv
    #: GoldenEnv fields the scoped numbers measurably depend on (see above)
    depends_on: tuple[str, ...]


GEOMETRY = Golden("geometry_cases.json",
                  GoldenEnv(RECORDED_FFMPEG_MAJOR, RECORDED_MACHINE, "h264_videotoolbox"),
                  ("encoder", "ffmpeg_major", "machine"))
KEYFRAME_MATRIX = Golden("keyframe_matrix.json",
                         GoldenEnv(RECORDED_FFMPEG_MAJOR, RECORDED_MACHINE, "libx264"),
                         ("encoder", "ffmpeg_major", "machine"))


def voice_fx(doc: dict[str, Any]) -> Golden:
    """The voice golden, with the ffmpeg version the FILE records."""
    return Golden("voice_fx_cases.json", GoldenEnv(parse_major(doc.get("ffmpeg")) or RECORDED_FFMPEG_MAJOR,
                                                   RECORDED_MACHINE), ("machine",))


# ------------------------------------------------------------------ probes

def parse_major(version: Any) -> int | None:
    """8 from "8.1.1", 6 from "6.1.1-3ubuntu5", 9 from "n9.0.2"; None from a
    git snapshot ("N-118000-g…"), which names no release."""
    m = re.match(r"n?(\d+)\.", str(version or ""))
    return int(m.group(1)) if m else None


def probe_ffmpeg_major() -> int | None:
    """The major of the ffmpeg on PATH: the one reading the tests share
    (`ffmpeg_caps.real_major`, the compositor's own, untouched by a test
    that forces a version)."""
    import ffmpeg_caps
    return ffmpeg_caps.real_major()


def probe_virtual() -> bool:
    return runner_env.is_virtual_mac()


def probe_machine() -> str:
    m = platform.machine().lower()
    return _MACHINES.get(m, m or "unknown")


def probe_encoder() -> str:
    """The encoder an export picks on this machine RIGHT NOW: the ladder's own
    answer (`compositor._video_encoder_args`), so a test that forces libx264
    (`software_encoder()`) is seen as libx264."""
    from video_ai_editor.render import compositor
    args = compositor._video_encoder_args(preview=False)
    return args[args.index("-c:v") + 1]


def current(*, encoder: str | None = None, video: bool = True) -> GoldenEnv:
    """This machine. `encoder` overrides the probe (a render that was made
    under a forced encoder); `video=False` for an audio-only golden."""
    enc = (encoder or probe_encoder()) if video else None
    return GoldenEnv(probe_ffmpeg_major(), probe_machine(), enc)


def foreign_reason(golden: Golden, scoped: str, *, portable: str, env: GoldenEnv | None = None) -> str | None:
    """None on the recording environment; elsewhere the skip reason, naming
    both environments, what is scoped and what was still checked."""
    here = env if env is not None else current(video=golden.recorded.encoder is not None)
    if all(getattr(here, f) == getattr(golden.recorded, f) for f in golden.depends_on):
        return None
    return (f"{golden.name} recorded on {golden.recorded.describe()}; this is {here.describe()}: "
            f"{scoped}; {portable}")


def stale_reason(golden: Golden, *, env: GoldenEnv | None = None, virtual: bool | None = None) -> str | None:
    """The message for a FAILURE, not a skip: this is the recording machine
    (same CPU, same encoder, real hardware) but its ffmpeg is another major,
    so `foreign_reason` skips the golden's own tolerance lines HERE as well
    and nothing anywhere holds them (no CI runner is the recording
    environment). None everywhere else, a CI guest included."""
    if "ffmpeg_major" not in golden.depends_on:
        return None
    here = env if env is not None else current(video=golden.recorded.encoder is not None)
    same_machine = all(getattr(here, f) == getattr(golden.recorded, f)
                       for f in golden.depends_on if f != "ffmpeg_major")
    if not same_machine or here.ffmpeg_major == golden.recorded.ffmpeg_major:
        return None
    if probe_virtual() if virtual is None else virtual:
        return None
    return (f"{golden.name} was recorded on {golden.recorded.describe()} and this machine is "
            f"{here.describe()}: the machine it was recorded on now has another ffmpeg, so its sub-pixel "
            f"lines are skipped here too and run NOWHERE. Compare the skip reasons' measured deviation, then "
            f"re-record the golden on this ffmpeg (tests/gen_*_goldens.py) and move RECORDED_FFMPEG_MAJOR")


# ------------------------------------------------- what holds on every machine

#: Beside the integer geometry, against either golden: a centroid stays inside
#: its own pixel and a gain within 3 grey levels. NOT a wider tolerance for a
#: golden (those stay 0.25 px / 0.005 and 0.3 px / 0.01, on the recording
#: environment): a guard that tells another encoder's rounding from a picture
#: that moved. Measured: geometry, libx264 against the VideoToolbox golden on
#: the recording Mac, all 26 live cases: at most 0.894 px (`contain_portrait`
#: green) and 2 levels (`kf_opacity_speed_half` k=2, 123 vs 125), and the x86
#: runners of CI run 36599751632 failed at the same 0.894 px and 2 levels;
#: keyframe matrix, libx264 on all three runners: 0.352 px (ubuntu, 6.1
#: x86_64), 0.306 px (Windows, 9.0.2 x86_64), 0.366 px (macOS, 9.0.1 arm64),
#: each the first line that failed.
PIXEL_GUARD = 1.0
LEVEL_GUARD = 3.0
SCOPED = "sub-pixel centroids, 8-bit gains and single-pixel marker visibility differ"


@dataclass
class Deviation:
    """How far a render's measurements are from a golden's, in the units the
    measurements are quantised in."""
    px: float = 0.0
    #: 8-bit grey levels (a gain is median grey / 128, so one level is 1/128)
    levels: float = 0.0
    #: markers one side recorded and the other dropped (the "wholly
    #: surrounded by grey" rule is a threshold on single pixels)
    presence: list[tuple] = field(default_factory=list)

    def marker(self, where: tuple, got, want) -> None:
        if (got is None) != (want is None):
            self.presence.append(where)
        elif want is not None:
            self.px = max(self.px, abs(got[0] - want[0]), abs(got[1] - want[1]))

    def gain(self, got: float, want: float, grey: float) -> None:
        self.levels = max(self.levels, abs(got - want) * grey)

    def describe(self) -> str:
        drop = f", {len(self.presence)} marker(s) kept on one side only {self.presence[:3]}" if self.presence else ""
        return f"measured here: at most {self.px:.3f} px and {self.levels:.2f} grey levels from the golden{drop}"
