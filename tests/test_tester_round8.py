"""Round-8 tester report: "most tools aren't installed" and "the transitions
are poor / some do nothing".

Both turned out to be reporting problems as much as engineering ones. The tools
were installed (one was genuinely broken, for a reason nobody had diagnosed),
and every transition rendered — a third of them were just synonyms for each
other, so trying them one by one found repeats.
"""
from __future__ import annotations
import subprocess
import tempfile
from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import DISPATCH, dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Canvas, empty_edl
from video_ai_editor.render import transitions as T


def _store(tmp_path: Path) -> EDLStore:
    (tmp_path / "edl.json").write_text(
        empty_edl(Canvas(w=320, h=180, fps=30)).model_dump_json())
    return EDLStore(tmp_path)


def _two_clip_timeline(tmp_path: Path) -> EDLStore:
    store = _store(tmp_path)
    src = tmp_path / "v.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc2=size=320x180:rate=30:duration=8",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True)
    dispatch(store, "add_clip", {"src": str(src), "track": "v1",
                                 "in": 0.0, "out": 8.0, "start": 0.0})
    dispatch(store, "split_at", {"track": "v1", "time": 4.0})
    return store


# ------------------------------------------------------------ the catalogue

def test_every_advertised_transition_resolves_to_something_ffmpeg_accepts():
    """A name that reaches the renderer with no mapping falls back to `fade`,
    which is exactly the "I applied it and nothing happened" report."""
    valid = set(T.NATIVE.values()) | {"custom"}
    for name in T.all_names():
        xf, expr = T.resolve_transition(name)
        assert xf in valid, f"{name!r} -> {xf!r}"
        if xf == "custom":
            assert expr, f"{name!r} resolves to custom with no expr"


def test_catalog_reports_distinct_looks_separately_from_names():
    """"The platform promises 88 transitions but some are the same." True: a
    third of the names are synonyms. Both numbers are published now — quoting
    only the larger one is what made the catalogue look padded."""
    c = T.catalog()
    looks = set(T.NATIVE) | set(T.CUSTOM_EXPRS)
    assert c["looks"] == len(looks)
    assert c["count"] == len(T.all_names())
    assert c["looks"] < c["count"], "sanity: aliases exist"
    assert str(c["looks"]) in c["note"] and str(c["count"]) in c["note"]


def test_no_alias_shadows_a_real_look():
    """An alias whose name also exists as a real effect is unreachable: the
    resolver checks customs first, so the alias silently wins/loses depending
    on table order. `blinds` was exactly this once it became a real effect."""
    looks = set(T.NATIVE) | set(T.CUSTOM_EXPRS)
    clash = sorted(set(T.ALIASES) & looks)
    assert not clash, f"alias shadows a real transition: {clash}"


def test_every_alias_points_at_a_real_look():
    looks = set(T.NATIVE) | set(T.CUSTOM_EXPRS)
    for alias, target in T.ALIASES.items():
        assert target in looks, f"{alias!r} -> {target!r} which does not exist"


def test_categories_and_descriptions_stay_in_sync_with_the_catalog():
    """list_transitions is the only discovery surface; a look missing from
    every category is one the user will never find."""
    looks = set(T.NATIVE) | set(T.CUSTOM_EXPRS)
    listed = {n for v in T.CATEGORIES.values() for n in v}
    assert not (looks - listed), f"not in any category: {sorted(looks - listed)}"
    assert not (listed - set(T.all_names())), \
        f"category lists an unknown name: {sorted(listed - set(T.all_names()))}"
    assert not (set(T.DESCRIPTIONS) - set(T.all_names())), \
        "description for a transition that does not exist"


@pytest.mark.parametrize("name", sorted(set(T.NATIVE) | set(T.CUSTOM_EXPRS)))
def test_each_distinct_look_actually_renders(tmp_path, name):
    """Renders the real xfade node for every look. A bad custom expr is a
    render-time failure, not an import error — nothing else catches it."""
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    for path, src in ((a, "testsrc2"), (b, "smptebars")):
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
             "-i", f"{src}=size=128x72:rate=25:duration=1",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
            check=True, capture_output=True)
    xf_name, expr = T.resolve_transition(name)
    xf = f"xfade=transition={xf_name}:duration=0.5:offset=0.5"
    if expr:
        xf += f":expr='{expr}'"
    out = tmp_path / "f.png"
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(a), "-i", str(b),
         "-filter_complex", f"[0:v]settb=AVTB[l];[1:v]settb=AVTB[r];[l][r]{xf}[v]",
         "-map", "[v]", "-ss", "0.75", "-frames:v", "1", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, f"{name} failed: {(r.stderr or '')[-300:]}"
    assert out.exists() and out.stat().st_size > 0


