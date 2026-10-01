"""The Content Graph assembler (spec §3.1–3.3): layers per source under
`WORKDIR/analysis/<src_key>/<layer>/<params>.json`, the assembled graph under
`<session>/brain/graph/<gid>.json`, plus `scenes.json` and `angles.json`.

    analyse(session_dir, sources, *, set_progress=None, cancel_event=None,
            layers=None, force=False) -> gid

is synchronous; lane F wraps it in a job. `sources` is the session's media
paths (v1 first, then the audio lanes); a `{path, role?, angle?, transcript?}`
dict is accepted in place of a path. Everything is written through lane C's
`brain.store` (validated against its models, atomic, capped) and the
finished graph becomes the session's CURRENT one (`set_current_graph`).

Roles are inferred when not given, and only from what is measured:
  * the first file with picture is v1 — angle `A`;
  * an audio-only file whose sound cross-correlates with v1 (confidence ≥ 0.5)
    is the RECORDER: `reference_audio`, the reference clock and the dialogue;
  * every other picture file is an angle `B, C…` when it syncs to the
    reference, else `broll`; an audio-only file that does not sync is `other`;
  * with no recorder the reference is v1's own file.
One clock: reference seconds. A member is `member_t = ref_t + sync_offset_s`.
The transcript of record is the reference's own upload transcript; a recorder
has none in a real session (an audio upload), so v1's is read and moved onto
the reference clock (`analysis/transcripts.py`; the speech layer's
`params.transcript_of` says so).

Keys: `src_key = "src_" + sha256(realpath, size, mtime_ns)[:24]` (store),
`gid = "g_" + sha256(sorted layer digests + offsets + roles +
ANALYSIS_VERSION)[:12]`; the semantic layer's digest excludes its run state
(`budget`) so two builds of one project agree byte for byte. The header
carries the ABSOLUTE `path` of each source (the graph lives in the session,
never in a shared cache; the route's summary drops it) and tags the v1
source `v1`.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from . import graph_scenes as GS
from . import schema as S
from . import store
from .analysis import ANALYSIS_VERSION, LAYERS
from .analysis import audio as _audio
from .analysis import pcm as _pcm
from .analysis import semantic as _semantic
from .analysis import speakers as _speakers
from .analysis import speech as _speech
from .analysis import sync as _sync
from .analysis import transcripts as _transcripts

_log = logging.getLogger(__name__)
REFERENCE_ROLES = ("reference_audio", "reference")
NON_ANALYSED_ROLES = ("broll", "music", "other")
MAX_RECORDER_CANDIDATES = 3
ANGLE_LETTERS = "ABCDEFGH"
# progress milestones (fractions of one analyse call)
P_PROBE, P_SYNC, P_AUDIO, P_SPEAKERS, P_SPEECH, P_SEMANTIC = 0.03, 0.15, 0.30, 0.45, 0.65, 0.95


class Cancelled(Exception):
    """Raised at a stage boundary when `cancel_event` is set (mapped to the
    job's `cancelled` status by lane F's route through `api.jobs.JobCancelled`)."""


class GraphId(str):
    """`analyse`'s return value: the graph id, a plain `str` to every caller that only wants the id.
    `pinned` — whether it became the session's current graph; `waiting_for` — `"transcript"` when the
    upload's speech was still being transcribed, so the graph was built WITHOUT its speech layer and was
    not pinned (the caller can ask again once the words are there)."""
    pinned: bool = True
    waiting_for: str | None = None

    def __new__(cls, gid: str, *, pinned: bool = True, waiting_for: str | None = None):
        obj = super().__new__(cls, gid)
        obj.pinned, obj.waiting_for = pinned, waiting_for
        return obj


#: One analysis of a session at a time: a second caller waits its turn (the layers are cached by then, so it is quick).
_SESSION_LOCKS: dict[str, threading.Lock] = {}
_SESSION_LOCKS_GUARD = threading.Lock()
TRANSCRIPT_POLL_S = 0.5


def _session_lock(session_dir: Path) -> threading.Lock:
    with _SESSION_LOCKS_GUARD:
        return _SESSION_LOCKS.setdefault(str(session_dir.resolve()), threading.Lock())


def _cancelled_exc() -> type[Exception]:
    try:
        from ..api.jobs import JobCancelled
        return JobCancelled
    except Exception:
        return Cancelled


# --------------------------------------------------------------------------
# canonical bytes, io helpers (public: tests and lane E read these)
# --------------------------------------------------------------------------

def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest_of(obj) -> str:
    return "sha256:" + hashlib.sha256(canonical(obj)).hexdigest()


def _slug(parts: list) -> str:
    return hashlib.sha256(canonical(parts)).hexdigest()[:8]


def graph_path(session_dir: str | os.PathLike, gid: str) -> Path:
    return store.graph_path(session_dir, gid)


def load_graph(session_dir: str | os.PathLike, gid: str) -> dict:
    data = store.read_json(store.graph_path(session_dir, gid), cap=S.MAX_GRAPH_BYTES, what="graph")
    if data is None:
        raise FileNotFoundError(f"graph {gid} is not in {session_dir}")
    return data


def load_layer(graph: dict, key: str, layer: str) -> dict:
    """A layer of the REFERENCE source, as persisted (validated, 4-decimal)."""
    if store.bare_key(key) != store.bare_key(graph["reference"]) or layer not in graph["layers"]:
        raise KeyError(f"layer {layer} of {key} is not in the graph")
    data = store.read_json(store.analysis_dir(key) / graph["layers"][layer], cap=S.MAX_LAYER_BYTES)
    if data is None:
        raise FileNotFoundError(f"layer file {graph['layers'][layer]} of {key} is missing")
    return data


def load_scenes(session_dir: str | os.PathLike, gid: str) -> list[dict]:
    g = load_graph(session_dir, gid)
    return store.read_json(store.brain_dir(session_dir) / g["scenes"]) or []


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------

def _reports_layers(cb) -> bool:
    """Does the progress callback take a second (layer name) argument? `lambda p:` and `list.append` do not."""
    try:
        params = list(inspect.signature(cb).parameters.values())
    except (TypeError, ValueError):
        return False
    if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params):
        return True
    return len([p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]) >= 2


class _Run:
    """One analyse() call's working state."""

    def __init__(self, session_dir: Path, *, controls, gateway, layers, force, set_progress, cancel_event):
        self.session_dir = session_dir
        self.controls = controls or {}
        self.gateway = gateway
        self.wanted = set(layers) if layers else set(LAYERS)
        self.force = bool(force)
        self._progress = set_progress
        self._with_layer = set_progress is not None and _reports_layers(set_progress)
        self._cancel = cancel_event
        self.timings: list[dict] = []
        self.paths: dict[str, str] = {}
        self.pin = True

    def check(self) -> None:
        """The cancel point: every layer boundary and the last step before a graph is pinned."""
        if self._cancel is not None and self._cancel.is_set():
            raise _cancelled_exc()("analysis cancelled")

    def step(self, fraction: float, layer: str | None = None) -> None:
        """A boundary: honour a cancel, then say how far along we are and which layer is being read next."""
        self.check()
        if self._progress is None:
            return
        if self._with_layer:
            self._progress(float(fraction), layer)
        else:
            self._progress(float(fraction))

    def nap(self, seconds: float) -> None:
        if self._cancel is not None:
            self._cancel.wait(seconds)
        else:
            time.sleep(seconds)

    def timed(self, layer: str, key: str, fn):
        t0 = time.monotonic()
        out = fn()
        self.timings.append({"layer": layer, "src_key": key, "wall_s": round(time.monotonic() - t0, 3)})
        return out

    def layer(self, key: str, layer: str, params: str, build, *, record: bool = True) -> dict:
        """The cached layer (valid on disk and not forced) or `build()`,
        written through the store (validated) and READ BACK, so downstream
        stages and the digests see exactly the persisted bytes."""
        rebuild = self.force and layer in self.wanted
        self.check()
        if not rebuild and store.layer_status(key, layer, params) == "ok":
            data = store.read_layer(key, layer, params)
        else:
            store.write_layer(key, layer, params, self.timed(layer, key, build))
            data = store.read_layer(key, layer, params)
        if record:      # the header's `layers` names the REFERENCE's files only
            self.paths[layer] = f"{layer}/{params}.json"
        return data


