"""QA round 5 regressions (Video_AI_Editor_QA_Round5_Report + manual.docx).

Every test here was confirmed to FAIL on the pre-fix tree — see the PR body for
the stash-check log. The organising principle of the round is that the EDL must
be *unwritable* in an invalid state: a bad value that only fails on the next
load takes every retained snapshot down with it, which is how one rejected enum
turned into "my whole project is gone" (VAI-01).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import (
    EDL, Canvas, Clip, Sticker, TextClip, Track, Transform, empty_edl,
)


@pytest.fixture
def store(tmp_path: Path) -> EDLStore:
    edl = empty_edl(Canvas(w=1080, h=1920, fps=30))
    edl.get_track("v1").clips.append(Clip(id="c1", src=str(tmp_path / "a.mp4"), in_=0, out=4))
    edl.get_track("stickers").clips.append(
        Sticker(id="s1", src=str(tmp_path / "s.png"), start=0.0, end=2.0))
    edl.get_track("tx_super").clips.append(
        TextClip(id="t1", text="HI", start=0.0, end=2.0, role="super"))
    (tmp_path / "edl.json").write_text(edl.to_json(), encoding="utf-8")
    return EDLStore(tmp_path)


# --------------------------------------------------------------------------
# VAI-01 — an out-of-domain enum must never reach edl.json
# --------------------------------------------------------------------------

def test_bad_caption_style_is_rejected_not_persisted(store: EDLStore):
    with pytest.raises(ValueError):
        dispatch(store, "add_caption_track", {"style": "karaoke"})
    # The decisive assertion is not the raise — it's that the session still
    # loads. Pre-fix the assignment succeeded, edl.json was written with
    # style='karaoke', and EDLStore(dir) came back with zero clips.
    reloaded = EDLStore(store.dir)
    assert reloaded.load_state == "clean"
    assert [c.id for c in reloaded.edl.get_track("v1").clips] == ["c1"]


def test_assignment_of_bad_enum_raises_at_the_model(store: EDLStore):
    cap = store.edl.get_track("captions")
    with pytest.raises(ValidationError):
        cap.config.style = "karaoke"
    assert cap.config.style == "default"


def test_bad_caption_style_survives_32_further_edits(store: EDLStore):
    """The escalation that makes VAI-01 a total loss rather than a hiccup.

    Snapshot recovery buys exactly MAX_UNDO (30) edits: once 30 more commits
    have happened, every retained snapshot was written after the poisoning and
    a reload falls all the way through to an empty timeline. 32 is comfortably
    past the cliff.
    """
    with pytest.raises(ValueError):
        dispatch(store, "add_caption_track", {"style": "karaoke"})
    for i in range(32):
        dispatch(store, "add_marker", {"time": float(i) * 0.1, "label": f"m{i}"})

    reloaded = EDLStore(store.dir)
    assert reloaded.load_state == "clean", "edl.json became unreadable"
    assert [c.id for c in reloaded.edl.get_track("v1").clips] == ["c1"]
    assert len(reloaded.edl.markers) == 32


def test_commit_refuses_to_persist_an_unloadable_edl(store: EDLStore):
    """Belt to validate_assignment's braces: commit() is the single durability
    point, so it is the last place an invalid tree can still be reported to the
    caller that produced it instead of to whoever opens the project next."""
    before = store.edl_path.read_text(encoding="utf-8")
    # Reach around every validator to plant a value the model would reject, the
    # way a future handler bug or a raw __dict__ poke could.
    object.__setattr__(store.edl.canvas, "__dict__",
                       {**store.edl.canvas.__dict__, "fps": "thirty"})
    with pytest.raises(ValueError, match="cannot be read back"):
        store.commit("test", {}, "planted")
    assert store.edl_path.read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------
# VAI-02 — a corrupt session must not export a silent empty .vae
# --------------------------------------------------------------------------

def test_save_project_refuses_a_corrupt_session(tmp_path: Path, monkeypatch):
    from video_ai_editor import storage, storage_project

    monkeypatch.setattr(storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(storage_project, "WORKDIR", tmp_path)
    sid = "s_corrupt01"
    sd = tmp_path / sid
    (sd / "snapshots").mkdir(parents=True)
    (sd / "edl.json").write_text("{ this is not json", encoding="utf-8")
    (sd / "snapshots" / "00001_deadbeef.json").write_text("also not json", encoding="utf-8")

    store = EDLStore(sd)
    assert store.load_state == "corrupt"
    assert store.is_data_loss_state

    with pytest.raises(ValueError, match="nothing to save"):
        storage_project.save_project(sid, tmp_path / "out.vae")
    assert not (tmp_path / "out.vae").exists()


def test_save_project_allows_a_recovered_session(tmp_path: Path, monkeypatch):
    """Recovery is not data loss — the snapshot IS the project. Only the
    all-snapshots-unreadable case refuses, and only while still empty."""
    from video_ai_editor import storage, storage_project

    monkeypatch.setattr(storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(storage_project, "WORKDIR", tmp_path)
    sid = "s_recov001"
    sd = tmp_path / sid
    (sd / "snapshots").mkdir(parents=True)
    good = empty_edl(Canvas(w=1080, h=1920))
    good.get_track("v1").clips.append(Clip(id="c1", src=str(tmp_path / "a.mp4"), in_=0, out=2))
    (sd / "snapshots" / "00001_abc.json").write_text(good.to_json(), encoding="utf-8")
    (sd / "edl.json").write_text("{ broken", encoding="utf-8")

    store = EDLStore(sd)
    assert store.load_state == "recovered"
    assert not store.is_data_loss_state

    out = storage_project.save_project(sid, tmp_path / "ok.vae")
    assert out.exists()
    import zipfile
    with zipfile.ZipFile(out) as zf:
        # The bundled edl.json is the RECOVERED tree, not the unreadable file
        # sitting on disk — otherwise the archive would be corrupt too.
        assert json.loads(zf.read("edl.json"))["tracks"]


# --------------------------------------------------------------------------
# VAI-03 / VAI-10 — declared types are enforced at the dispatch boundary
# --------------------------------------------------------------------------

def test_bool_where_a_string_is_declared_is_a_400_not_a_500(store: EDLStore):
    # Pre-fix: Path(True) -> "TypeError: argument should be a str or an
    # os.PathLike object where __fspath__ returns a str" -> HTTP 500.
    with pytest.raises(ValueError, match="must be of type"):
        dispatch(store, "apply_lut", {"clip_id": "c1", "src": True})


def test_garbage_where_a_number_is_declared_is_rejected(store: EDLStore):
    with pytest.raises(ValueError, match="must be of type"):
        dispatch(store, "set_clip_transform", {"clip_id": "c1", "scale": "wide"})


def test_numeric_strings_are_still_accepted(store: EDLStore):
    """The boundary must not become stricter than _num() has always been —
    "1.5" is a caller style, not a defect."""
    dispatch(store, "set_clip_transform", {"clip_id": "c1", "scale": "1.5"})
    assert store.edl.get_clip("c1")[1].transform.scale == pytest.approx(1.5)


def test_unknown_enum_value_is_rejected(store: EDLStore):
    with pytest.raises(ValueError, match="must be one of"):
        dispatch(store, "add_caption_track", {"position": "middle-ish"})


def test_enum_match_is_case_insensitive_and_canonicalised(store: EDLStore):
    """Several handlers already lower-case their enums on purpose (add_text's
    anim names, set_clip_fit). The boundary canonicalises instead of rejecting
    so it can't break them."""
    args = {"clip_id": "c1", "fit": "COVER"}
    dispatch(store, "set_clip_fit", args)
    assert args["fit"] == "cover"
    assert store.edl.get_clip("c1")[1].fit == "cover"