# ---------------------------------------------------------------- whip pans

def test_whip_carries_a_directional_blur_scaled_to_the_render():
    """`whip` was an alias for `smoothleft` — a soft slide with no motion blur,
    which is why it never read as a whip pan. An xfade expr cannot smear
    pixels, so this needs a real filter on the xfade output.
    """
    f = T.post_filter("whip", 2.0, 2.5, 1080, 1920)
    assert f and "gblur" in f
    assert "sigmaV=0" in f, "a sideways whip must not blur vertically"
    assert "between(t,2.000,2.500)" in f, "blur must be gated to the cut"

    # Scaled to the axis it smears, not to a fixed pixel count: preview and
    # export render at different sizes, and a constant sigma would smear the
    # preview harder than the delivered file.
    small = T.post_filter("whip", 0, 1, 640, 360)
    big = T.post_filter("whip", 0, 1, 1920, 1080)
    assert small != big
    # The vertical twin scales off the height instead.
    assert "sigma=0:" in T.post_filter("whipup", 0, 1, 1080, 1920)


def test_only_whips_get_a_post_filter():
    """The post-filter node is skipped entirely for everything else — it must
    not change a single frame of the transitions that were already correct."""
    for name in ("fade", "dissolve", "slideleft", "glitch", "spiral", "burn"):
        assert T.post_filter(name, 0, 1, 1080, 1920) is None


def test_spin_is_a_rotation_not_a_clock_wipe():
    """`spin` resolved to `radial`, a clock wipe with no rotational motion —
    the name promised a movement the engine never performed."""
    xf, expr = T.resolve_transition("spin")
    assert xf == "custom" and expr
    assert "atan2" in expr, "spin should sweep by angle"
    assert T.resolve_transition("spin") == T.resolve_transition("spiral")


# ------------------------------------------------------- stacking at one cut

def test_a_second_transition_on_one_cut_replaces_the_first(tmp_path):
    """add_transition appended, so a cut could hold several while the renderer
    keys them by seam and uses one. The EDL then disagreed with the picture and
    remove_transition had to sweep up an unknown number of leftovers."""
    store = _two_clip_timeline(tmp_path)
    dispatch(store, "add_transition", {"at": 4.0, "type": "fade", "duration": 0.5})
    dispatch(store, "add_transition", {"at": 4.0, "type": "whip", "duration": 0.4})

    trs = store.edl.get_track("v1").transitions
    assert len(trs) == 1, [(t.at, t.type) for t in trs]
    assert trs[0].type == "whip" and trs[0].duration == pytest.approx(0.4)


def test_replacement_tolerates_a_click_that_is_slightly_off(tmp_path):
    """`at` comes from a click on the timeline, not exact arithmetic."""
    store = _two_clip_timeline(tmp_path)
    dispatch(store, "add_transition", {"at": 4.0, "type": "fade", "duration": 0.5})
    dispatch(store, "add_transition", {"at": 4.02, "type": "burn", "duration": 0.5})
    assert len(store.edl.get_track("v1").transitions) == 1


def test_a_different_cut_keeps_its_own_transition(tmp_path):
    store = _two_clip_timeline(tmp_path)
    dispatch(store, "split_at", {"track": "v1", "time": 6.0})
    dispatch(store, "add_transition", {"at": 4.0, "type": "fade", "duration": 0.4})
    dispatch(store, "add_transition", {"at": 6.0, "type": "spiral", "duration": 0.4})
    assert len(store.edl.get_track("v1").transitions) == 2


# --------------------------------------------------------- feature reporting