def _default_gateway():
    """The model Gateway unless the brain is pinned off it: `VAI_BRAIN=recipes`
    (or any pin that is not `fm`/`auto`) means no model, ever."""
    pin = (os.environ.get("VAI_BRAIN") or "").strip().lower()
    if pin not in ("", "auto", "fm", "apple_intelligence"):
        return None
    from .gateway import default_gateway
    return default_gateway()


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------

def _probe(spec: dict, i: int) -> dict | None:
    p = Path(spec["path"])
    if not p.is_file():
        _log.warning("brain: skipped %s (not a file)", p.name)
        return None
    kinds = _pcm.stream_kinds(p)
    if not kinds & {"audio", "video"}:
        _log.warning("brain: skipped %s (no audio or video stream)", p.name)
        return None
    return {"path": str(p), "leaf": p.name, "role": spec.get("role"), "angle": spec.get("angle"),
            "key": "src_" + store.src_key(p), "content_key": store.content_key(p),
            "duration": round(_pcm.duration_s(p), 3), "has_audio": "audio" in kinds, "has_video": "video" in kinds,
            "video": _pcm.video_info(p) if "video" in kinds else None, "transcript": spec.get("transcript"),
            "index": i, "tags": []}


def _specs(sources) -> list[dict]:
    return [{"path": os.fspath(s)} if isinstance(s, (str, os.PathLike)) else dict(s) for s in sources]