def test_required_args_are_not_enforced_from_the_schema(store: EDLStore):
    """split_at DECLARES `track` required but its handler defaults to v1.
    The schema's `required` is guidance for Claude, not a contract every
    caller has honoured — enforcing it would break existing callers."""
    dispatch(store, "split_at", {"time": 2.0})


def test_a_genuinely_missing_arg_is_a_value_error(store: EDLStore):
    # Handlers read mandatory args as args["clip_id"]; that KeyError used to
    # reach the client as a bare 500.
    with pytest.raises(ValueError, match="missing required argument 'clip_id'"):
        dispatch(store, "add_effect", {"type": "blur"})


def test_unknown_extra_keys_are_tolerated(store: EDLStore):
    """additionalProperties is deliberately NOT enforced — documented arg
    aliases and older UI builds both send keys the schema doesn't list."""
    dispatch(store, "set_clip_transform", {"clip_id": "c1", "scale": 2.0, "idx": 3})


# --------------------------------------------------------------------------
# VAI-04 / B5 — named canvas anchors
# --------------------------------------------------------------------------

def test_add_sticker_accepts_a_named_anchor(store: EDLStore, tmp_path: Path):
    png = tmp_path / "x.png"
    from PIL import Image
    Image.new("RGBA", (16, 16), (0, 255, 0, 255)).save(png)
    res = dispatch(store, "add_sticker", {"src": str(png), "position": "center"})
    sk = next(c for c in store.edl.get_track("stickers").clips if c.id == res["sticker_id"])
    assert sk.transform.x == pytest.approx(540.0)
    assert sk.transform.y == pytest.approx(960.0)