def test_check_features_is_registered_and_read_only(tmp_path):
    """The agent told a user to `uv add noisereduce soundfile` for a working
    noisereduce, and called vocal isolation "not installed" when the real fault
    was a torchcodec DLL. It had no way to look — now it does."""
    store = _store(tmp_path)
    assert "check_features" in DISPATCH
    before = len(store.ops.ops)
    r = dispatch(store, "check_features", {})
    assert len(store.ops.ops) == before, "a status probe must not commit"
    assert r["summary"] and isinstance(r["available"], list)
    for entry in r["unavailable"]:
        assert entry["fix"], f"{entry['key']} reports no way to fix it"


def test_every_feature_names_tools_that_exist():
    """A report pointing at a tool that isn't registered would send the agent
    to call something that 400s."""
    from video_ai_editor.ai.features import FEATURES
    for f in FEATURES:
        assert f.tools, f"{f.key} lists no tools"
        for t in f.tools:
            assert t in DISPATCH, f"{f.key} names unknown tool {t!r}"


def test_feature_probes_never_raise():
    """A probe is called to DIAGNOSE a broken install; one that throws on a
    machine missing a dependency would take the diagnosis down with it."""
    from video_ai_editor.ai.features import FEATURES, feature_report
    for f in FEATURES:
        f.probe()                       # must not raise even when absent
    r = feature_report()
    assert len(r["available"]) + len(r["unavailable"]) == len(FEATURES)


def test_stem_separation_probe_covers_what_it_actually_uses():
    """separate.py decodes audio itself now, so a probe that only saw `demucs`
    would report the feature ready and then fail on soundfile/torch."""
    import inspect
    from video_ai_editor.ai import separate
    src = inspect.getsource(separate.available)
    for mod in ("demucs", "torch", "soundfile"):
        assert mod in src, f"availability probe ignores {mod}"


def test_stem_separation_does_not_shell_out_to_the_demucs_cli():
    """The CLI loads audio through torchaudio → torchcodec, whose DLLs cannot
    load on a standard Windows ffmpeg install (it needs ffmpeg's SHARED
    libraries; Windows users have a static ffmpeg.exe). That made demucs exit 1
    and got reported as "vocal isolation is not installed"."""
    import ast
    import inspect
    from video_ai_editor.ai import separate

    fn = ast.parse(inspect.getsource(separate._demucs_separate)).body[0]
    # Strip the docstring: it EXPLAINS the CLI we no longer use, so a plain
    # substring search over the source matches the comment and fails on prose.
    body = fn.body[1:] if (isinstance(fn.body[0], ast.Expr)
                           and isinstance(fn.body[0].value, ast.Constant)) else fn.body
    code = "\n".join(ast.dump(n) for n in body)
    assert "demucs.separate" not in code, "back on the torchcodec-dependent CLI"
    assert "subprocess" not in code, "stem separation must not shell out"
    assert "apply_model" in code


# ------------------------------------------------- why the agent "got confused"

def test_a_large_tool_result_stays_valid_json():
    """The tool result was `json.dumps(result)[:8000]` — a slice mid-structure.
    A 40-clip timeline is ~11.4k chars, so the model received a document that
    simply stopped: an unclosed string, half a key. Asked to reason over that,
    it reports being confused or invents the missing half.
    """
    import json
    from video_ai_editor.agent.loop import TOOL_RESULT_LIMIT, _tool_result_json

    big = {"clips": [{"id": f"c_{i}", "src": "x" * 120} for i in range(500)]}
    assert len(json.dumps(big)) > TOOL_RESULT_LIMIT

    sent = _tool_result_json(big)
    parsed = json.loads(sent)                 # must not raise
    assert parsed["_truncated"] is True
    assert parsed["original_chars"] > TOOL_RESULT_LIMIT
    assert len(sent) <= TOOL_RESULT_LIMIT, "the cap has to actually hold"


def test_a_small_tool_result_is_passed_through_untouched():
    import json
    from video_ai_editor.agent.loop import _tool_result_json
    r = {"summary": "did the thing", "clip_id": "c_1"}
    assert json.loads(_tool_result_json(r)) == r