class _Pcm:
    """Decoded PCM by source key, popped on use so at most a couple of
    files are ever resident."""

    def __init__(self) -> None:
        self._c: dict[str, np.ndarray] = {}

    def get(self, src: dict) -> np.ndarray:
        if src["key"] not in self._c:
            self._c[src["key"]] = _pcm.read_pcm(src["path"]) if src["has_audio"] else np.zeros(0, np.float32)
        return self._c[src["key"]]

    def drop(self, src: dict) -> None:
        self._c.pop(src["key"], None)


def _sync_of(ref_pcm: np.ndarray, other: np.ndarray) -> dict:
    if len(ref_pcm) == 0 or len(other) == 0:
        return {"offset_s": 0.0, "confidence": 0.0, "unverified": True, "anchors": [], "anchors_max_dev_ms": 0.0,
                "engine": _sync.ENGINE}
    return _sync.estimate_offset(ref_pcm, other)


def _find_recorder(srcs: list[dict], primary: dict, cache: _Pcm) -> dict | None:
    """The first audio-only file (no role given) whose sound syncs to v1."""
    if not primary["has_audio"]:
        return None
    cands = [s for s in srcs if s is not primary and not s["has_video"] and s["has_audio"] and not s["role"]]
    for c in cands[:MAX_RECORDER_CANDIDATES]:
        if not _sync_of(cache.get(primary), cache.get(c))["unverified"]:
            return c
        cache.drop(c)
    return None


def _classify(run: _Run, srcs: list[dict], cache: _Pcm) -> tuple[dict, list[dict]]:
    """(reference, members): roles set on every source; members are the
    synced angles with their audio layer and sync result."""
    primary = next((s for s in srcs if s["has_video"]), srcs[0])
    ref = next((s for s in srcs if s["role"] in REFERENCE_ROLES), None) or _find_recorder(srcs, primary, cache) or primary
    ref["role"] = "reference_audio" if not ref["has_video"] else "angle"
    ref_pcm = cache.get(ref)
    members: list[dict] = []
    for s in srcs:
        if s is ref:
            continue
        if s["role"] in NON_ANALYSED_ROLES or not (s["has_video"] or s["role"] == "angle"):
            s["role"] = s["role"] if s["role"] in NON_ANALYSED_ROLES else "other"
            cache.drop(s)
            continue
        est = _sync_of(ref_pcm, cache.get(s))
        if est["unverified"] and s["role"] != "angle":
            s["role"] = "broll"
            cache.drop(s)
        else:
            s["role"], s["sync"], s["sync_offset_s"] = "angle", est, est["offset_s"]
            if est["unverified"]:
                s["tags"].append("sync_unverified")
            s["audio"] = _audio_layer(run, s, cache.get(s), record=False)   # its own mic; the PCM is released
            cache.drop(s)
            members.append(s)
        run.step(P_PROBE + (P_SYNC - P_PROBE) * (s["index"] + 1) / max(1, len(srcs)), "sync")
    return ref, members