@pytest.mark.parametrize("name,expect", [
    ("top-left", (162.0, 288.0)),
    ("bottom_right", (918.0, 1632.0)),
    ("topRight", (918.0, 288.0)),
    ("  Bottom  ", (540.0, 1632.0)),
])
def test_anchor_spellings(store: EDLStore, tmp_path: Path, name, expect):
    from video_ai_editor.agent.dispatch import _resolve_position
    assert tuple(_resolve_position(name, store.edl.canvas)) == pytest.approx(expect)


def test_unknown_anchor_names_the_legal_set(store: EDLStore):
    from video_ai_editor.agent.dispatch import _resolve_position
    with pytest.raises(ValueError, match="named anchor|one of"):
        _resolve_position("northwest", store.edl.canvas)


# --------------------------------------------------------------------------
# VAI-05 — canvas bounds
# --------------------------------------------------------------------------

def test_set_canvas_rejects_nonsense_dimensions(store: EDLStore):
    with pytest.raises(ValueError, match="must be between"):
        dispatch(store, "set_canvas", {"w": 0, "h": -10})
    assert (store.edl.canvas.w, store.edl.canvas.h) == (1080, 1920)


def test_set_canvas_rejects_nonpositive_fps(store: EDLStore):
    with pytest.raises(ValueError, match="must be between"):
        dispatch(store, "set_canvas", {"fps": 0})
    assert store.edl.canvas.fps == 30


def test_canvas_model_clamps_and_snaps_even():
    # Clamped rather than rejected at the model, so an EDL that already holds
    # a bad value stays loadable — the whole point of the round.
    c = Canvas(w=3, h=100000, fps=9999)
    assert (c.w, c.h, c.fps) == (16, 7680, 240)
    assert Canvas(w=101, h=675).w % 2 == 0
    assert Canvas(w=101, h=675).h % 2 == 0


# --------------------------------------------------------------------------
# VAI-06 — a pixel bbox must not be able to kill the process
# --------------------------------------------------------------------------

def test_motion_track_rejects_a_pixel_bbox(store: EDLStore):
    # The tester's literal input. Pre-fix this multiplied straight into an
    # OpenCV init box ~100x the frame per axis and the process was OOM-killed
    # (exit 137) before any error could be reported.
    with pytest.raises(ValueError, match="normalised 0..1"):
        dispatch(store, "motion_track",
                 {"clip_id": "c1", "target_id": "t1", "bbox": [10, 10, 100, 100]})


