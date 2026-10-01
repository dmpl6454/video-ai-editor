"""`cut_source_ranges` — the Editor Brain's cut tool (EB1-B, spec §5.4 row 1).

A public wrapper over `dispatch._cut_source_ranges`: SOURCE ranges of named
files, merged per file, cut last-first, re-mapped through the LIVE EDL before
every cut, one commit. What these tests prove, by executing the tool:

* it removes exactly what `remove_silences` + `remove_fillers` remove on the
  benchmark talking-head (the ranges those tools computed, handed to the new
  tool, give the same v1 layout);
* over 200 random cut sets on 1x, 2x, reversed and duplicated clips it equals
  the private helper it wraps (the `tests/test_transcript_timemap.py` style:
  assertions on the surviving SOURCE ranges, never on summaries);
* ranges cut only clips of their own file; the caps and the path guard hold;
* it is ONE op (one undo step), and none inside an enclosing batch.
"""
from __future__ import annotations

import importlib
import json
import random
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain_tool_fixtures as BT  # noqa: E402
import prompt_fixtures as F  # noqa: E402

from video_ai_editor import config  # noqa: E402
from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.agent.prompt import changes as C  # noqa: E402
from video_ai_editor.edl.snapshot import EDLStore  # noqa: E402

# NOT `from video_ai_editor.agent import dispatch as D`: the package re-exports
# the dispatch FUNCTION under that name.
D = importlib.import_module("video_ai_editor.agent.dispatch")

BENCH = Path.home() / "Library/Caches/Video AI Editor/bench/8515fa4411c9"
BENCH_SRC = BENCH / "scene_16x9.mp4"
BENCH_TRUTH = BENCH / "narration_en.json"
#: A 360p transcode of the bench talking-head with its AUDIO STREAM COPIED, so
#: silencedetect sees the bench's own samples; built once, beside the bench.
BENCH_LITE = BENCH.parent / "eb1-tools" / "scene_16x9_360p" / "scene.normalized.mp4"


@pytest.fixture(autouse=True)
def _posture():
    with BT.restriction_off():
        yield


def _ops(store: EDLStore) -> int:
    return len(store.ops.ops)


# --------------------------------------------------------- the bench fixture

def _bench_lite() -> Path:
    if BENCH_LITE.exists() and BENCH_LITE.stat().st_mtime >= BENCH_SRC.stat().st_mtime:
        return BENCH_LITE
    BENCH_LITE.parent.mkdir(parents=True, exist_ok=True)
    tmp = BENCH_LITE.with_suffix(".part.mp4")
    BT._run(["-i", str(BENCH_SRC), "-vf", "scale=-2:360", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "28", "-c:a", "copy", str(tmp)])
    tmp.replace(BENCH_LITE)
    return BENCH_LITE


def _truth_transcript() -> dict:
    """The narration truth as a word-timed transcript: one segment per speech
    utterance (tokens spread evenly over its voiced span, the model
    `caption_format` applies) and ONE word per filler utterance at its voiced
    span — so `remove_fillers` cuts the planted fillers by their true time."""
    truth = json.loads(BENCH_TRUTH.read_text(encoding="utf-8"))
    segs: list[dict] = []
    for u in truth["utterances"]:
        if u["kind"] not in ("speech", "filler") or u.get("voiced_start") is None:
            continue
        s, e = float(u["voiced_start"]), float(u["voiced_end"])
        toks = u["text"].split()
        step = (e - s) / max(1, len(toks))
        words = [{"word": w, "start": round(s + i * step, 3), "end": round(s + (i + 1) * step, 3),
                  "prob": 1.0} for i, w in enumerate(toks)]
        segs.append({"id": len(segs), "start": s, "end": e, "text": u["text"], "words": words})
    return {"language": "en", "duration": float(truth["duration"]), "segments": segs}