def _assign_angles(srcs: list[dict]) -> None:
    """Angle letters in source order (v1 first → `A`); the v1 source is tagged."""
    angles = [s for s in srcs if s["role"] == "angle"]
    free = [c for c in ANGLE_LETTERS if c not in {s["angle"] for s in angles if s.get("angle")}]
    for s in angles:
        if not s.get("angle"):
            s["angle"] = free.pop(0) if free else f"A{s['index']}"
    v1 = next((s for s in angles if s["has_video"]), None)
    if v1 is not None:
        v1["tags"].append("v1")


# --------------------------------------------------------------------------
# layers
# --------------------------------------------------------------------------

def _audio_layer(run: _Run, src: dict, pcm, *, record: bool = True) -> dict:
    name = f"hz{_audio.HZ}-v{ANALYSIS_VERSION}"
    return run.layer(src["key"], "audio", name, lambda: _audio.build_audio_layer(src["path"], pcm=pcm), record=record)


def _reference_audio(run: _Run, ref: dict, ref_pcm, members: list[dict]) -> dict:
    base = _audio_layer(run, ref, ref_pcm)
    if not members:
        return base
    mic = _slug([[m["key"], m["sync_offset_s"]] for m in members])
    name = f"hz{_audio.HZ}-v{ANALYSIS_VERSION}-mic{mic}"
    audio = run.layer(ref["key"], "audio", name, lambda: _audio.attach_own_mic_energy(
        base, {m["key"]: (m["audio"], m["sync_offset_s"]) for m in members}))
    return audio


def _angle_hints(layer: dict, ref_audio: dict, members: list[dict]) -> dict[str, dict]:
    """{speaker id: {angle: letter, by, confidence}}: own-mic correlation
    first, face size for the speakers it leaves unplaced."""
    by_path = {m["path"]: m["audio"] for m in members}
    offsets = {m["path"]: m["sync_offset_s"] for m in members}
    hints = _speakers.angle_hints(layer, ref_audio, by_path, offsets=offsets)
    if len(hints) < len(layer["speakers"]):
        rest = _speakers.face_hints(layer, {m["path"]: m["path"] for m in members if m["has_video"]}, offsets=offsets)
        hints = {**{k: v for k, v in rest.items() if k not in hints}, **hints}
    letter = {m["path"]: m["angle"] for m in members}
    return {sid: {"angle": letter[h["angle"]], "by": h["by"], "confidence": h["confidence"]} for sid, h in hints.items()}


def _speakers_layer(run: _Run, ref: dict, ref_pcm, ref_audio: dict, members: list[dict], tr: dict | None) -> dict:
    k = run.controls.get("speakers")
    mic = _slug([[m["key"], m["angle"], m["sync_offset_s"]] for m in members]) if members else "none"
    thash = hashlib.sha256(canonical(tr)).hexdigest()[:8] if tr else "none"
    name = f"k{'auto' if k is None else int(k)}-mfcc13-s{_speakers.SEED}-v{ANALYSIS_VERSION}-mic{mic}-tr{thash}"

    def build():
        layer = _speakers.build_speakers_layer(ref_pcm, ref_audio, k=k, transcript=tr)
        hints = _angle_hints(layer, ref_audio, members) if members else {}
        for sp in layer["speakers"]:
            sp["angle_hint"] = hints.get(sp["id"])
        return layer

    return run.layer(ref["key"], "speakers", name, build)


