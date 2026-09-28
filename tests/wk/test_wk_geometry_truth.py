"""Engine vs export geometry parity for anamorphic sources and the keyframe
matrix, in real WKWebView (Wave D3, lane E1a; spec §3.4, §8).

The page (the product's PreviewEngine over REAL proxies built by the ingest
ProxyManager) draws chosen output frames; the export renders the same EDL;
Y-PSNR per feature must reach EXACT (≥ 35 dB).

* anamorphic masters (PAL 4:3 and 16:9 at 720x576, HDV 1440x1080): the proxy
  is square-pixel at the DISPLAYED width and the export now fits the same
  shape (render/sar.py) — before, the export drew them squeezed;
* every clock (1x, 0.5x, 2x, a curve, reverse, reverse 2x, a freeze) with all
  five properties keyed, clips at 0, 2 and 30 s: the engine keys at
  `playhead - clip.start` and so does the export now.

The masters hold ONE still picture (a frame of real footage), so which source
frame a clock selects cannot move the score: this suite measures geometry
and the keyframe clock only (frame selection has its own goldens).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from .conftest import PAGES
from .engine_server import EngineServer
from .harness import WKHarness
from .test_wk_phase1_video import (BENCH, CANVAS, ENTRY, EXACT_DB, FPS, _decode_y, _esbuild, _load,
                                   geometry_psnr, run)

pytestmark = pytest.mark.wk

INTERPS = ("linear", "ease-in", "ease-out", "ease-in-out", "step", "back-out", "bounce")
#: name → (display w x h, stored w x h, sar, rate)
MASTERS = {
    "land": ((1280, 720), (1280, 720), None, 30),
    "pal43": ((768, 576), (720, 576), "16/15", 25),
    "pal169": ((1024, 576), (720, 576), "64/45", 25),
    "hdv": ((1920, 1080), (1440, 1080), "4/3", 30),
}
CLOCKS = {
    "1x": {"in": 1.0, "out": 2.5},
    "0.5x": {"in": 1.0, "out": 2.0, "speed": 0.5},
    "2x": {"in": 1.0, "out": 4.0, "speed": 2.0},
    "curve": {"in": 1.0, "out": 2.6, "speed": {"curve": [[0, 1], [0.5, 0.5], [1, 1.5]]}},
    "reverse": {"in": 1.0, "out": 2.5, "reverse": True},
    "reverse_2x": {"in": 1.0, "out": 4.0, "speed": 2.0, "reverse": True},
    "freeze": {"in": 1.5, "out": 1.5 + 1 / 30, "freeze": 1.5},
}


def _kf(pts, interp="linear"):
    return {"keyframes": [list(p) for p in pts], "interp": interp}


def _on_frame(t: float) -> float:
    return round(t * FPS) / FPS


def _all_keys(dur: float, interp: str) -> dict:
    mid = _on_frame(0.55 * dur)
    return {"x": _kf([(0.1, -60), (mid, 40), (dur - 0.1, 80)], interp),
            "y": _kf([(0.0, 30), (_on_frame(0.4 * dur), -10), (dur, -30)], interp),
            "scale": _kf([(0.1, 1.0), (mid, 1.6), (dur - 0.1, 1.2)]),
            "rotation": _kf([(0.0, -15), (_on_frame(0.4 * dur), 20), (dur, 50)], interp),
            "opacity": _kf([(0.15, 1.0), (dur - 0.15, 0.4)], interp)}


def _groups() -> dict[str, list[tuple[str, dict]]]:
    """group → [(feature, clip fields)]; clips back to back unless `start`."""
    from video_ai_editor.edl.schema import Clip
    g: dict[str, list[tuple[str, dict]]] = {"anamorphic": [
        ("pal43_contain", {"src": "pal43"}),
        ("pal169_contain", {"src": "pal169"}),
        ("hdv_contain", {"src": "hdv"}),
        ("hdv_cover_pan", {"src": "hdv", "fit": "cover", "transform": {"x": 40.0, "y": -20.0, "scale": 1.25}}),
        ("pal43_rotate_shrink", {"src": "pal43", "transform": {"rotation": 12.0, "scale": 0.8, "x": 30.0}}),
        ("pal169_kf_half", {"src": "pal169", "in": 0.0, "out": 1.0, "speed": 0.5,
                            "transform": {"x": _kf([(0, -50), (2, 50)]), "scale": 1.4}}),
    ],
        # wave E (F4a): Transform.flip_h / flip_v — mirrored before the turn
        "flip": [
            ("flip_h_rotate", {"src": "land", "transform": {"flip_h": True, "rotation": 20.0}}),
            ("flip_v_cover_pan", {"src": "hdv", "fit": "cover",
                                  "transform": {"flip_v": True, "x": 40.0, "y": -20.0, "scale": 1.25}}),
            ("flip_hv_scale_pan", {"src": "land", "transform": {"flip_h": True, "flip_v": True, "scale": 0.8,
                                                                "x": 30.0, "y": -12.0}}),
            ("flip_h_kf", {"src": "land", "in": 0.0, "out": 1.0,
                           "transform": {"flip_h": True, "x": _kf([(0, -50), (1, 50)]),
                                         "rotation": _kf([(0, 0), (1, 30)])}}),
            ("flip_h_pal43", {"src": "pal43", "transform": {"flip_h": True, "rotation": -10.0}}),
        ]}
    for i, (clock, spec) in enumerate(CLOCKS.items()):
        probe = Clip(src="s", speed=spec.get("speed"))
        probe.in_, probe.out = spec["in"], spec["out"]
        if spec.get("freeze"):
            probe.freeze = spec["freeze"]
        dur = probe.effective_duration
        g[f"kf_{clock}"] = [(f"kf_{clock}", {"src": "land", "start": st, **spec,
                                            "transform": _all_keys(dur, INTERPS[i])}) for st in (0.0, 2.0, 30.0)]
    return g


def _edl(group: list[tuple[str, dict]], paths: dict[str, str]):
    from video_ai_editor.edl.schema import Canvas, Clip, Keyframe, empty_edl
    e = empty_edl(Canvas(w=CANVAS[0], h=CANVAS[1], fps=FPS))
    e.canvas.loudness_lufs = None
    v1 = e.get_track("v1")
    cursor = 0.0
    for i, (_feat, f) in enumerate(group):
        c = Clip(src=paths[f["src"]], id=f"c{i:02d}", start=f.get("start", cursor), speed=f.get("speed"))
        c.in_ = f.get("in", 0.0)
        c.out = f.get("out", c.in_ + 1.0)
        c.fit = f.get("fit", "contain")
        c.reverse = bool(f.get("reverse", False))
        if f.get("freeze"):
            c.freeze = f["freeze"]
        for k, v in f.get("transform", {}).items():
            setattr(c.transform, k, Keyframe(**v) if isinstance(v, dict) else v)
        v1.clips.append(c)
        cursor = c.start + c.effective_duration
    e.recompute_duration()
    return e


def _ks(edl, group) -> list[tuple[str, int]]:
    from video_ai_editor.render.compositor import clip_frames
    out = []
    for (feat, _f), c in zip(group, edl.get_track("v1").clips, strict=True):
        f0, n = round(c.start * FPS), clip_frames(c, FPS)
        for j in sorted({0, 1, 2, n // 3, n // 2, (2 * n) // 3, n - 2}):
            out.append((feat, f0 + j))
    return out


def _masters(root: Path) -> dict[str, str]:
    """One still (a frame of real footage) as every master."""
    src = BENCH / "broll_20s.mp4"
    if not src.is_file() or shutil.which("ffmpeg") is None:
        pytest.skip(f"no bench footage at {src}")
    still = root / "still.png"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", "3", "-i", str(src), "-frames:v", "1",
                    "-vf", "scale=1920:1080,setsar=1", str(still)], check=True, capture_output=True)
    paths = {}
    for name, ((dw, dh), (sw, sh), sar, rate) in MASTERS.items():
        out = root / f"{name}.mp4"
        # crop the still to the DISPLAYED aspect, then store it squeezed
        vf = (f"crop='min(iw,ih*{dw}/{dh})':'min(ih,iw*{dh}/{dw})',scale={sw}:{sh},"
              f"setsar={sar or 1},format=yuv420p")
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-loop", "1", "-framerate", str(rate),
                        "-i", str(still), "-t", "14", "-vf", vf, "-an", "-c:v", "libx264", "-crf", "14",
                        "-preset", "veryfast", str(out)], check=True, capture_output=True)
        paths[name] = str(out)
    return paths


@pytest.fixture(scope="module")
def truth_env(tmp_path_factory):
    proxy_queue = pytest.importorskip("video_ai_editor.ingest.proxy_queue")
    P = pytest.importorskip("video_ai_editor.ingest.proxy")
    from video_ai_editor import storage as _storage
    from video_ai_editor.render import compositor
    from video_ai_editor.render.frame_map import SourceInfo as FmSource

    root = tmp_path_factory.mktemp("wk-truth")
    bundle = tmp_path_factory.mktemp("wk-truth-kit")
    _esbuild(ENTRY, bundle / "wkEnginePage.js")
    paths = _masters(root)
    old = _storage.WORKDIR
    _storage.WORKDIR = root / "wd"
    _storage.WORKDIR.mkdir()
    manager = proxy_queue.ProxyManager()
    saved_enc = compositor._usable_encoder
    compositor._usable_encoder = lambda name: False     # deterministic libx264
    try:
        keys = {n: manager.ensure(p) for n, p in paths.items()}
        assert manager.wait_idle(300), "proxy builds did not finish"
        dirs = {keys[n]: P.proxy_dir(keys[n]) for n in paths}
        infos = {n: FmSource.from_proxy(P.load_source(keys[n])) for n in paths}
        sources = {paths[n]: {"key": keys[n], "info": infos[n].to_json()} for n in paths}
        groups, server_y = [], {}
        for name, group in _groups().items():
            edl = _edl(group, paths)
            sess = root / "render" / name
            sess.mkdir(parents=True)
            out = compositor._render(edl, sess / "out.mp4", height=CANVAS[1], fps=FPS, preview=False,
                                     cache_dir=sess / "cache", chunked=False, crf=12)
            ys = _decode_y(out, *CANVAS)
            ks = _ks(edl, group)
            for feat, k in ks:
                server_y[(name, k)] = (feat, ys[k])
            d = edl.model_dump(by_alias=True, mode="json")
            d["tracks"] = [t for t in d["tracks"] if t["id"] == "v1"]
            groups.append({"name": name, "edl": d, "ks": [k for _f, k in ks]})
    finally:
        compositor._usable_encoder = saved_enc
        manager.shutdown()
        manager.wait_idle(10)
        _storage.WORKDIR = old
    fx = root / "fixture"
    fx.mkdir()
    (fx / "timeline.json").write_text(json.dumps({"edl": groups[0]["edl"], "sources": sources,
                                                  "canvas": list(CANVAS), "groups": groups}))
    srv = EngineServer({"pages": PAGES, "testkit": bundle, "fixture": fx}, proxies=dirs)
    try:
        yield srv, server_y, infos
    finally:
        srv.close()


@pytest.fixture(scope="module")
def truth_parity(truth_env, tmp_path_factory):
    srv, server_y, _infos = truth_env
    harness = WKHarness(srv, tmp_path_factory.mktemp("wk-truth-runs"))
    try:
        yield geometry_psnr(run(harness, "geometry", timeout=300, mipmaps="1"), server_y)
    finally:
        harness.close()


def test_proxies_carry_the_displayed_size(truth_env):
    """The engine's SourceInfo w/h (the proxy probe) is the displayed size —
    the same integers the export now fits (render/sar.display_width)."""
    _srv, _y, infos = truth_env
    for name, ((dw, dh), _s, _sar, _r) in MASTERS.items():
        assert (infos[name].width, infos[name].height) == (dw, dh), name


def test_engine_matches_export_for_anamorphic_sources_and_every_keyframe_clock(truth_parity):
    report = {f: min(v) for f, v in truth_parity.items()}
    print(json.dumps({"geometry_truth_psnr_db": report, "load": _load()}))
    want = {f for g in _groups().values() for f, _ in g}
    assert set(report) == want
    low = {f: db for f, db in report.items() if db < EXACT_DB}
    assert not low, f"below EXACT ({EXACT_DB} dB): {low}"
