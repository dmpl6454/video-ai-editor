"""A read-only view over a Content Graph for the planner passes.

The graph on disk is a header (`<session>/brain/graph/<gid>.json`, lane C's
`schema.Graph`) plus layer files under `WORKDIR/analysis/<src_key>/`,
`scenes.json` and `angles.json`. A golden graph is one file: the envelope
`{"graph": header, "layers": {name: layer}, "scenes": [...], "angles": {...}}`
(tests/gen_brain_goldens.py); a header whose `layers` values are dicts is
accepted too. `load_graph()` reads either; `Graph` answers the questions a
pass asks (words, sentences, turns, the envelope, offsets, the primary
angle) and derives nothing a layer already states.

Clock (spec §2.4): layers are in REFERENCE seconds; a file's own seconds are
`t_ref + sync_offset_s` (`to_file` / `to_ref`), the convention lane B's
tools and lane D's fixtures use (`other_t = ref_t + offset`).
"""
from __future__ import annotations

import base64
import bisect
import json
import re
import statistics
from functools import cached_property
from pathlib import Path
from typing import Any

_WORD = re.compile(r"[\w'’]+", re.UNICODE)
CONJUNCTIONS = frozenset({"and", "but", "so", "or", "because", "which", "then", "also", "plus", "yet"})
PRONOUNS = frozenset({"it", "that", "this", "they", "these", "those", "he", "she", "its", "their", "there"})
IMPERATIVES = frozenset({"buy", "ship", "stop", "listen", "look", "take", "try", "start", "never", "don't", "do",
                         "make", "use", "remember", "think", "get", "put", "go", "keep", "ask", "call", "watch",
                         "build", "sell", "hire", "read", "write", "learn", "avoid", "pick", "choose", "test"})
BACKCHANNEL = frozenset({"mm-hm", "mmhm", "mm", "mhm", "uh-huh", "yeah", "yes", "right", "sure", "okay", "ok",
                         "exactly", "true", "totally", "hmm", "yep", "yup", "wow"})
SUPERLATIVE = re.compile(r"\b(?:most|least|only|first|last|best|worst|than|\w{3,}est)\b", re.I)
CONTRAST = frozenset({"never", "nobody", "secret", "mistake", "wrong", "best", "worst", "only", "stop", "but", "not"})
STOP = frozenset({"the", "a", "an", "of", "to", "in", "on", "at", "is", "was", "are", "were", "and", "or", "it",
                  "that", "this", "for", "with", "we", "you", "i", "he", "she", "they", "be", "as", "by", "so"})
_TERMINAL = (".", "?", "!", "…", "।")
#: Speech has started when the envelope stays at or above this for this long.
ONSET_FLOOR_DB = -45           # measured on fixture P2: −45 dB puts the onset within −27…+36 ms of the truth
ONSET_SUSTAIN_S = 0.05
#: Two closes with their own microphones tell who speaks: a speaker's own mic is louder than the other's while they
#: talk. The evidence counts only when the two speakers' medians are at least this many dB apart.
MIC_SEPARATION_DB = 6.0
MIC_SPEECH_DB = -50.0          # a 10 ms frame is speech when the reference envelope is at least this
MIC_LOOKBACK_S = 1.5           # a turn is looked for this far before its label starts (an overlap the words missed) …
MIC_EARLY_MIN_S = 0.25         # … and moved only when the evidence starts at least this much earlier
MIC_DIP_S = 0.15               # a stretch that does not fit this short does not end the evidence
MIC_BRIDGE_S = 0.5             # nor does a silence this short (a speaker's own pause between two utterances)


def tokens(text: str) -> list[str]:
    return [t.lower() for t in _WORD.findall(text or "")]


def content_tokens(text: str) -> set[str]:
    return {t for t in tokens(text) if t not in STOP and len(t) > 2}


def decode_env(b64: str) -> list[int]:
    """`env_10ms`: base64 of one signed byte (dBFS) per 10 ms frame."""
    if not b64:
        return []
    raw = base64.b64decode(b64)
    return [b - 256 if b > 127 else b for b in raw]


