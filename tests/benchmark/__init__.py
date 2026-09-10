"""CapCut-parity benchmark for the Prompt Editor (spec §6, docs/BENCHMARK.md).

Module map:

  narration.py          the 12-sentence script, planted fillers and pauses;
                        Piper synthesis; GROUND TRUTH from concat offsets.
  media.py              lavfi scene video (known hard cuts), 9:16 / b-roll /
                        200 s / 12-minute variants, procedural beat beds with
                        librosa's own detected beats stored beside the grid.
  harness.py            TestClient with WORKDIR redirected, the socket-level
                        egress guard, fixture upload + transcribe ONCE per
                        media variant, session cloning, the prompt runner
                        that auto-answers clarifications.
  measure.py            independent measurements on the EDL / transcript /
                        ffprobe / a 360p render — every one goes through
                        `agent/timemap` directly, never through the app's
                        `verify` event (which is asserted to AGREE).
  prompts.py            the 24 CASES with machine-checkable assertions — the
                        benchmark case format (§0.2, consumed by P's grammar
                        fixtures too).
  report.py             per-case records, per-brain totals, the two headline
                        numbers, the machine line, the first-use download
                        list, written as JSON + Markdown.
  test_capcut_parity.py `@pytest.mark.benchmark` — excluded from the default
                        run by `addopts`; `pytest -m benchmark` opts in.
  test_media_synthesis.py  UNMARKED (< 20 s warm): synthesis, ground truth,
                        the 200 s loop fixture, the socket guard, cloning.

WHY the benchmark is a package under tests/ and not a script: pytest gives
it parametrised ids per case, `-m` tiers, `-k` selection, skips with reasons
(MADLAD not cached, routes not mounted yet), and a session-scoped fixture that
uploads + transcribes each media variant exactly once.
"""