def _bench_store(root: Path, name: str) -> tuple[EDLStore, str]:
    up = root / name / "uploads" / "scene"
    up.mkdir(parents=True)
    src = up / "scene.normalized.mp4"
    try:
        (src).hardlink_to(_bench_lite())
    except OSError:
        shutil.copy2(_bench_lite(), src)
    (up / "ingest.json").write_text(json.dumps({"transcript": _truth_transcript()}), encoding="utf-8")
    from video_ai_editor.ingest.probe import probe
    store = EDLStore(root / name / "sess")
    dispatch(store, "add_clip", {"track": "v1", "src": str(src), "in": 0, "out": probe(src).duration,
                                 "start": 0})
    return store, str(store.edl.get_track("v1").clips[0].src)


@pytest.mark.skipif(not (BENCH_SRC.exists() and BENCH_TRUTH.exists()),
                    reason="needs the benchmark talking-head media")
def test_equals_remove_silences_plus_remove_fillers_on_bench_fixture(tmp_path, monkeypatch):
    """The ranges `remove_silences` then `remove_fillers` hand to the private
    helper, given to `cut_source_ranges` in the same two steps — and in ONE
    step, merged — leave the identical v1 layout."""
    seen: list[list[tuple[str | None, float, float]]] = []
    real = D._cut_source_ranges

    def spy(store, track_id, source_ranges):
        seen.append([(s, float(a), float(b)) for s, a, b in source_ranges])
        return real(store, track_id, source_ranges)

    monkeypatch.setattr(D, "_cut_source_ranges", spy)
    ref, _ = _bench_store(tmp_path, "ref")
    dispatch(ref, "remove_silences", {})
    dispatch(ref, "remove_fillers", {})
    monkeypatch.setattr(D, "_cut_source_ranges", real)
    assert len(seen) == 2 and seen[0] and seen[1], seen
    silences, fillers = seen
    assert len(silences) >= 5 and len(fillers) >= 5, (len(silences), len(fillers))

    def ranges(rows, src):
        return [{"src": src, "start": a, "end": b} for _s, a, b in rows]

    two, src2 = _bench_store(tmp_path, "two")
    r1 = dispatch(two, "cut_source_ranges", {"track": "v1", "ranges": ranges(silences, src2)})
    r2 = dispatch(two, "cut_source_ranges", {"track": "v1", "ranges": ranges(fillers, src2)})
    assert r1["cuts"] + r2["cuts"] >= 10
    one, src1 = _bench_store(tmp_path, "one")
    dispatch(one, "cut_source_ranges", {"track": "v1", "ranges": ranges(silences + fillers, src1)})

    def relative(store):
        return [(round(i, 4), round(o, 4), round(s, 4)) for _src, i, o, s in BT.layout(store)]

    assert relative(two) == relative(ref)
    assert relative(one) == relative(ref)
    assert len(relative(ref)) >= 10


# ------------------------------------------------ 200 random sets vs helper

_VARIANTS = ("1x", "2x", "reversed", "duplicated")


@pytest.fixture(scope="module")
def variants(tmp_path_factory):
    """One base session per variant; every random set copies one."""
    root = tmp_path_factory.mktemp("csr")
    with BT.restriction_off():
        src = F.speech_clip(root)
        F.write_ingest(src)
        out: dict[str, Path] = {}
        for name in _VARIANTS:
            store = EDLStore(root / name)
            dispatch(store, "add_clip", {"track": "v1", "src": str(src), "in": 0, "out": F.CLIP_DUR,
                                         "start": 0})
            cid = store.edl.get_track("v1").clips[0].id
            if name == "2x":
                dispatch(store, "set_speed", {"clip_id": cid, "factor": 2.0})
            elif name == "reversed":
                dispatch(store, "set_clip_reverse", {"clip_id": cid, "reverse": True})
            elif name == "duplicated":
                dispatch(store, "split_at", {"track": "v1", "time": 5.0})
                dispatch(store, "duplicate_clip", {"clip_id": cid})
            out[name] = root / name
    return {"root": root, "src": str(src), "sessions": out}