def _r4(v: float) -> float:
    out = round(float(v), 4)
    return 0.0 if out == 0 else out


def angle_group(sources: list[dict], angles: dict[str, Any] | None, reference: str) -> list[dict]:
    """The angle group in angle order, `{angle, src_key, path, sync_offset_s, sees}` each.

    `angles.json` lists the group's members; with no recorder the REFERENCE is camera A itself, which the
    analysis did not list beside camera B (a graph analysed before the fix has `members == [B]`), so the
    primary angle was the guest's camera and no camera plan was made (closer: two cameras, no recorder). A
    reference that is a video angle joins its group at offset 0 — it IS the clock the offsets are against."""
    if angles and angles.get("members"):
        out = [dict(m) for m in angles["members"]]
        ref = next((s for s in sources if _bare(s.get("key", "")) == _bare(reference)), None)
        if ref is not None and ref.get("role", "angle") == "angle" and ref.get("has_video", True) \
                and not any(_bare(m.get("src_key", "")) == _bare(reference) for m in out):
            out.append({"angle": ref.get("angle") or "A", "src_key": ref["key"], "path": ref.get("path"),
                        "sync_offset_s": 0.0, "sees": list((ref.get("angle_guess") or {}).get("sees") or [])})
    else:
        out = []
        for s in sources:
            if s.get("role", "angle") == "angle" and s.get("has_video", True):
                out.append({"angle": s.get("angle") or "A", "src_key": s["key"], "path": s.get("path"),
                            "sync_offset_s": float(s.get("sync_offset_s") or 0.0),
                            "sees": list((s.get("angle_guess") or {}).get("sees") or [])})
    return sorted(out, key=lambda m: str(m.get("angle") or ""))


def _bare(key: Any) -> str:
    return str(key).removeprefix("src_")