def _speech_layer(run: _Run, ref: dict, ref_pcm, ref_audio: dict, speakers: dict, tr: dict | None,
                  whose: str | None = None) -> dict | None:
    """`whose` is recorded only when the transcript is NOT the reference's own
    (a recorder reads v1's, moved onto the reference clock)."""
    if tr is None:
        return None
    params = None if whose in (None, _transcripts.OF_REFERENCE) else {"transcript_of": whose}
    thash = hashlib.sha256(canonical(tr)).hexdigest()[:10]
    name = f"upload-{thash}-v{ANALYSIS_VERSION}-spk{_slug([speakers['params'], speakers['k']])}"
    return run.layer(ref["key"], "speech", name, lambda: _speech.build_speech_layer(
        tr, audio_layer=ref_audio, pcm=ref_pcm, speakers_layer=speakers, params=params))


def _semantic_layer(run: _Run, ref: dict, speech: dict, ref_audio: dict, ref_pcm) -> dict:
    tag = "heur" if run.gateway is None else _slug([run.gateway.engine_version, run.gateway.active_provider_id()])
    name = f"{tag}-v{ANALYSIS_VERSION}-{Path(run.paths['speech']).stem}"
    previous = store.read_layer(ref["key"], "semantic", name) if store.layer_status(ref["key"], "semantic", name) == "ok" else None
    return run.layer(ref["key"], "semantic", name, lambda: _semantic.build_semantic_layer(
        json.loads(json.dumps(speech)), ref_audio, pcm=ref_pcm, gateway=run.gateway, previous=previous))


# --------------------------------------------------------------------------
# header
# --------------------------------------------------------------------------

def _angle_guess(m: dict, speakers: list[dict]) -> dict | None:
    sees = [sp["id"] for sp in speakers if isinstance(sp.get("angle_hint"), dict) and sp["angle_hint"]["angle"] == m["angle"]]
    if not sees:
        return None
    by = next(sp["angle_hint"]["by"] for sp in speakers if sp["id"] == sees[0])
    conf = min(sp["angle_hint"]["confidence"] or 0.0 for sp in speakers if sp["id"] in sees)
    return {"kind": "close" if len(sees) == 1 else "wide", "sees": sees, "confidence": round(float(conf), 3), "by": by}


def _source_row(s: dict, layers: dict[str, str], *, is_ref: bool, dialogue: bool, guess: dict | None) -> dict:
    row = {"key": s["key"], "role": s["role"], "leaf": s["leaf"], "path": s["path"], "content_key": s["content_key"],
           "duration": s["duration"], "sync_offset_s": 0.0 if is_ref else float(s.get("sync_offset_s") or 0.0),
           "has_video": s["has_video"], "has_audio": s["has_audio"], "layers": layers, "dialogue": dialogue,
           "tags": sorted(s["tags"])}
    if s["video"]:
        row["fps"] = s["video"]["fps"]
    if s["role"] == "angle":
        row["angle"] = s["angle"]
    if guess:
        row["angle_guess"] = guess
    return row


def _hdr_speakers(ref: dict, members: list[dict], speakers: dict, speech: dict | None) -> list[dict]:
    """The header's speakers; a lone speaker on a lone picture file sits on it."""
    rows = GS.header_speakers(speakers, speech)
    if len(rows) == 1 and ref["role"] == "angle" and not members and rows[0]["angle_hint"] is None:
        rows[0]["angle_hint"] = {"angle": ref["angle"], "by": "sole_speaker", "confidence": 1.0}
    return rows