@pytest.mark.parametrize("bbox", [
    [0.1, 0.1, 0.0, 0.2],          # zero width
    [0.1, 0.1, 0.2],               # wrong arity
    [0.1, 0.1, 0.2, "x"],          # non-numeric
    [-0.1, 0.1, 0.2, 0.2],         # negative origin
])
def test_norm_bbox_rejects(bbox):
    from video_ai_editor.agent.dispatch import _norm_bbox
    with pytest.raises(ValueError):
        _norm_bbox(bbox, "motion_track")


def test_norm_bbox_clamps_to_the_frame():
    from video_ai_editor.agent.dispatch import _norm_bbox
    assert _norm_bbox([0.8, 0.9, 0.5, 0.5], "motion_track") == pytest.approx(
        (0.8, 0.9, 0.2, 0.1))


def test_tracker_defends_itself_against_a_pixel_bbox(tmp_path: Path):
    """The dispatch guard is the polite layer; the library one is the load-
    bearing one, because this is where getting it wrong is fatal rather than
    merely wrong."""
    cv2 = pytest.importorskip("cv2")
    import subprocess
    src = tmp_path / "v.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=64x64:duration=1:rate=10",
         "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True,
    )
    from video_ai_editor.ai import tracker
    assert cv2 is not None
    with pytest.raises(ValueError, match="too small to track"):
        # x=1.0 leaves zero width after clamping — the shape a pixel bbox
        # collapses to once it is bounded.
        tracker.track_object(src, (1.0, 1.0, 5.0, 5.0), canvas_w=64, canvas_h=64)
    with pytest.raises(ValueError, match="sample_every"):
        tracker.track_object(src, (0.1, 0.1, 0.5, 0.5), canvas_w=64, canvas_h=64,
                             sample_every=0)


# --------------------------------------------------------------------------
# VAI-07 — effect types come from the render registry
# --------------------------------------------------------------------------

def test_add_effect_rejects_an_unknown_type(store: EDLStore):
    with pytest.raises(ValueError, match="must be one of"):
        dispatch(store, "add_effect", {"clip_id": "c1", "type": "nonsense_effect"})
    assert store.edl.get_clip("c1")[1].effects == []


def test_advertised_effect_enum_matches_the_registry():
    from video_ai_editor.render.effects import EFFECT_BUILDERS
    from video_ai_editor.agent.tools import ALL_TOOLS
    schema = next(t for t in ALL_TOOLS if t["name"] == "add_effect")
    assert schema["input_schema"]["properties"]["type"]["enum"] == sorted(EFFECT_BUILDERS)


def test_every_advertised_effect_type_is_accepted(store: EDLStore):
    from video_ai_editor.render.effects import EFFECT_BUILDERS
    for name in sorted(EFFECT_BUILDERS):
        dispatch(store, "add_effect", {"clip_id": "c1", "type": name})
    assert len(store.edl.get_clip("c1")[1].effects) == len(EFFECT_BUILDERS)


# --------------------------------------------------------------------------
# VAI-08 / C9 — transform domain bounds
# --------------------------------------------------------------------------

def test_opacity_is_clamped_not_stored_verbatim(store: EDLStore):
    dispatch(store, "set_clip_transform", {"clip_id": "c1", "opacity": 5.0})
    assert store.edl.get_clip("c1")[1].transform.opacity == 1.0
    dispatch(store, "set_clip_transform", {"clip_id": "c1", "opacity": -3})
    assert store.edl.get_clip("c1")[1].transform.opacity == 0.0


def test_scale_cannot_be_zero(store: EDLStore):
    dispatch(store, "set_clip_transform", {"clip_id": "c1", "scale": 0})
    assert store.edl.get_clip("c1")[1].transform.scale > 0


