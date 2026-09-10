"""The CapCut-parity benchmark (spec §6): every case in `prompts.CASES`
through the real `/api/sessions/{sid}/prompt` route, on a clone of a
fixture session that was uploaded and transcribed once, with zero network
egress, and measured independently of the app's own verifier.

    VAI_BRAIN=recipes uv run pytest -m benchmark tests/benchmark          # everything
    VAI_BRAIN=recipes uv run pytest -m "benchmark and not slow" tests/benchmark
    uv run pytest -m benchmark -k transitions tests/benchmark

Excluded from the default run by `addopts = "-m 'not benchmark'"`. The
report lands under `<user cache>/Video AI Editor/bench/reports/` (or
`VAI_BENCH_REPORT`); docs/BENCHMARK.md quotes it verbatim.

A case is one pytest item so `-k`, `-m slow`, `-x` and the per-case ids work;
the session-scoped `bench` fixture holds the app and the fixture sessions and
writes the report when the session ends — including the skipped cases and
the reason, because "skipped" is a fact the headline numbers must carry.

Every case also gets the harness-level assertions of §6.2's last line:
a `brain{answered}` event, the first `text_delta` starting with `via `, one
`done` per turn, no `error`, exactly one `op` for a mutating case (none for a
read-only one), only known event types, and no socket egress during the run.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from video_ai_editor.agent.prompt import service

from . import measure as M
from .harness import BenchEnv, open_bench, requirement_missing
from .measure import Assertion, check
from .prompts import CASES, Case, CaseCtx
from .report import CaseRecord, Report, build_record

pytestmark = pytest.mark.benchmark

#: Every case needs these on this machine; a missing one skips with the reason.
_COMMON_REQUIREMENTS: tuple[str, ...] = ("whisper_small", "validate_module")


@pytest.fixture(scope="session")
def bench(tmp_path_factory) -> tuple[BenchEnv, Report]:
    brain = (os.environ.get("VAI_BRAIN") or "").strip().lower() or None
    if brain == "auto":
        brain = None
    report = Report(brain=brain, expected_ids=tuple(c.id for c in CASES))
    with open_bench(tmp_path_factory.mktemp("bench_workdir"), brain=brain) as env:
        yield env, report
        report.timings = dict(env.timings)
        # A deselected case (-k, -m slow, -x) is a fact the headline must
        # carry: the report marks itself PARTIAL and leaves latest.* alone.
        js, md = report.write()
        print(f"\nbenchmark report: {md}\n{report.to_markdown()}")


def _params() -> list:
    return [pytest.param(c, id=c.slug, marks=[getattr(pytest.mark, m) for m in c.marks]) for c in CASES]


def _harness_assertions(case: Case, env: BenchEnv, run, egress_before: int) -> list[Assertion]:
    unknown = service.unknown_event_types(run.events)
    ops = len(run.ops)
    out = [check("brain_answered", run.brain is not None, run.brain, "a brain{answered} event"),
           check("first_text_via_prefix", run.first_text.startswith(service.VIA_PREFIX.split("{")[0]),
                 run.first_text[:40], "via <Brain> — …"),
           check("one_done_per_turn", all(sum(1 for e in t if e.get("type") == "done") == 1 for t in run.turns),
                 [sum(1 for e in t if e.get("type") == "done") for t in run.turns], "1 per turn"),
           check("no_error_events", not run.errors, run.errors[:2], []),
           check("op_count", ops == (1 if case.expects_op else 0), ops, 1 if case.expects_op else 0,
                 "one op per mutating prompt run; none for a read-only one"),
           check("known_event_types_only", not unknown, sorted(unknown), []),
           check("no_network_egress", len(env.egress.attempts) == egress_before,
                 env.egress.attempts[egress_before:], [], "socket-level guard")]
    if case.wall_max_s is not None:
        out.append(check("wall_clock", run.wall_s <= case.wall_max_s, round(run.wall_s, 1), f"≤ {case.wall_max_s} s"))
    return out


@pytest.mark.parametrize("case", _params())
def test_case(bench: tuple[BenchEnv, Report], case: Case):
    env, report = bench
    for req in (*_COMMON_REQUIREMENTS, *case.requires):
        why = requirement_missing(req)
        if why:
            report.add(CaseRecord.skipped_case(case, why))
            pytest.skip(f"case {case.id}: {why}")

    fixture = env.fixture(case.fixture)
    sid = env.clone(case.fixture, label=case.slug)
    ctx = CaseCtx(env=env, sid=sid, narration=fixture.narration, before=M.Snapshot.take(env.session_dir(sid)))
    if case.setup is not None:
        case.setup(ctx)
        ctx.before = ctx.snapshot()

    egress_before = len(env.egress.attempts)
    run = env.run_prompt(sid, case.prompt, allow_downloads=case.allow_downloads, answers=case.answers)
    ctx.run = run
    try:
        assertions = case.checks(ctx)
    except Exception as e:  # noqa: BLE001 — a check that cannot run is a failed case, not a crashed suite
        assertions = [check("checks_raised", False, f"{type(e).__name__}: {e}", "checks complete")]
    record = build_record(case, run, assertions, extra=_harness_assertions(case, env, run, egress_before))
    report.add(record)

    failed = record.failed_assertions
    if failed:
        lines = [f"{a['name']}: measured {a['measured']!r}, expected {a['expected']!r}"
                 + (f" — {a['detail']}" if a.get("detail") else "") for a in failed]
        pytest.fail(f"case {case.id} {case.prompt!r} via {run.brain}:\n  " + "\n  ".join(lines)
                    + f"\n  reply: {run.reply[:300]!r}")


def test_brains_report_is_honest_about_this_machine(bench: tuple[BenchEnv, Report]):
    """`/api/prompt/brains` with no key: every unavailable rung carries a fix
    naming a real remedy; a cached local model is reported as cached and is
    never offered as a download; Apple Intelligence reports its probe state."""
    env, _ = bench
    body = env.brains_report()
    rows = {b["id"]: b for b in body["brains"]}
    assert set(rows) >= {"recipes", "apple_intelligence", "local_model", "claude"}
    assert rows["recipes"]["available"] is True
    assert rows["claude"]["available"] is False and rows["claude"]["fix"]
    for b in rows.values():
        if not b["available"]:
            assert b["fix"], f"{b['id']} is unavailable without a fix"
    hub = Path(os.environ.get("HF_HOME") or (Path.home() / ".cache" / "huggingface")) / "hub"
    cached_7b = any((hub / "models--mlx-community--Qwen2.5-7B-Instruct-4bit" / "snapshots").glob("*/config.json"))
    local = rows["local_model"]
    if cached_7b and local["available"]:
        assert "cached" in local["detail"].lower() and local["action"] != "download", local
    if not rows["apple_intelligence"]["available"]:
        assert rows["apple_intelligence"]["detail"], rows["apple_intelligence"]


def test_fixture_transcription_hears_the_planted_fillers(bench: tuple[BenchEnv, Report]):
    """The fixture pre-flight (§8 K): whisper-small on the 85 s fixture must
    transcribe every planted filler as a `FILLERS_STRICT` token, else
    "≥ 8/9 gone" measures the ASR, not the editor. The ground-truth timing
    tolerance (0.3 s) is the same margin `remove_fillers`' pad + a cut edge
    tolerate."""
    env, _ = bench
    fx = env.fixture("en_16x9")
    assert fx.narration is not None and fx.transcribe_s is not None
    edl = M.load_edl(env.session_dir(fx.sid))
    words = M.transcript_words_source(env.session_dir(fx.sid), edl)
    heard = [w for w in words if str(w.get("word", "")).strip().lower().rstrip(",.!?") in
             ("um", "uh", "hmm", "erm", "uhh", "umm")]
    matched = sum(1 for u in fx.narration.fillers
                  if any(abs(float(w["start"]) - u.voiced[0]) <= 0.3 for w in heard))
    assert matched >= 8, (matched, [(w["word"], w["start"]) for w in heard])
    assert fx.transcribe_s < 60, f"transcribe(small) took {fx.transcribe_s:.1f}s on the fixture"
