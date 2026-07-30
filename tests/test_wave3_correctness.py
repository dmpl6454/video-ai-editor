"""Wave-3/4 correctness fixes: arg validation, dead transform, transcript, dupes.

Each test corresponds to a defect that produced silently wrong output or a
silently dead control, rather than a visible error — the class of bug a tester
cannot report because nothing looks broken.
"""
from __future__ import annotations
import json
import subprocess
from pathlib import Path

import pytest

from importlib import import_module

# NOT `import video_ai_editor.agent.dispatch as D`: the package re-exports a
# `dispatch` FUNCTION, and that form binds the attribute, so D would be the
# function rather than the module.
D = import_module("video_ai_editor.agent.dispatch")
from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Canvas, Clip, Transform, empty_edl


def _mk(path: Path, duration: float = 4.0, w: int = 320, h: int = 180) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=30:duration={duration}",
         "-f", "lavfi", "-i", f"sine=f=440:duration={duration}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(path)],
        check=True, capture_output=True)
    return path


@pytest.fixture()
def store(tmp_path) -> EDLStore:
    src = _mk(tmp_path / "v.mp4")
    edl = empty_edl(Canvas(w=320, h=180, fps=30))
    edl.get_track("v1").clips.append(Clip(src=str(src), in_=0.0, out=4.0, start=0.0))
    edl.recompute_duration()
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(tmp_path)


def _cid(store: EDLStore) -> str:
    return store.edl.get_track("v1").clips[0].id


# ---------------------------------------------------------------- RC-ARGS

@pytest.mark.parametrize("bad", [None, "abc", float("nan"), float("inf"), {}, []])
def test_non_numeric_trim_is_rejected_not_stored(store, bad):
    """NaN/inf used to serialize into edl.json as null and make it unloadable;
    None and a non-numeric string produced an HTTP 500 traceback."""
    before = store.edl.get_track("v1").clips[0].in_
    with pytest.raises((ValueError, TypeError)):
        dispatch(store, "trim_clip", {"clip_id": _cid(store), "in": bad})
    assert store.edl.get_track("v1").clips[0].in_ == before


def test_zero_length_trim_is_rejected(store):
    """Clearing the Out field in the UI committed out=0, leaving a zero-length
    clip that stayed in the EDL and still entered the filtergraph."""
    with pytest.raises(ValueError, match="greater than"):
        dispatch(store, "trim_clip", {"clip_id": _cid(store), "out": 0.0})
    assert store.edl.get_track("v1").clips[0].duration > 0


def test_out_is_clamped_to_the_source_duration(store):
    """out=20 on a 4s source was accepted, so the timeline claimed more media
    than exists and the render diverged from the timeline."""
    dispatch(store, "trim_clip", {"clip_id": _cid(store), "out": 20.0})
    assert store.edl.get_track("v1").clips[0].out == pytest.approx(4.0, abs=0.2)


def test_negative_audio_fade_is_clamped(store):
    """A negative fade makes afade emit nothing — it read as 'fade stopped
    working'. set_video_fade already clamped; add_fade did not."""
    dispatch(store, "add_fade", {"clip_id": _cid(store), "in_s": -3.0})
    assert store.edl.get_track("v1").clips[0].audio.fade_in == 0.0


def test_num_helper_rejects_non_finite():
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            D._num({"k": bad}, "k")
    assert D._num({"k": "2.5"}, "k") == 2.5
    assert D._num({}, "k", 7.0) == 7.0
    assert D._num({"k": -5}, "k", min=0.0) == 0.0


# ------------------------------------------------------- RC-CAPGAP (P13-b)

def test_static_transform_pan_reaches_the_filtergraph(store):
    """transform.x/y were emitted ONLY by the keyframed branch, so the static
    Position inputs committed, re-rendered, and never moved the picture."""
    from video_ai_editor.render.compositor import _build_clip_video_chain as build
    c = store.edl.get_track("v1").clips[0]

    c.transform = Transform()
    neutral = build(c, input_label="[0:v]", label_out="[v]", canvas_w=320, canvas_h=180)

    c.transform = Transform(x=40.0, y=-20.0)
    panned = build(c, input_label="[0:v]", label_out="[v]", canvas_w=320, canvas_h=180)

    assert panned != neutral, "a static pan produced an identical filter chain"
    assert "crop=320:180" in panned