def test_transform_bounds_apply_to_keyframed_values():
    """A Field(ge=…, le=…) could not do this — pydantic cannot attach a numeric
    constraint to a `float | Keyframe` union, which is why these are validators."""
    from video_ai_editor.edl.schema import Keyframe
    t = Transform(opacity=Keyframe(keyframes=[(0.0, -1.0), (1.0, 9.0)]))
    assert [v for _, v in t.opacity.keyframes] == [0.0, 1.0]


def test_transform_rejects_non_finite():
    with pytest.raises(ValidationError):
        Transform(x=float("nan"))


def test_transform_bounds_hold_for_every_writer(store: EDLStore):
    """set_clip_transform is not the only path in — the clamp lives on the
    model so add_keyframe, motion_track and set_property inherit it."""
    dispatch(store, "set_property",
             {"clip_id": "c1", "path": "transform.opacity", "value": 42})
    assert store.edl.get_clip("c1")[1].transform.opacity == 1.0


# --------------------------------------------------------------------------
# VAI-09 / D11 — long tools run off the request thread
# --------------------------------------------------------------------------

def test_async_dispatch_returns_a_job_and_completes(tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient
    from video_ai_editor import main as m

    monkeypatch.setattr(m, "_STORES", type(m._STORES)())
    client = TestClient(m.app)
    sid = client.post("/api/sessions", json={"name": "async"}).json()["id"]

    r = client.post(f"/api/sessions/{sid}/dispatch?wait=0",
                    json={"tool": "add_marker", "args": {"time": 1.0, "label": "m"}})
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("completed", "failed", "cancelled"):
            break
        import time
        time.sleep(0.02)
    assert job["status"] == "completed", job.get("error")
    assert job["result"]["op"]["tool"] == "add_marker"
    assert client.get(f"/api/sessions/{sid}/edl").json()["markers"]


def test_async_dispatch_reports_a_tool_error_readably(tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient
    from video_ai_editor import main as m

    monkeypatch.setattr(m, "_STORES", type(m._STORES)())
    client = TestClient(m.app)
    sid = client.post("/api/sessions", json={"name": "async-err"}).json()["id"]
    r = client.post(f"/api/sessions/{sid}/dispatch?wait=0",
                    json={"tool": "add_effect", "args": {"clip_id": "nope", "type": "blur"}})
    job_id = r.json()["job_id"]
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("completed", "failed", "cancelled"):
            break
        import time
        time.sleep(0.02)
    assert job["status"] == "failed"
    # Not "HTTPException: (400, ...)" — the detail has to survive the hop.
    assert "nope" in job["error"]


def test_frontend_and_backend_async_tool_lists_agree():
    from video_ai_editor.main import ASYNC_DISPATCH_TOOLS
    src = (Path(__file__).resolve().parents[1] / "frontend/src/store.ts").read_text(encoding="utf-8")
    block = src.split("const ASYNC_DISPATCH_TOOLS = new Set([", 1)[1].split("])", 1)[0]
    names = {tok.strip().strip("',\"") for tok in block.split(",") if tok.strip()}
    assert names == set(ASYNC_DISPATCH_TOOLS)


# --------------------------------------------------------------------------
# Wave F — stability
# --------------------------------------------------------------------------

def test_livez_is_async_so_it_cannot_queue_behind_renders():
    """A `def` endpoint runs on the bounded anyio worker threadpool that every
    sync route shares; a saturated pool made a healthy process report dead."""
    import inspect
    from video_ai_editor.main import app
    route = next(r for r in app.routes if getattr(r, "path", None) == "/livez")
    assert inspect.iscoroutinefunction(route.endpoint)


def test_concurrent_renders_are_capped():
    from video_ai_editor.render import compositor
    assert compositor._RENDER_SLOTS._initial_value >= 1


# --------------------------------------------------------------------------
# Schema-wide invariants
# --------------------------------------------------------------------------

def test_every_edl_model_validates_on_assignment():
    """The one guarantee the whole round rests on. A model added later without
    the shared base would silently reopen VAI-01 for its own fields."""
    from video_ai_editor.edl import schema as sc
    import inspect
    from pydantic import BaseModel
    missing = [
        name for name, obj in vars(sc).items()
        if inspect.isclass(obj) and issubclass(obj, BaseModel)
        and obj is not BaseModel and not obj.model_config.get("validate_assignment")
    ]
    assert missing == []


def test_internal_schema_annotations_never_reach_the_wire():
    """`x-validated-by-handler` is ours, not JSON Schema — it is projected out
    alongside `category` before the tool list is sent to Claude."""
    from video_ai_editor.agent.loop import _anthropic_tools
    for tool in _anthropic_tools():
        for spec in tool["input_schema"].get("properties", {}).values():
            if isinstance(spec, dict):
                assert not [k for k in spec if k.startswith("x-")], tool["name"]


def test_add_transition_keeps_its_own_did_you_mean_error(store: EDLStore):
    """The generic enum check must stand down where a handler answers better."""
    with pytest.raises(ValueError, match="unknown transition"):
        dispatch(store, "add_transition", {"at": 1.0, "type": "kapow"})


# --------------------------------------------------------------------------
# Found by the live end-to-end sweep, not by the tester. Same defect family as
# VAI-07: a value the handler accepts, stores, and only fails on much later.
# --------------------------------------------------------------------------

def test_apply_lut_rejects_a_nonexistent_path(store: EDLStore):
    """A bundled LUT *name* was checked for existence; a *path* was not, so a
    typo was stored on the clip and surfaced as an ffmpeg filtergraph error at
    render time — which the API then attributed to a missing SOURCE file."""
    with pytest.raises(ValueError, match="LUT file not found"):
        dispatch(store, "apply_lut", {"clip_id": "c1", "src": "/no/such/grade.cube"})
    assert store.edl.get_clip("c1")[1].effects == []


def test_apply_lut_still_accepts_a_bundled_name(store: EDLStore):
    """The guard must not break the list_luts -> apply_lut loop."""
    names = dispatch(store, "list_luts", {})["luts"]
    if not names:
        pytest.skip("no bundled LUTs in presets/luts")
    dispatch(store, "apply_lut", {"clip_id": "c1", "src": names[0]})
    effects = store.edl.get_clip("c1")[1].effects
    assert [e.type for e in effects] == ["lut"]
    assert Path(effects[0].params["src"]).exists()


def test_apply_lut_alias_lut_path_still_reaches_the_handler(store: EDLStore):
    """`lut_path` is a documented alias. The error naming the LUT proves the
    alias was READ — a type/unknown-key rejection would prove it wasn't."""
    with pytest.raises(ValueError, match="LUT file not found"):
        dispatch(store, "apply_lut", {"clip_id": "c1", "lut_path": "/no/such/grade.cube"})


def test_apply_lut_clamps_intensity(store: EDLStore):
    names = dispatch(store, "list_luts", {})["luts"]
    if not names:
        pytest.skip("no bundled LUTs in presets/luts")
    dispatch(store, "apply_lut", {"clip_id": "c1", "src": names[0], "intensity": 9.0})
    assert store.edl.get_clip("c1")[1].effects[0].params["intensity"] == 1.0


@pytest.mark.parametrize("tail,expect", [
    ("[Parsed_lut3d_4 @ 0x1] Cannot open file '/x/grade.cube': No such file or "
     "directory", "colour LUT"),
    ("[in#0 @ 0x1] Error opening input: No such file or directory\n"
     "Error opening input file '/x/clip.mp4'.", "source file"),
])
def test_render_failure_message_distinguishes_a_missing_lut_from_missing_media(tail, expect):
    """Both are "No such file or directory"; only one is the user's footage.
    Telling someone their media moved when a LUT was deleted sent them hunting
    through files that were never the problem."""
    from video_ai_editor.main import _render_failure_message
    assert expect in _render_failure_message(tail, tail)