def _random_set(rng: random.Random, src: str) -> list[dict]:
    n = rng.randint(1, 6)
    out = []
    for _ in range(n):
        a = round(rng.uniform(0.0, F.CLIP_DUR - 0.1), 3)
        b = round(min(F.CLIP_DUR, a + rng.uniform(0.05, 3.0)), 3)
        out.append({"src": src, "start": a, "end": b})
    return out


def test_200_random_cut_sets_over_1x_2x_and_reversed_equal_private_helper(variants, tmp_path):
    rng = random.Random(20260929)
    for i in range(200):
        name = _VARIANTS[i % len(_VARIANTS)]
        ranges = _random_set(rng, variants["src"])
        a_dir, b_dir = tmp_path / f"a{i}", tmp_path / f"b{i}"
        shutil.copytree(variants["sessions"][name], a_dir)
        shutil.copytree(variants["sessions"][name], b_dir)
        a, b = EDLStore(a_dir), EDLStore(b_dir)
        res = dispatch(a, "cut_source_ranges", {"track": "v1", "ranges": ranges})
        with b.batch():
            n = D._cut_source_ranges(b, "v1", [(r["src"], r["start"], r["end"]) for r in ranges])
        b.commit("helper", {}, "helper")
        assert BT.layout(a) == BT.layout(b), (name, ranges)
        assert res["cuts"] == n, (name, ranges, res)
        # (not the tree hash: cut_range names each right-hand piece with a fresh uuid)


# --------------------------------------------------------------- multi-src

def test_multi_src_ranges_cut_only_their_own_clips(tmp_path):
    a_src = F.speech_clip(tmp_path, "talk")
    b_src = F.speech_clip(tmp_path, "other")
    store = EDLStore(tmp_path / "sess")
    dispatch(store, "add_clip", {"track": "v1", "src": str(a_src), "in": 0, "out": F.CLIP_DUR, "start": 0})
    dispatch(store, "add_clip", {"track": "v1", "src": str(b_src), "in": 0, "out": F.CLIP_DUR,
                                 "start": F.CLIP_DUR})
    a_path, b_path = (c.src for c in BT.v1_pieces(store))
    res = dispatch(store, "cut_source_ranges", {
        "track": "v1", "ranges": [{"src": b_path, "start": 2.0, "end": 3.0},
                                  {"src": b_path, "start": 2.5, "end": 3.5},   # overlaps: merged
                                  {"src": a_path, "start": 10.0, "end": 11.0}]})
    lay = BT.layout(store)
    assert res["cuts"] == 2
    assert [(s, i, o) for s, i, o, _ in lay] == [
        (a_path, 0.0, 10.0), (a_path, 11.0, 12.0), (b_path, 0.0, 2.0), (b_path, 3.5, 12.0)]
    assert res["removed_s"] == pytest.approx(2.5, abs=1 / 30)


def test_ranges_off_the_track_are_skipped_not_cut(tmp_path):
    store = F.make_store(tmp_path)
    src = BT.v1_pieces(store)[0].src
    other = F.speech_clip(tmp_path, "unused")
    before = store.edl.hash()
    res = dispatch(store, "cut_source_ranges", {"track": "v1", "ranges": [
        {"src": str(other), "start": 1.0, "end": 2.0},
        {"src": src, "start": 20.0, "end": 21.0}]})    # past the clip's source extent
    assert res["cuts"] == 0 and len(res["skipped"]) == 2
    assert store.edl.hash() == before


# ------------------------------------------------------------ guards, caps