def test_get_timeline_summary_stays_inside_the_tool_result_budget(tmp_path):
    """The per-clip summary grew unbounded, so an ordinary edit (one
    remove_silences pass) pushed it past the limit and it arrived truncated."""
    import json
    from video_ai_editor.agent.dispatch import MAX_SUMMARY_CLIPS
    from video_ai_editor.agent.loop import TOOL_RESULT_LIMIT

    store = _store(tmp_path)
    src = tmp_path / "v.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc2=size=160x90:rate=15:duration=60",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True)
    dispatch(store, "add_clip", {"src": str(src), "track": "v1",
                                 "in": 0.0, "out": 60.0, "start": 0.0})
    for t in range(1, 50):
        dispatch(store, "split_at", {"track": "v1", "time": float(t)})

    r = dispatch(store, "get_timeline", {})
    v1 = next(t for t in r["tracks"] if t["id"] == "v1")
    assert v1["clip_count"] == 50, "the true count must survive the cap"
    assert len(v1["clips"]) <= MAX_SUMMARY_CLIPS + 1     # +1 for the marker
    assert any("_omitted" in c for c in v1["clips"]), "elision must be visible"
    # First and last clips are kept: that is what positional reasoning needs.
    assert v1["clips"][0]["start"] == pytest.approx(0.0)
    assert len(json.dumps(r, default=str)) < TOOL_RESULT_LIMIT


def test_short_timelines_are_not_elided(tmp_path):
    """The cap must be invisible for the projects people actually have open."""
    store = _store(tmp_path)
    src = tmp_path / "v.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc2=size=160x90:rate=15:duration=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True)
    dispatch(store, "add_clip", {"src": str(src), "track": "v1",
                                 "in": 0.0, "out": 10.0, "start": 0.0})
    for t in (2.0, 4.0, 6.0):
        dispatch(store, "split_at", {"track": "v1", "time": t})
    v1 = next(t for t in dispatch(store, "get_timeline", {})["tracks"]
              if t["id"] == "v1")
    assert len(v1["clips"]) == 4
    assert not any("_omitted" in c for c in v1["clips"])


def test_packaged_app_never_advises_an_install_it_cannot_honour(monkeypatch):
    """Found by running the real Windows .exe, not by pytest.

    The packaged build EXCLUDES the heavy ML libraries on purpose, so 8 of 13
    features report unavailable there — the common case in a shipped install.
    Every one of them was answering "run `uv sync --all-extras`", which cannot
    install anything into a frozen bundle. The user runs it, nothing changes,
    and the tool that told them to loses exactly the trust features.py exists
    to protect. This is the same defect the module was written to kill, just
    reached from the packaging side.
    """
    import sys as _sys

    from video_ai_editor.ai.features import FEATURES, PACKAGED_FIX, feature_report

    excluded = {f.key for f in FEATURES if not f.in_packaged_app}
    assert excluded, "nothing marked as excluded from the bundle"

    monkeypatch.setattr(_sys, "frozen", True, raising=False)
    rep = feature_report()
    assert rep["packaged_app"] is True

    by_key = {m["key"]: m for m in rep["unavailable"]}
    for key in excluded & by_key.keys():
        m = by_key[key]
        assert m["fix"] == PACKAGED_FIX, f"{key} still advises a pip install"
        assert m.get("packaged_app_excluded") is True
        assert "uv sync" not in m["fix"].split("run the app from source")[0]

    # A feature backed by an external BINARY is installable in BOTH builds —
    # dropping realesrgan/rife/ffmpeg in place works in a frozen app — so its
    # advice must survive unchanged. Blanket-substituting on `frozen` would
    # have replaced a correct, actionable fix with a dead end.
    for key in ("upscale", "interpolate", "stabilize"):
        if key in by_key:
            assert by_key[key]["fix"] != PACKAGED_FIX, (
                f"{key} is a binary drop-in and stays fixable in the packaged app")


def test_unfrozen_report_keeps_the_pip_fixes():
    """The substitution must be conditional — from source, `uv sync` IS right."""
    from video_ai_editor.ai.features import PACKAGED_FIX, feature_report
    rep = feature_report()
    assert rep["packaged_app"] is False
    for m in rep["unavailable"]:
        assert m["fix"] != PACKAGED_FIX
        assert "packaged_app_excluded" not in m