def test_zero_pan_does_not_change_the_chain(store):
    """Guard the 99% case: x=y=0 must not start emitting pad/crop work."""
    from video_ai_editor.render.compositor import _build_clip_video_chain as build
    c = store.edl.get_track("v1").clips[0]
    c.transform = Transform()
    a = build(c, input_label="[0:v]", label_out="[v]", canvas_w=320, canvas_h=180)
    c.transform = Transform(x=0.0, y=0.0)
    b = build(c, input_label="[0:v]", label_out="[v]", canvas_w=320, canvas_h=180)
    assert a == b


# ----------------------------------------------------------- C5-X1 transcript

def test_export_srt_works_from_a_whisper_transcript(store, tmp_path):
    """`_load_transcript` read only <session>/transcript.json, which ONLY
    import_srt writes — so export_srt/vtt/ass raised 'no transcript on this
    project' for every whisper project."""
    clip = store.edl.get_track("v1").clips[0]
    ing_dir = Path(clip.src).parent
    (ing_dir / "ingest.json").write_text(json.dumps({
        "transcript": {"language": "en", "duration": 4.0, "segments": [
            {"id": 0, "start": 0.0, "end": 1.5, "text": "hello there"},
            {"id": 1, "start": 1.5, "end": 3.0, "text": "second line"},
        ]}
    }))
    res = dispatch(store, "export_srt", {})
    out = Path(res["path"])
    assert out.exists()
    body = out.read_text(encoding="utf-8")
    assert "hello there" in body and "second line" in body


def test_explicit_transcript_json_wins_over_ingest(store):
    """An imported/edited transcript is a deliberate override."""
    clip = store.edl.get_track("v1").clips[0]
    (Path(clip.src).parent / "ingest.json").write_text(json.dumps({
        "transcript": {"language": "en", "duration": 4.0,
                       "segments": [{"id": 0, "start": 0.0, "end": 1.0,
                                     "text": "from ingest"}]}
    }))
    (store.dir / "transcript.json").write_text(json.dumps({
        "language": "en", "duration": 4.0,
        "segments": [{"id": 0, "start": 0.0, "end": 1.0, "text": "from import"}],
    }))
    tr = D._load_transcript(store)
    assert tr is not None and tr.segments[0].text == "from import"


# ----------------------------------------------------- RC-REGISTRY (QA-M2)

def test_no_duplicate_dispatch_handler_definitions():
    """Three handlers were defined twice with different bodies. Python keeps the
    last, so the earlier body was unreachable — any edit to it would be silently
    ignored. An AST check is the only way to keep this from recurring."""
    import ast
    src = Path(D.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    seen: dict[str, list[int]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            seen.setdefault(node.name, []).append(node.lineno)
    dupes = {n: ls for n, ls in seen.items() if len(ls) > 1}
    assert not dupes, f"duplicate module-level handler definitions: {dupes}"


def test_dispatch_dict_has_no_duplicate_keys():
    """A duplicated DISPATCH key silently shadows the earlier entry."""
    import ast
    src = Path(D.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        # `DISPATCH: dict[str, DispatchFn] = {...}` is an AnnAssign, so matching
        # only ast.Assign silently found nothing and the check never ran.
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Assign):
            targets = node.targets
        else:
            continue
        if not any(getattr(t, "id", None) == "DISPATCH" for t in targets):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        keys = [k.value for k in node.value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        dupes = {k for k in keys if keys.count(k) > 1}
        assert not dupes, f"duplicate DISPATCH keys: {sorted(dupes)}"
        return
    pytest.fail("DISPATCH dict literal not found")


def test_set_track_muted_toggles_when_muted_is_omitted():
    """The surviving definition's better default — the dead one always muted."""
    import inspect
    src = inspect.getsource(D.set_track_muted)
    assert "not track.muted" in src