class Graph:
    """The planner's view. Every accessor is pure and cached."""

    def __init__(self, data: dict[str, Any]):
        if "graph" in data and isinstance(data["graph"], dict):
            self.header: dict[str, Any] = data["graph"]
            self.layers: dict[str, Any] = dict(data.get("layers") or {})
            self.scenes: list[dict] = list(data.get("scenes") or [])
            self.angles: dict[str, Any] | None = data.get("angles")
        else:
            self.header = data
            self.layers = {k: v for k, v in (data.get("layers") or {}).items() if isinstance(v, dict)}
            scenes = data.get("scenes")
            self.scenes = list(scenes) if isinstance(scenes, list) else []
            self.angles = data.get("angles") if isinstance(data.get("angles"), dict) else None
        self.raw = data
        self.project_fps: float | None = None       # the PROJECT's rate when the caller knows it (see `fps`)

    # --- header -----------------------------------------------------------

    @property
    def id(self) -> str:
        return str(self.header.get("id", ""))

    @property
    def digest(self) -> str:
        d = self.header.get("digest")
        if isinstance(d, str) and d:
            return d
        ds = self.header.get("digests") or {}
        return "sha256:" + ",".join(f"{k}={ds[k]}" for k in sorted(ds))[:56] if ds else ""

    @property
    def sources(self) -> list[dict]:
        return list(self.header.get("sources") or [])

    @property
    def reference(self) -> str:
        return str(self.header.get("reference") or (self.sources[0]["key"] if self.sources else ""))

    @property
    def fps(self) -> float:
        """The rate every removal, key and lead is put on: the PROJECT's
        (`project_fps`, set when the plan runs — `load_graph` reads the
        session's canvas), else what the analysis recorded (the primary
        file's own rate). A removal put on the FILE's grid lands between the
        project's output frames when the two differ (EB1 EX-08)."""
        if self.project_fps:
            return float(self.project_fps)
        p = self.header.get("project") or {}
        return _fps_float(p.get("fps")) or 30.0

    @property
    def canvas(self) -> tuple[int, int]:
        p = self.header.get("project") or {}
        c = p.get("canvas") or [1920, 1080]
        return int(c[0]), int(c[1])

    @property
    def content_type_guess(self) -> tuple[str, float]:
        ct = self.header.get("content_type") or {}
        return str(ct.get("guess") or "talking_head"), float(ct.get("confidence") or 0.0)

    @property
    def music_mood(self) -> str | None:
        mh = self.header.get("music_hint") or {}
        return mh.get("mood")

    @property
    def speakers(self) -> list[dict]:
        return list(self.header.get("speakers") or [])

    def speaker_angle(self, spk: str | None) -> str | None:
        for s in self.speakers:
            if s.get("id") == spk:
                hint = s.get("angle_hint")
                return hint.get("angle") if isinstance(hint, dict) else hint
        return None

    # --- sources, angles, clocks ----------------------------------------

    @staticmethod
    def bare(key: str) -> str:
        return str(key).removeprefix("src_")

    def source(self, key: str) -> dict | None:
        b = self.bare(key)
        return next((s for s in self.sources if self.bare(s.get("key", "")) == b), None)

    @cached_property
    def members(self) -> list[dict]:
        """The angle group in angle order: `{angle, src_key, path, sync_offset_s, sees}`."""
        return angle_group(self.sources, self.angles, self.reference)

    @cached_property
    def primary(self) -> str:
        """The source on v1 when the plan runs: the angle that carries the dialogue (the one whose own file the
        transcript was read from — with no recorder the reference camera), else the first video angle. Not
        `members[0]` by position: a group listed without its reference put the OTHER camera first (closer N-01)."""
        for key in (self.angles.get("dialogue") if self.angles else None, self.reference):
            hit = next((m for m in self.members if key and self.bare(m["src_key"]) == self.bare(key)), None)
            if hit is not None:
                return str(hit["src_key"])
        if self.members:
            return str(self.members[0]["src_key"])
        return self.reference

    @cached_property
    def dialogue_key(self) -> str:
        """The dialogue source (spec §4.6.1): the recorder when one exists, else the primary angle's own file."""
        if self.angles and self.angles.get("dialogue"):
            return str(self.angles["dialogue"])
        rec = next((s for s in self.sources if s.get("role") == "reference_audio"), None)
        return str(rec["key"]) if rec else self.primary

    def offset(self, key: str) -> float:
        for m in self.members:
            if self.bare(m["src_key"]) == self.bare(key):
                return float(m.get("sync_offset_s") or 0.0)
        s = self.source(key)
        return float((s or {}).get("sync_offset_s") or 0.0)

    def path_of(self, key: str) -> str | None:
        s = self.source(key)
        if s and s.get("path"):
            return str(s["path"])
        for m in self.members:
            if self.bare(m["src_key"]) == self.bare(key) and m.get("path"):
                return str(m["path"])
        return None

    def leaf_of(self, key: str) -> str:
        s = self.source(key) or {}
        return str(s.get("leaf") or Path(str(s.get("path") or key)).name)

    def duration_of(self, key: str) -> float:
        s = self.source(key) or {}
        d = s.get("duration")
        return float(d) if d else self.ref_end + 1.0

    def to_file(self, t_ref: float, key: str) -> float:
        return _r4(t_ref + self.offset(key))

    def to_ref(self, t_file: float, key: str) -> float:
        return _r4(t_file - self.offset(key))

    def angle_of_key(self, key: str) -> str | None:
        return next((str(m["angle"]) for m in self.members if self.bare(m["src_key"]) == self.bare(key)), None)

    def key_of_angle(self, angle: str) -> str | None:
        return next((str(m["src_key"]) for m in self.members if str(m.get("angle")) == angle), None)

    # --- speech -----------------------------------------------------------

    @property
    def speech(self) -> dict:
        return self.layers.get("speech") or {}

    @cached_property
    def words(self) -> list[dict]:
        return sorted((dict(w) for w in self.speech.get("words") or []), key=lambda w: (w["t0"], w["t1"]))

    @cached_property
    def _word_starts(self) -> list[float]:
        return [float(w["t0"]) for w in self.words]

    @cached_property
    def _longest_word_s(self) -> float:
        return max((float(w["t1"]) - float(w["t0"]) for w in self.words), default=0.0)

    def word_straddling(self, t: float, tol: float = 0.02) -> dict | None:
        """The word a cut at reference second `t` would split — `t` is more than `tol` inside it — or None
        (`brain/checks.DEFAULT_MID_WORD_TOL_S` is the stick the safety net measures with)."""
        starts = self._word_starts
        i = bisect.bisect_left(starts, t - tol)
        while i > 0 and starts[i - 1] > t - self._longest_word_s - tol:     # no earlier word can reach `t`
            i -= 1
            w = self.words[i]
            if float(w["t0"]) + tol < t < float(w["t1"]) - tol:
                return w
        return None

    @cached_property
    def sentences(self) -> list[dict]:
        return sorted((dict(s) for s in self.speech.get("sentences") or []), key=lambda s: (s["t0"], s["t1"]))

    @cached_property
    def turns(self) -> list[dict]:
        turns = self.speech.get("turns") or (self.layers.get("speakers") or {}).get("turns") or []
        return sorted((dict(t) for t in turns), key=lambda t: (t["t0"], t["t1"]))

    @property
    def flags(self) -> dict:
        return self.speech.get("flags") or {}

    @property
    def acoustic_fillers(self) -> list[dict]:
        return list(self.speech.get("acoustic_fillers") or [])

    @cached_property
    def _sent_by_id(self) -> dict[str, dict]:
        return {s["id"]: s for s in self.sentences}

    def sentence(self, sid: str) -> dict | None:
        return self._sent_by_id.get(sid)

    @cached_property
    def _words_by_sent(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for w in self.words:
            out.setdefault(str(w.get("sent")), []).append(w)
        return out

    def words_of(self, sid: str) -> list[dict]:
        return self._words_by_sent.get(sid, [])

    def spoken(self, s: dict) -> str:
        """The sentence as the viewer will hear it: its words without the fillers the edit removes ("Um, when
        the card is full" is quoted as "when the card is full"); the sentence's own text when it has no words."""
        words = [str(w["text"]) for w in self.words_of(str(s.get("id"))) if not w.get("filler")]
        return " ".join(words) if words else str(s.get("text", ""))

    def turn_of(self, sid: str) -> dict | None:
        return next((t for t in self.turns if sid in (t.get("sents") or [])), None)

    def turn_at(self, t: float) -> dict | None:
        return next((tu for tu in self.turns if tu["t0"] - 1e-6 <= t <= tu["t1"] + 1e-6), None)

    def speaker_before(self, t: float) -> str | None:
        """Who held the floor just before `t`: the turn over `t`, else the
        last one that ended before it (the turns are cut from the SOUND, so
        they hold where a word was timed onto the wrong side of a pause)."""
        over = self.turn_at(t)
        if over is not None:
            return over.get("spk")
        done = [tu for tu in self.turns if tu["t1"] <= t + 1e-6]
        return max(done, key=lambda tu: tu["t1"]).get("spk") if done else None

    def speaker_after(self, t: float) -> str | None:
        over = self.turn_at(t)
        if over is not None:
            return over.get("spk")
        ahead = [tu for tu in self.turns if tu["t0"] >= t - 1e-6]
        return min(ahead, key=lambda tu: tu["t0"]).get("spk") if ahead else None

    def scores(self, sid: str) -> dict:
        return dict(((self.layers.get("semantic") or {}).get("scores") or {}).get(sid) or {})

    @cached_property
    def ref_end(self) -> float:
        src = self.source(self.reference) or {}
        if src.get("duration"):
            return float(src["duration"])
        return max((w["t1"] for w in self.words), default=0.0) + 1.0

    # --- audio ------------------------------------------------------------

    @property
    def audio(self) -> dict:
        return self.layers.get("audio") or {}

    @cached_property
    def env(self) -> list[int]:
        return decode_env(str(self.audio.get("env_10ms") or ""))

    @property
    def hz(self) -> int:
        return int(self.audio.get("hz") or 100)

    @property
    def silences(self) -> list[dict]:
        return list(self.audio.get("silences") or [])

    @property
    def speech_lufs(self) -> float:
        v = self.audio.get("loudness_i")
        return float(v) if v is not None else -19.0

    def env_mean(self, t0: float, t1: float) -> float | None:
        f0, f1 = int(round(t0 * self.hz)), int(round(t1 * self.hz))
        seg = self.env[max(0, f0):max(f0 + 1, f1)]
        return statistics.fmean(seg) if seg else None

    # --- derived per sentence (cached, pure) ------------------------------

    @cached_property
    def _speaker_stats(self) -> dict[str, tuple[float, float, float]]:
        """spk → (mean level dB, std dB, median wpm) over its sentences."""
        levels: dict[str, list[float]] = {}
        wpms: dict[str, list[float]] = {}
        for s in self.sentences:
            spk = str(s.get("spk"))
            m = self.env_mean(s["t0"], s["t1"])
            if m is not None:
                levels.setdefault(spk, []).append(m)
            wpms.setdefault(spk, []).append(self.wpm(s))
        out = {}
        for spk in set(levels) | set(wpms):
            lv = levels.get(spk) or [0.0]
            mean = statistics.fmean(lv)
            std = max(2.0, statistics.pstdev(lv)) if len(lv) > 1 else 2.0
            out[spk] = (mean, std, statistics.median(wpms.get(spk) or [150.0]))
        return out

    def wpm(self, s: dict) -> float:
        f = s.get("features") or {}
        if f.get("wpm"):
            return float(f["wpm"])
        n = len(self.words_of(s["id"])) or len(tokens(s.get("text", "")))
        return 60.0 * n / max(0.1, s["t1"] - s["t0"])

    def rms_z(self, s: dict) -> float:
        """The sentence's level in speaker-relative σ (0 when the envelope is absent)."""
        m = self.env_mean(s["t0"], s["t1"])
        if m is None:
            return 0.0
        mean, std, _ = self._speaker_stats.get(str(s.get("spk")), (m, 2.0, 150.0))
        return round((m - mean) / std, 3)

    def stretch(self, s: dict) -> float:
        _, _, med = self._speaker_stats.get(str(s.get("spk")), (0.0, 2.0, 150.0))
        return round(med / max(1.0, self.wpm(s)), 3)

    def first_token(self, s: dict) -> str:
        toks = tokens(s.get("text", ""))
        return toks[0] if toks else ""

    def n_words(self, s: dict) -> int:
        f = s.get("features") or {}
        return int(f.get("len_words") or len(tokens(s.get("text", ""))))

    def feature(self, s: dict, name: str, default: Any = False) -> Any:
        return (s.get("features") or {}).get(name, default)

    def is_backchannel(self, turn: dict) -> bool:
        toks = [t for sid in turn.get("sents") or [] for t in tokens((self.sentence(sid) or {}).get("text", ""))]
        return bool(toks) and all(t in BACKCHANNEL for t in toks)

    @cached_property
    def voiced_runs(self) -> list[tuple[float, float]]:
        return [(float(v[0]), float(v[1])) for v in self.audio.get("vad") or []]

    def voiced_run_at(self, t: float) -> tuple[float, float] | None:
        return next(((a, b) for a, b in self.voiced_runs if a - 1e-6 <= t <= b + 1e-6), None)

    def onset(self, t: float, *, within: float = 0.12) -> float:
        """The instant speech really starts at or just after `t` (a word or
        turn start from the layers): the first 10 ms frame within `within`
        that is followed by sustained sound. A one-frame transient ahead of
        the voice — a clap, a slate click, a lip smack — is what the voicing
        detector latches onto; a camera change timed from it leads the
        speaker by that much more. With no envelope, `t`."""
        env, hz = self.env, self.hz
        f0 = int(round(t * hz))
        need = max(3, int(round(ONSET_SUSTAIN_S * hz)))
        for f in range(max(0, f0), min(len(env) - need, f0 + int(round(within * hz)) + 1)):
            if all(v >= ONSET_FLOOR_DB for v in env[f:f + need]):
                return _r4(f / hz)
        return _r4(t)

    # --- who speaks, from the closes' own microphones ----------------------

    @cached_property
    def _mic_pair(self) -> tuple[str, str, list[float], list[float]] | None:
        """(spk_a, spk_b, own_mic_a, own_mic_b) when there are exactly two speakers, each with a close whose own-mic
        energy is in the audio layer; else None."""
        mics = self.audio.get("own_mic_energy") or {}
        ids = [str(sp.get("id")) for sp in self.speakers]
        keys = [self.key_of_angle(self.speaker_angle(i) or "") for i in ids]
        if len(ids) != 2 or None in keys or keys[0] == keys[1]:
            return None
        a, b = mics.get(keys[0]) or mics.get(self.bare(keys[0])), mics.get(keys[1]) or mics.get(self.bare(keys[1]))
        return (ids[0], ids[1], list(a), list(b)) if a and b else None

    def _own_minus_other(self, spk: str) -> tuple[list[float], list[float]] | None:
        """(own mic − the other close's, own mic) per 10 ms frame for `spk`."""
        pair = self._mic_pair
        if pair is None or spk not in pair[:2]:
            return None
        own, other = (pair[2], pair[3]) if spk == pair[0] else (pair[3], pair[2])
        return [o - x for o, x in zip(own, other)], own

    def mic_diff(self, spk: str, t0: float, t1: float) -> float | None:
        """The median, over the speech frames of `[t0, t1]`, of `spk`'s own mic minus the other close's; None without the evidence."""
        both = self._own_minus_other(spk)
        if both is None:
            return None
        d = both[0]
        f0, f1 = max(0, int(t0 * self.hz)), min(len(d), len(self.env), int(t1 * self.hz))
        vals = [d[f] for f in range(f0, f1) if self.env[f] >= MIC_SPEECH_DB]
        return statistics.median(vals) if len(vals) >= 5 else None

    @cached_property
    def mic_signature(self) -> dict[str, float] | None:
        """Per speaker, the median of `mic_diff` over their turns of a second or more; None unless both are
        measured and the mics really separate the two (`MIC_SEPARATION_DB`)."""
        pair = self._mic_pair
        if pair is None:
            return None
        sig: dict[str, float] = {}
        for spk in pair[:2]:
            vals = [v for t in self.turns if str(t.get("spk")) == spk and t["t1"] - t["t0"] >= 1.0
                    for v in [self.mic_diff(spk, t["t0"], t["t1"])] if v is not None]
            if len(vals) < 3:
                return None
            sig[spk] = statistics.median(vals)
        return sig if sig[pair[0]] + sig[pair[1]] >= MIC_SEPARATION_DB else None

    def turn_spk(self, turn: dict) -> str | None:
        """Who a turn's sound says spoke it: its label, unless the closes' own microphones put the OTHER speaker
        on it (the words of a turn are labelled by sentence; the diarised utterance of "number." at the end of the
        guest's line was called the host's — the microphones say −6 dB, the guest's level)."""
        spk, sig, pair = turn.get("spk"), self.mic_signature, self._mic_pair
        if sig is None or pair is None or spk not in sig or turn["t1"] - turn["t0"] < 0.3:
            return spk
        d = self.mic_diff(str(spk), turn["t0"], turn["t1"])
        other = pair[1] if spk == pair[0] else pair[0]
        if d is None:
            return spk
        return other if abs(d + sig[other]) < abs(d - sig[str(spk)]) else spk

    def speaker_onset(self, turn: dict) -> float:
        """The first sound of a turn: `onset()` of its start — or, when the microphones hear the speaker over the
        previous turn's tail (an overlap the words missed, so the label starts late), where that begins. Walks back from
        the label through frames where the speaker's own mic is louder than the other's (a dip of `MIC_DIP_S`, a
        silence of `MIC_BRIDGE_S` do not end it) and moves the onset only when that is `MIC_EARLY_MIN_S` earlier."""
        t0 = float(turn["t0"])
        base = self.onset(t0)
        both, sig, pair = self._own_minus_other(str(turn.get("spk"))), self.mic_signature, self._mic_pair
        if both is None or sig is None or pair is None:
            return base
        d, own = both
        spk = str(turn.get("spk"))
        mid = 0.5 * (sig[spk] - sig[pair[1] if spk == pair[0] else pair[0]])       # halfway from "the other alone" to "me alone"
        f_end, f_lo = int(round(t0 * self.hz)), max(0, int(round((t0 - MIC_LOOKBACK_S) * self.hz)))
        start, bad, quiet = None, 0, 0
        for f in range(min(f_end, len(d)) - 1, f_lo - 1, -1):
            if self.env[f] < MIC_SPEECH_DB or own[f] < MIC_SPEECH_DB:
                quiet, bad = quiet + 1, 0
                if quiet > MIC_BRIDGE_S * self.hz:
                    break
            elif d[f] >= mid - 1.5:
                start, bad, quiet = f, 0, 0
            else:
                bad, quiet = bad + 1, 0
                if bad > MIC_DIP_S * self.hz:
                    break
        if start is None or t0 - start / self.hz < MIC_EARLY_MIN_S:
            return base
        prev = max((t for t in self.turns if t["t1"] <= t0 + 1e-6 and t.get("spk") != turn.get("spk")), key=lambda t: t["t1"], default=None)
        if prev is None or not (prev["t0"] < start / self.hz < prev["t1"] - 0.05):      # only an overlap over the other's line
            return base
        return _r4(start / self.hz)

    def word_gaps(self, t0: float, t1: float) -> list[tuple[float, float]]:
        """Gaps between consecutive words inside [t0, t1]."""
        ws = [w for w in self.words if w["t0"] >= t0 - 1e-6 and w["t1"] <= t1 + 1e-6]
        return [(a["t1"], b["t0"]) for a, b in zip(ws, ws[1:]) if b["t0"] > a["t1"]]


def _fps_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v)
    if "/" in s:
        a, b = s.split("/", 1)
        try:
            return float(a) / float(b)
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return float(s)
    except ValueError:
        return None


