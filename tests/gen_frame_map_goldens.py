"""Regenerate ``tests/goldens/frame_map/*.json`` through the real compositor.

    .venv/bin/python tests/gen_frame_map_goldens.py [--only NAME_SUBSTR] [--groups rates,speed] [--jobs 4]
        [--work DIR] [--check]

Every case is rendered twice (``render_preview`` and the export's single
pass), both renders are decoded frame by frame, and they must agree with each
other. The decoded frames are the golden; ``render/frame_map.py`` is compared
against them and every disagreement is printed (``--check`` exits non-zero on
any). Nothing here trusts the model: a golden records what ffmpeg did.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import frame_map_golden_lib as lib  # noqa: E402
from video_ai_editor.edl.schema import Clip, EDL  # noqa: E402
from video_ai_editor.render import compositor, render_preview  # noqa: E402
from video_ai_editor.render import transitions as _transitions  # noqa: E402
from video_ai_editor.render.frame_map import FRAME_MAP_VERSION  # noqa: E402


@contextlib.contextmanager
def split_screen_transitions():
    """Every transition renders as the split-screen probe expression (the
    frame SELECTION of xfade is independent of the look)."""
    orig_resolve, orig_post = _transitions.resolve_transition, _transitions.post_filter
    _transitions.resolve_transition = lambda _name: ("custom", lib.SPLIT_EXPR)
    _transitions.post_filter = lambda *a, **k: ""
    try:
        yield
    finally:
        _transitions.resolve_transition, _transitions.post_filter = orig_resolve, orig_post


def with_paths(edl: EDL, paths: dict[str, str]) -> EDL:
    out = edl.model_copy(deep=True)
    for t in out.tracks:
        for c in t.clips:
            if isinstance(c, Clip) and c.src in paths:
                c.src = paths[c.src]
    return out


def ensure_sources(work: Path) -> tuple[dict[str, str], dict, dict[str, int]]:
    src_dir = work / "sources"
    src_dir.mkdir(parents=True, exist_ok=True)
    paths, infos, sids = {}, {}, {}
    for spec in lib.source_specs():
        p = src_dir / f"{spec.key}.mp4"
        if not p.exists():
            lib.make_bar_source(p, spec)
        paths[spec.key] = str(p)
        infos[spec.key] = lib.probe_source(p)
        sids[spec.key] = spec.sid
        assert infos[spec.key].frames == spec.frames, (spec.key, infos[spec.key].frames)
        if spec.key.startswith("bar") and spec.timescale is None:
            # The speed group picks freeze in-points from the CFR info the
            # source is expected to probe as (lib._spec_info).
            assert infos[spec.key].to_json() == lib._spec_info(spec.key).to_json(), spec.key
    return paths, infos, sids


def render_and_measure(case: lib.CaseSpec, paths: dict[str, str], work: Path) -> dict:
    edl = lib.build_edl(case)
    real = with_paths(edl, paths)
    sess = work / "sessions" / case.name
    if sess.exists():
        shutil.rmtree(sess)
    sess.mkdir(parents=True)
    t0 = time.time()
    out: dict = {"edl": edl, "errors": []}
    try:
        out["preview"] = lib.measure(render_preview(real, sess).path)
    except Exception as e:  # noqa: BLE001 — a failing render is a finding, not a crash
        out["errors"].append(f"preview render failed: {str(e)[-600:]}")
    try:
        exp = compositor._render(real, sess / "export.mp4", height=lib.H, fps=real.canvas.fps,
                                 preview=False, cache_dir=sess / "cache", chunked=False)
        out["export"] = lib.measure(exp)
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"export render failed: {str(e)[-600:]}")
    out["seconds"] = time.time() - t0
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--work", default="")
    ap.add_argument("--check", action="store_true", help="exit 1 on any model mismatch")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--rederive", action="store_true",
                    help="keep the measured frames, recompute only the stored model JSON")
    ap.add_argument("--groups", default="",
                    help="comma-separated groups to render AND write (the others' files are "
                         "left byte-identical); default: every group")
    args = ap.parse_args()
    if args.rederive:
        return rederive()
    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="fmgold_"))
    work.mkdir(parents=True, exist_ok=True)
    paths, infos, sids = ensure_sources(work)
    groups_only = {g for g in args.groups.split(",") if g}
    cases = [c for c in lib.all_cases() if args.only in c.name
             and (not groups_only or c.group in groups_only)]
    print(f"{len(cases)} cases, work dir {work}", flush=True)

    results: dict[str, dict] = {}
    with split_screen_transitions(), ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futs = {ex.submit(render_and_measure, c, paths, work): c for c in cases}
        for fut, c in futs.items():
            results[c.name] = fut.result()

    bad = 0
    groups: dict[str, list] = {}
    for c in cases:
        r = results[c.name]
        edl = r["edl"]
        if r["errors"]:
            print(f"{'FAILED':8} {c.name:32}", flush=True)
            for e in r["errors"]:
                print("    " + e.replace("\n", "\n    ")[-900:])
            bad += 1
            if "export" not in r:
                continue
            r.setdefault("preview", r["export"])
        exp = lib.expected_frames(edl, infos, sids)
        path_errs = lib.compare(r["preview"], r["export"], check_p=False)
        model_errs = lib.compare(r["export"], exp)
        status = "ok" if not (path_errs or model_errs) else "MISMATCH"
        print(f"{status:8} {c.name:32} frames={len(r['export']['top']):4} "
              f"{r['seconds']:.1f}s", flush=True)
        for e in path_errs[:8]:
            print(f"    preview!=export {e}")
        for e in model_errs[:12]:
            print(f"    model {e}")
        bad += bool(path_errs or model_errs)
        rec = lib.case_record(c, edl, infos, sids, r["export"])
        rec["preview_matches_export"] = not path_errs
        groups.setdefault(c.group, []).append(rec)

    if not args.no_write and not args.only:
        lib.GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        ver = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True).stdout.split("\n")[0]
        for g, recs in groups.items():
            doc = {"version": FRAME_MAP_VERSION, "ffmpeg": ver,
                   "generator": "tests/gen_frame_map_goldens.py", "cases": recs}
            (lib.GOLDEN_DIR / f"{g}.json").write_text(
                json.dumps(doc, separators=(",", ":"), sort_keys=True) + "\n")
        print(f"wrote {len(groups)} golden files to {lib.GOLDEN_DIR}")
    print(f"{bad} case(s) with mismatches")
    return 1 if (bad and args.check) else 0


def rederive() -> int:
    """Recompute every golden's model JSON from its stored EDL (after a
    model-side change that does not touch frame selection, e.g. the sound
    placement fields). The measured frames are kept and re-checked."""
    bad = 0
    for p in sorted(lib.GOLDEN_DIR.glob("*.json")):
        doc = json.loads(p.read_text())
        for rec in doc["cases"]:
            edl = EDL.model_validate(rec["edl"])
            infos = {k: lib.SourceInfo.from_json(v) for k, v in rec["sources"].items()}
            sids = {k: v["sid"] for k, v in rec["sources"].items()}
            errs = lib.compare(lib.expand_measured(rec["measured"]), lib.expected_frames(edl, infos, sids))
            if errs:
                bad += 1
                print(f"MISMATCH {rec['name']}: {errs[:3]}")
            rec["model"] = lib.model_json(edl, infos)
        p.write_text(json.dumps(doc, separators=(",", ":"), sort_keys=True) + "\n")
    print(f"re-derived; {bad} case(s) with mismatches")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