def _header(run: _Run, ref: dict, members: list[dict], others: list[dict], ref_audio: dict, hdr_speakers: list[dict],
            speech: dict | None, semantic: dict | None, scenes: list[dict], primary: dict | None,
            digests: dict) -> dict:
    sem_status = _semantic.layer_status(semantic) if semantic else "missing"
    ref_layers = {"speech": _speech_status(speech, ref["duration"]), "speakers": "ok", "audio": "ok",
                  "semantic": sem_status}
    rows = [_source_row(ref, ref_layers, is_ref=True, dialogue=True,
                        guess=_angle_guess(ref, hdr_speakers) if ref["role"] == "angle" else None)]
    if ref["role"] == "angle" and not members:
        rows[0]["angle_guess"] = _lone_guess(hdr_speakers)
    rows += [_source_row(m, {"audio": "ok"}, is_ref=False, dialogue=False, guess=_angle_guess(m, hdr_speakers))
             for m in members]
    rows += [_source_row(o, {}, is_ref=False, dialogue=False, guess=None) for o in others]
    guess = GS.classify_content(hdr_speakers, (speech or {}).get("sentences") or [], len(members) + (ref["role"] == "angle"),
                                speech is not None)
    ident = {"digests": dict(sorted(digests.items())), "offsets": [[m["key"], m["sync_offset_s"]] for m in members],
             "roles": [[r["key"], r["role"]] for r in rows], "analysis_version": ANALYSIS_VERSION}
    gid = "g_" + hashlib.sha256(canonical(ident)).hexdigest()[:12]
    vid = (primary or {}).get("video") or {}
    return {"version": 1, "id": gid, "analysis_version": ANALYSIS_VERSION, "clock": "reference",
            "reference": ref["key"], "sources": rows, "speakers": hdr_speakers, "content_type": guess,
            "layers": {k: v for k, v in run.paths.items()}, "digests": digests, "scenes": "scenes.json",
            "topics": _topics(scenes), "music_hint": GS.music_hint(semantic, guess["guess"]),
            "project": {"canvas": [vid["width"], vid["height"]] if vid else None, "fps": vid.get("fps"),
                        "session_language": ((speech or {}).get("params") or {}).get("language"),
                        "controls_seen": sorted(run.controls)}}


#: A transcript with fewer real words than this, or fewer per minute, is not speech: a tone or a silent clip
#: still gets one hallucinated word ("you") or an annotation from the recogniser (review UX-01).
MIN_SPEECH_WORDS = 5
MIN_SPEECH_WPM = 5.0


def _speech_status(speech: dict | None, duration_s: float | None) -> str:
    """`missing` (no transcript was read), `empty` (one was, with next to no words in it), else `ok`."""
    if not speech:
        return "missing"
    n = sum(1 for w in speech.get("words") or [] if not w.get("filler"))
    minutes = max(1e-6, float(duration_s or 0.0) / 60.0)
    return "empty" if n < MIN_SPEECH_WORDS or n / minutes < MIN_SPEECH_WPM else "ok"


def _lone_guess(speakers: list[dict]) -> dict | None:
    if len(speakers) != 1:
        return None
    return {"kind": "close", "sees": [speakers[0]["id"]], "confidence": 1.0, "by": "sole_speaker"}


def _topics(scenes: list[dict]) -> list[dict]:
    speech_scenes = [s for s in scenes if s["kind"] == "speech"]
    if not speech_scenes:
        return []
    return [{"id": GS.TOPIC_ID, "t0": speech_scenes[0]["t0"], "t1": speech_scenes[-1]["t1"], "title": None,
             "by": "recipes", "sents": [i for s in speech_scenes for i in s["sents"]]}]


def _digests(ref_audio: dict, speakers: dict, speech: dict | None, semantic: dict | None, members: list[dict]) -> dict:
    d = {"audio": digest_of(ref_audio), "speakers": digest_of(speakers)}
    if speech:
        d["speech"] = digest_of(speech)
    if semantic:
        d["semantic"] = "sha256:" + hashlib.sha256(_semantic.canonical_content(semantic)).hexdigest()
    for m in members:
        d[f"audio:{m['key']}"] = digest_of(m["audio"])
    return d


# --------------------------------------------------------------------------
# entry
# --------------------------------------------------------------------------

def _transcript_pending(ref: dict, members: list[dict]) -> bool:
    """True when the transcript of record is not on disk yet but is on its way: an upload whose background
    whisper pass has not rewritten its `ingest.json`."""
    if _transcripts.transcript_of_record(ref, members)[0] is not None:
        return False
    return any(_transcripts.transcript_state(x["path"], x.get("transcript")) == _transcripts.PENDING
               for x in [ref, *members] if x["has_audio"])