def _bundle_excludes() -> tuple[set[str], set[str]]:
    """(windows .spec excludes, macOS build_app.sh excludes).

    The two builds do NOT exclude the same set, and reading only the .spec is
    how `visual_search` ended up telling every packaged-Mac user to run
    `uv sync` inside a frozen .app — open_clip and torch are dropped by
    build_app.sh and kept by the .spec.
    """
    import ast
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = (root / "Video AI Editor.spec").read_text(encoding="utf-8")
    win: set[str] = set()
    for node in ast.walk(ast.parse(spec)):
        if isinstance(node, ast.keyword) and node.arg == "excludes":
            win = {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    mac = set(re.findall(r"--exclude-module\s+(\S+)",
                         (root / "build_app.sh").read_text(encoding="utf-8")))
    return win, mac


def test_excluded_features_match_the_pyinstaller_spec():
    """Drift guard: `in_packaged_app=False` is a claim about BOTH bundles.

    If someone stops excluding a library, the flag must follow or the app will
    tell a user a working feature is unavailable in the packaged build.
    """
    from video_ai_editor.ai.features import FEATURES

    win, mac = _bundle_excludes()
    assert win, "could not read `excludes` out of the .spec"
    assert mac, "could not read `--exclude-module` out of build_app.sh"

    # feature key -> the excluded library its probe depends on
    depends_on = {
        "noise_reduce": "noisereduce",
        "stems": "demucs",
        "bg_remove": "rembg",
        "diarize": "librosa",
        "beats": "librosa",
        "object_erase": "simple_lama_inpainting",
    }
    for key, lib in depends_on.items():
        f = next(f for f in FEATURES if f.key == key)
        assert lib in win, (
            f"{lib} is no longer excluded from the bundle — {key} should now "
            "set in_packaged_app=True")
        assert not f.in_packaged_app, f"{key} depends on excluded {lib}"


def test_the_gpu_fix_names_a_dependency_group_that_exists():
    """`check_features` forbids composing install commands by hand, so the one it
    hands out has to be real. It is a dependency GROUP rather than an extra on
    purpose — `uv sync --all-extras` is the documented dev setup, and an extra
    would silently add ~1.3GB of NVIDIA wheels to every Windows/Linux checkout,
    NVIDIA card or not. Groups are not pulled by --all-extras.
    """
    import re
    from pathlib import Path

    from video_ai_editor.ai.features import FEATURES

    f = next(f for f in FEATURES if f.key == "gpu_transcribe")
    assert "--group cuda" in f.fix, f.fix
    assert "--extra cuda" not in f.fix, (
        "an extra would be pulled by --all-extras; this must stay a group")

    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
        encoding="utf-8")
    groups = pyproject.split("[dependency-groups]", 1)
    assert len(groups) == 2, "no [dependency-groups] table"
    assert re.search(r"^cuda\s*=\s*\[", groups[1], re.M), (
        "features.py advertises `uv sync --group cuda` but no such group exists")
    # And it must NOT also be an extra, which would defeat the whole point.
    extras = pyproject.split("[project.optional-dependencies]", 1)
    if len(extras) == 2:
        before_next_table = extras[1].split("\n[", 1)[0]
        assert not re.search(r"^cuda\s*=", before_next_table, re.M)


def test_gpu_transcription_probe_needs_both_a_device_and_the_math_libraries():
    """The measured failure mode: the CUDA driver alone made ctranslate2 report a
    device, the model loaded, and the first forward pass raised
    "Library cublas64_12.dll is not found or cannot be loaded". A probe that
    checked only the device would have called that available.
    """
    import video_ai_editor.ai.features as F
    from video_ai_editor.ingest import transcribe as T

    saved_dev, saved_libs = T._resolve_device, F._cuda_math_libs_present
    try:
        for dev, libs, expected in [
            ("cuda", True, True),
            ("cuda", False, False),     # the box this was found on
            ("cpu", True, False),       # libs present, device opted out
            ("cpu", False, False),
        ]:
            T._resolve_device = lambda d=dev: d
            F._cuda_math_libs_present = lambda v=libs: v
            assert F._gpu_transcribe_ok() is expected, (dev, libs)
    finally:
        T._resolve_device, F._cuda_math_libs_present = saved_dev, saved_libs