def test_path_guard_and_2000_cap(tmp_path, monkeypatch):
    store = F.make_store(tmp_path)
    src = BT.v1_pieces(store)[0].src
    one = {"src": src, "start": 1.0, "end": 1.1}
    with pytest.raises(ValueError, match="2000"):
        dispatch(store, "cut_source_ranges", {"track": "v1", "ranges": [one] * 2001})
    with pytest.raises(ValueError, match="end"):
        dispatch(store, "cut_source_ranges", {"track": "v1", "ranges": [{"src": src, "start": 2.0, "end": 2.0}]})
    with pytest.raises(ValueError, match="600"):
        dispatch(store, "cut_source_ranges", {"track": "v1", "ranges": [{"src": src, "start": 0.0, "end": 601.0}]})
    with pytest.raises(ValueError, match="src"):
        dispatch(store, "cut_source_ranges", {"track": "v1", "ranges": [{"start": 1.0, "end": 2.0}]})
    with pytest.raises(ValueError):
        dispatch(store, "cut_source_ranges", {"track": "v1", "ranges": ["not-a-range"]})
    # The allowlist: with restriction ON only WORKDIR is readable; the session's
    # own upload is inside it, a file elsewhere is refused before any cut.
    outside = F.speech_clip(tmp_path / "elsewhere", "far")
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    config.enable_path_restriction(True)
    try:
        with pytest.raises(ValueError, match="outside"):
            dispatch(store, "cut_source_ranges", {"track": "v1", "ranges": [
                {"src": str(tmp_path.parent / "x" / "y.mp4"), "start": 1.0, "end": 2.0}]})
        res = dispatch(store, "cut_source_ranges", {"track": "v1", "ranges": [one]})
        assert res["cuts"] == 1
    finally:
        config.enable_path_restriction(False)
    assert outside.exists()


def test_one_commit(tmp_path):
    store = F.make_store(tmp_path)
    src = BT.v1_pieces(store)[0].src
    before_hash, before_ops, depth = store.edl.hash(), _ops(store), store.undo_depth
    res = dispatch(store, "cut_source_ranges", {"track": "v1", "ranges": [
        {"src": src, "start": 1.0, "end": 2.0}, {"src": src, "start": 5.0, "end": 6.0},
        {"src": src, "start": 9.0, "end": 9.5}], "why": "three planted pauses"})
    assert res["cuts"] == 3
    assert _ops(store) == before_ops + 1 and store.undo_depth == depth + 1
    op = store.ops.ops[-1]
    assert op.tool == "cut_source_ranges" and "Cut 3 ranges" in op.summary
    assert store.undo() and store.edl.hash() == before_hash
    # Inside an enclosing batch (a plan step) the tool records nothing itself.
    with store.batch():
        dispatch(store, "cut_source_ranges", {"track": "v1", "ranges": [{"src": src, "start": 1.0, "end": 2.0}]})
        assert _ops(store) == before_ops
    store.commit("prompt", {}, "plan")
    assert _ops(store) == before_ops + 1


def test_the_op_text_reads_as_a_sentence_for_one_range(tmp_path):
    """UX-13: '1 ranges' — the count word follows the count."""
    store = F.make_store(tmp_path)
    src = BT.v1_pieces(store)[0].src
    res = dispatch(store, "cut_source_ranges", {"track": "v1", "ranges": [{"src": src, "start": 1.0, "end": 2.0}]})
    assert res["summary"].startswith("Cut 1 range on v1 (") and "1 ranges" not in store.ops.ops[-1].summary


def test_card_covers_every_changed_key(tmp_path):
    store = F.make_store(tmp_path)
    src = BT.v1_pieces(store)[0].src
    before = store.edl.model_copy(deep=True)
    dispatch(store, "cut_source_ranges", {"track": "v1", "ranges": [
        {"src": src, "start": 1.0, "end": 2.0}, {"src": src, "start": 5.0, "end": 6.0}]})
    lines = C.summarize(before, store.edl, session_dir=Path(store.dir))
    assert C.diff_keys(before, store.edl) <= C.covered(lines)
    assert any("Deleted" in c.text for c in lines), [c.text for c in lines]