def _wait_for_transcript(run: _Run, ref: dict, members: list[dict], wait_s: float, poll_s: float) -> bool:
    """Wait (cancellably, up to `wait_s`) for the upload's transcript; True when it is STILL not there."""
    deadline = time.monotonic() + max(0.0, float(wait_s))
    while _transcript_pending(ref, members):
        run.step(P_SYNC, "transcript")
        if time.monotonic() >= deadline:
            return True
        run.nap(min(float(poll_s), max(0.01, deadline - time.monotonic())))
    return False


def _lock_session(run: _Run) -> threading.Lock:
    """Take the session's analysis lock, honouring a cancel while queued behind another run."""
    lock = _session_lock(run.session_dir)
    while not lock.acquire(timeout=0.2):
        run.check()
    return lock


def analyse(session_dir: str | os.PathLike, sources: list, *, set_progress=None, cancel_event=None,
            layers: list[str] | None = None, force: bool = False, controls: dict | None = None,
            gateway: Any = "auto", wait_transcript_s: float = 0.0,
            transcript_poll_s: float = TRANSCRIPT_POLL_S) -> GraphId:
    """Build (or reuse) every layer for `sources`, assemble the graph under
    `<session>/brain/`, make it the session's current graph, return its id.

    `layers` limits which layers are REBUILT (with `force`); the rest come
    from the cache or are built when missing — a graph is always complete.
    `gateway`: "auto" = the model gateway unless `VAI_BRAIN` pins it off;
    None = heuristics only.

    The transcript of an upload arrives AFTER the upload answers (a background
    whisper pass rewrites its `ingest.json`). A graph made before it lands has
    no speech layer and would plan from nothing, so while the transcript is on
    its way this waits up to `wait_transcript_s` (cancellable); if it is still
    not there the graph is built with the speech layer marked `missing`, is NOT
    pinned as current, and the returned `GraphId` says `waiting_for="transcript"`.

    `set_progress(fraction[, layer])`: a callback that takes a second argument is
    told which layer is being read next (`sync`, `transcript`, `audio`, `speakers`,
    `speech`, `semantic`, `graph`, `done`). `cancel_event` is honoured at every
    layer boundary and right before the graph is pinned (`JobCancelled`)."""
    session_dir = Path(session_dir)
    run = _Run(session_dir, controls=controls, gateway=_default_gateway() if gateway == "auto" else gateway,
               layers=layers, force=force, set_progress=set_progress, cancel_event=cancel_event)
    lock = _lock_session(run)
    try:
        srcs = [s for s in (_probe(sp, i) for i, sp in enumerate(_specs(sources))) if s is not None]
        if not srcs:
            raise ValueError("no analysable media among the sources")
        run.step(P_PROBE, "sync")
        cache = _Pcm()
        ref, members = _classify(run, srcs, cache)
        _assign_angles(srcs)
        ref_pcm = cache.get(ref)
        still_waiting = _wait_for_transcript(run, ref, members, wait_transcript_s, transcript_poll_s)
        run.pin = not still_waiting
        run.step(P_SYNC, "audio")
        gid = _build(run, srcs, ref, members, ref_pcm)
        return GraphId(gid, pinned=run.pin, waiting_for="transcript" if still_waiting else None)
    finally:
        lock.release()


def _build(run: _Run, srcs: list[dict], ref: dict, members: list[dict], ref_pcm) -> str:
    ref_audio = _reference_audio(run, ref, ref_pcm, members)
    run.step(P_AUDIO, "speakers")
    tr, whose = _transcripts.transcript_of_record(ref, members)
    speakers = _speakers_layer(run, ref, ref_pcm, ref_audio, members, tr)
    run.step(P_SPEAKERS, "speech")
    speech = _speech_layer(run, ref, ref_pcm, ref_audio, speakers, tr, whose)
    run.step(P_SPEECH, "semantic")
    semantic = _semantic_layer(run, ref, speech, ref_audio, ref_pcm) if speech else None
    run.step(P_SEMANTIC, "graph")
    others = [s for s in srcs if s is not ref and s not in members]
    return _assemble(run, srcs, ref, members, others, ref_audio, speakers, speech, semantic)