def test_mac_only_exclusions_are_flagged_too():
    """The gap this test was blind to: a library the macOS build drops and the
    Windows build keeps. `fix` is only ever shown for a feature that probed
    UNAVAILABLE, so flagging it costs nothing on Windows (where it is bundled
    and therefore available) and is the difference between useful and
    impossible advice on a Mac.
    """
    from video_ai_editor.ai.features import FEATURES

    win, mac = _bundle_excludes()
    mac_only = mac - win
    assert {"torch", "open_clip", "faster_whisper"} <= mac_only, (
        "build_app.sh no longer drops these — re-check the feature flags")

    by_key = {f.key: f for f in FEATURES}
    # open_clip + torch: no route inside a bundle at all.
    assert not by_key["visual_search"].in_packaged_app
    # faster-whisper has one (a drop-in whisper.cpp binary), so it gets a real
    # route rather than the blanket "run from source".
    assert by_key["captions"].packaged_fix
    assert "whisper.cpp" in by_key["captions"].packaged_fix


def test_object_erase_is_gated_on_lama_not_on_cv2():
    """`object_erase` used to be grouped under the "tracking" Feature and gated
    on cv2 alone. `ai/lama.py` (the module the handler actually calls) never
    imports cv2 — its only import is `simple_lama_inpainting`, a BASE pyproject
    dependency, so the wrong gate was invisible from source (always True there)
    and reported "available" in BOTH packaged builds, where
    `simple_lama_inpainting` IS excluded (build_app.sh AND the Windows .spec).
    A packaged-app user asking to erase an object got a confident "yes" from
    check_features and then a bare RuntimeError from the real call — exactly
    the failure class this module exists to prevent, reproduced by grouping
    rather than by a missing probe.
    """
    from video_ai_editor.ai.features import FEATURES

    by_key = {f.key: f for f in FEATURES}
    assert "object_erase" in by_key, "object_erase needs its OWN Feature entry"
    obj = by_key["object_erase"]
    assert obj.tools == ["object_erase"]
    assert not obj.in_packaged_app, (
        "simple_lama_inpainting is excluded from both packaged builds")

    tracking = by_key["tracking"]
    assert "object_erase" not in tracking.tools, (
        "object_erase must not ride cv2's probe — it doesn't need cv2 at all")
    assert tracking.tools == ["motion_track", "auto_reframe"]


def test_object_erase_probe_matches_what_ai_lama_actually_needs():
    """The probe must track lama.py's real dependency, not a proxy for it."""
    from video_ai_editor.ai import features as F

    by_key = {f.key: f for f in F.FEATURES}
    probe = by_key["object_erase"].probe
    assert probe() == F._has("simple_lama_inpainting")


def test_a_packaged_fix_is_preferred_over_the_blanket_answer(monkeypatch):
    """A feature with a bundle-compatible route must not be told to give up."""
    import sys as _sys

    from video_ai_editor.ai import features as F

    monkeypatch.setattr(_sys, "frozen", True, raising=False)
    monkeypatch.setattr(F, "FEATURES", [
        F.Feature("k", "L", ["t"], lambda: False, fix="pip install x",
                  packaged_fix="drop the binary in models/"),
    ])
    entry = F.feature_report()["unavailable"][0]
    assert entry["fix"] == "drop the binary in models/"
    assert entry["packaged_app_excluded"] is True
    assert F.PACKAGED_FIX not in entry["fix"]


def test_list_transitions_is_honest_at_the_TOP_level(tmp_path):
    """`count` counts accepted NAMES; a third of them are synonyms.

    The honest split existed only inside `catalog`, but `count` sits at the top
    of the result and is the number a reader quotes — which is how "the platform
    promises N transitions" became a claim a tester could disprove by finding
    repeats. Surfacing `looks` alongside it makes the flat read stand on its own.
    """
    store = _store(tmp_path)
    r = dispatch(store, "list_transitions", {})
    assert r["looks"] == r["catalog"]["looks"]
    assert r["alias_count"] == r["catalog"]["alias_count"]
    assert r["looks"] < r["count"], "aliases must not inflate the look count"
    assert str(r["looks"]) in r["note"]
    # `count`/`transitions` keep their old meaning for existing callers.
    assert r["count"] == len(r["transitions"])
