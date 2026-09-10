# CapCut-parity benchmark

Twenty-six prompts through the real `/api/sessions/{sid}/prompt` route on
synthesized media with **known** ground truth, measured independently of the
app's own verifier, with **zero network egress**. Spec: `docs/design/PROMPT_EDITOR_SPEC.md` §6;
method and the honest claim: `docs/BENCHMARK.md`.

```bash
# everything (fast + slow tiers), recipes brain
VAI_BRAIN=recipes uv run pytest -m benchmark tests/benchmark

# fast tier only (< 12 min on an M4 Max)
VAI_BRAIN=recipes uv run pytest -m "benchmark and not slow" tests/benchmark

# the three transition cases (18 basic, 25 zoom, 26 glitch)
uv run pytest -m benchmark -k transitions tests/benchmark

# another brain (the ladder is what the app would use; VAI_BRAIN pins one rung)
VAI_BRAIN=mlx uv run pytest -m benchmark tests/benchmark
```

The default `uv run pytest` **excludes** the benchmark (`addopts = "-m 'not benchmark'"`);
`test_media_synthesis.py` is deliberately unmarked so the media pipeline, the
socket guard and session cloning stay covered by every default run.

Never run two pytest processes at once: pytest's basetemp pruning deletes the
other run's temp dirs, and the benchmark's WORKDIR is one of them.

## What runs

| Module | Role |
|---|---|
| `narration.py` | the 12-sentence script, 9 planted single-token fillers (`um`×6, `umm`×3), one content "like", seven 2.0 s pauses; Piper `en_US-amy-medium`; ground truth from concat offsets |
| `media.py` | lavfi six-scene 1920×1080@30 with hard cuts at known times, the 9:16 variant, 20 s b-roll, the 200 s loop fixture, the 12-minute run-time-gate fixture; procedural beds (48 kHz, 180 s, −18 LUFS, exact kick grid) with librosa's own detected beats stored beside the grid |
| `harness.py` | TestClient with WORKDIR redirected; `ANTHROPIC_API_KEY=""`, `HF_HUB_OFFLINE=1`, `WHISPER_BACKEND=faster_whisper`, `WHISPER_MODEL=small`; one upload + `transcribe` per media variant, cloned per case (paths rewritten); the prompt runner that auto-answers clarifications; the socket-level egress guard |
| `measure.py` | every assertion's numbers, from the persisted EDL / transcript / ffprobe / a 360p render; every source↔timeline conversion through `agent/timemap` |
| `prompts.py` | `CASES` — the case format (prompt, fixture, tier, setup, checks, expected intents) |
| `report.py` | per-case records, per-brain totals, the two headline numbers, the machine line, the first-use download list → JSON + Markdown |
| `test_capcut_parity.py` | `@pytest.mark.benchmark`, one item per case |
| `test_media_synthesis.py` | unmarked: synthesis, ground truth, the loop fixture, the guard, cloning, the SSE parser |

## Tiers and marks

* **fast** (1–5, 9–18, 21, 22, 25, 26): captions, silences, fillers, tighten,
  music + duck, beat sync, hook, LUT, audio clean-up, speed, trim, lower third,
  brand kit, transitions ×3, voiceover, undo.
* **slow** (6, 7, 8, 19, 20, 23, 24; `-m slow`): shorts, reframe, export
  preset, auto-edit, the full Reels pipeline, the 12-minute run-time gate, the
  pending-transcript wait.
* `tts`: needs a Hindi voice (Piper `hi_IN-priyamvada-medium` cached, else
  macOS `say -v Lekha`); case 2 also needs MADLAD cached and **skips with the
  reason** otherwise — the benchmark never downloads anything.

## Clarifications, downloads, egress

Questions the planner asks are answered from the case (`answers`), else the
default, else the first option. `downloads` is answered **no** unless the case
sets `allow_downloads` (none do); `go` is answered **yes**, except case 23 which
answers **no** to prove the run-time gate leaves the timeline untouched.

The guard patches `socket.socket.connect` / `connect_ex`,
`socket.create_connection` and `socket.getaddrinfo` for the whole session:
any non-loopback attempt raises and fails the case. `huggingface_hub.
snapshot_download` is wrapped with `local_files_only=True` so a cached whisper
model still loads and an uncached one raises instead of fetching.

## Per-case harness assertions

Every case also asserts: a `brain{answered}` event; the first `text_delta`
starts with `via `; exactly one `done` per turn; no `error`; exactly one `op`
for a mutating case (none for a read-only one); only the 11 known event types;
no egress; and `verifier_drift` — the app's `verify` headline verdict must
agree with the independent measurement.

## Output

`<user cache>/Video AI Editor/bench/reports/<stamp>_<brain>.{json,md}` plus
`latest.{json,md}` (override the directory with `VAI_BENCH_REPORT`). Media is
cached under `<user cache>/Video AI Editor/bench/<media_key>/` (override with
`VAI_BENCH_CACHE`); the key hashes the script and synthesis parameters, so a
change to either rebuilds once.

## Fixture facts (measured on the build Mac, 2026-09-10)

* narration 85.0 s; speech 67.9 s; planted pauses 7 × 2.0 s; fillers at
  7.01, 22.77, 30.47, 36.17, 39.96, 55.29, 63.43, 70.92, 79.97 s (source)
* `detect_shots` finds all five cuts within 0.05 s; `silencedetect` (−30 dB,
  0.5 s) finds exactly the seven pauses, starts within 0.05 s, durations 2.09 s
* bench bed 100 BPM: librosa's beats sit 0.02 s after the kick grid (all on
  grid within 60 ms)
* whisper-small on the 16:9 fixture: 6.4–7.3 s, 202 words, all nine fillers
  transcribed as `Um` within 0.3 s of the planted time, the content `like` at
  36.89 s; upload + normalise 4.1 s