def _write_sources(run: _Run, srcs: list[dict]) -> None:
    sid = run.session_dir.name
    for s in srcs:
        info = {"src_key": store.bare_key(s["key"]), "content_key": s["content_key"], "leaf": s["leaf"],
                "duration": s["duration"], "fps": (s["video"] or {}).get("fps"), "has_audio": s["has_audio"],
                "has_video": s["has_video"], "proxy_key": None}
        try:
            from ..ingest import proxy as _proxy
            info["proxy_key"] = _proxy.proxy_key(s["path"])
        except Exception:
            pass
        store.write_source(s["key"], info)
        store.add_ref(s["key"], sid)


def _assemble(run: _Run, srcs: list[dict], ref: dict, members: list[dict], others: list[dict], ref_audio: dict,
              speakers: dict, speech: dict | None, semantic: dict | None) -> str:
    hdr_speakers = _hdr_speakers(ref, members, speakers, speech)
    active = {sp["id"]: next((m["key"] for m in members if m["angle"] == sp["angle_hint"]["angle"]),
                             ref["key"] if ref["role"] == "angle" else None)
              for sp in hdr_speakers if isinstance(sp.get("angle_hint"), dict)}
    if ref["role"] == "angle" and not members:
        active = {sp["id"]: ref["key"] for sp in hdr_speakers}
    scenes = GS.build_scenes(speech, semantic, ref_audio, active)
    primary = next((s for s in srcs if s["has_video"]), None)
    for m in members + ([ref] if ref["role"] == "angle" else []):
        m["sees"] = [sp["id"] for sp in hdr_speakers if isinstance(sp.get("angle_hint"), dict)
                     and sp["angle_hint"]["angle"] == m.get("angle")]
    digests = _digests(ref_audio, speakers, speech, semantic, members)
    header = S.Graph.model_validate(_header(run, ref, members, others, ref_audio, hdr_speakers, speech, semantic, scenes,
                                            primary, digests))
    _write_sources(run, srcs)
    bd = store.brain_dir(run.session_dir)
    checked = S.Scenes.model_validate(scenes)
    store.write_json(bd / "scenes.json", [sc.model_dump(mode="json") for sc in checked.items], cap=S.MAX_LAYER_BYTES,
                     what="scenes")
    store.write_json(bd / "angles.json", S.Angles.model_validate(GS.angles_json(ref, members)), cap=S.MAX_LAYER_BYTES,
                     what="angles")
    store.write_graph(run.session_dir, header)
    with open(bd / "timings.jsonl", "a", encoding="utf-8") as fh:
        for row in run.timings:
            fh.write(json.dumps({**row, "gid": header.id}) + "\n")
    run.check()                      # the last cancel point: a cancelled read pins nothing
    if run.pin:
        store.set_current_graph(run.session_dir, header.id)
    run.step(1.0, "done")
    return header.id


def blockers(graph: dict) -> list[dict]:
    """Why no edit may be OFFERED on this graph, in one sentence each: `[{code, message}]`, empty when the
    graph can be planned on. A graph without its speech layer has no words to cut on, no hook and no story —
    a plan made on it would be a "reel" of the whole clip with no cuts. Takes the header (`load_graph`) or a
    golden's envelope; a shape it cannot read blocks nothing (the loaders validate)."""
    header = graph.get("graph") if isinstance(graph.get("graph"), dict) else graph
    status = store.graph_speech_status(header)
    if status == "missing":
        return [{"code": "no_speech", "message": "The speech in this footage has not been read yet, so there is "
                                                 "nothing to cut or caption on."}]
    if status == "empty":
        return [{"code": "silent", "message": "There is no speech in this footage to edit."}]
    return []


__all__ = ["REFERENCE_ROLES", "Cancelled", "GraphId", "blockers", "canonical", "digest_of", "graph_path", "load_graph", "load_layer",
           "load_scenes", "analyse"]