def load_graph(where: str | Path, gid: str | None = None) -> Graph:
    """A golden envelope file, or a session directory + graph id (lane C's
    store layout: header under brain/graph/, layers under WORKDIR/analysis/)."""
    p = Path(where)
    if p.is_file():
        return Graph(json.loads(p.read_text(encoding="utf-8")))
    if gid is None:
        raise ValueError("load_graph(session_dir) needs the graph id")
    from .. import store as _store
    header = _store.read_json(_store.graph_path(p, gid))
    if not isinstance(header, dict):
        raise FileNotFoundError(f"graph {gid} not found under {p}")
    ref = str(header.get("reference") or "")
    layers: dict[str, Any] = {}
    for name, rel in (header.get("layers") or {}).items():
        if isinstance(rel, dict):
            layers[name] = rel
            continue
        for cand in (_store.analysis_dir(ref) / str(rel), _store.brain_dir(p) / str(rel)):
            data = _store.read_json(cand)
            if isinstance(data, dict):
                layers[name] = data
                break
    scenes = _store.read_json(_store.brain_dir(p) / str(header.get("scenes") or "scenes.json"))
    angles = _store.read_json(_store.brain_dir(p) / "angles.json")
    graph = Graph({"graph": header, "layers": layers, "scenes": scenes if isinstance(scenes, list) else [],
                   "angles": angles if isinstance(angles, dict) else None})
    graph.project_fps = session_project_fps(p)
    return graph


def session_project_fps(session_dir: Path) -> float | None:
    """The session's canvas rate now (read-only: `edl.json` beside the
    brain files), None when it cannot be read — the planner then keeps the
    rate the analysis recorded."""
    try:
        fps = json.loads((Path(session_dir) / "edl.json").read_text(encoding="utf-8"))["canvas"]["fps"]
        return _fps_float(fps)
    except (OSError, ValueError, KeyError, TypeError):
        return None


__all__ = ["Graph", "load_graph", "session_project_fps", "tokens", "content_tokens", "decode_env", "CONJUNCTIONS", "PRONOUNS",
           "IMPERATIVES", "BACKCHANNEL", "SUPERLATIVE", "CONTRAST"]
