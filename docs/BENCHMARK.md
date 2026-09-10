# The CapCut-parity benchmark

What the Prompt Editor can do with **no API key**, measured — not described —
on media whose ground truth is known to the millisecond, with zero network
egress. The code is `tests/benchmark/` (its README has the commands); this
page is the method and the claim.

## The claim, and how it is made

Every number in the "Latest report" section below is copied **verbatim** from
the Markdown the harness writes (`<user cache>/Video AI Editor/bench/reports/latest.md`).
A number that is not in a report file is not a claim this project makes. The
report carries, per case: the prompt, the brain that answered (and the brain
that wrote any content), pass/fail, every assertion with measured vs expected,
whether the app's own `verify` event agreed, wall-clock seconds, the steps
that ran, the steps that were skipped, and any downloads the plan would have
needed. Per brain it carries totals; overall it carries two headline numbers
— the **fast tier** and the **slow tier** — the machine line and the
first-use download list.

Not covered, by design: object removal, background removal, motion tracking,
multicam, licensed music (the beds are procedural). Beat sync places pulses
and word-boundary-safe splits on detected beats; it does not re-arrange
footage.

## Ground truth

The narration is twelve sentences of a camera review (~85 s) synthesized one
utterance at a time with Piper `en_US-amy-medium`, so the source time of
every planted event is an exact concat offset, not a whisper guess:

* **nine single-token fillers** — `um`×6, `umm`×3 — each its own utterance
  with a 0.12 s gap on both sides. Not the spec's `um/uh/hmm` mix: measured on
  this voice, whisper-small transcribes `Um.`/`Umm.` every time but **drops
  `Uh.` in every spelling and length tried** and writes `Hmm.` as `Hum`, which
  is in no filler list. A filler the ASR cannot perceive from a synthetic
  voice would measure whisper, not the editor. The count assertion
  (≥ 8/9 gone, judged by ground-truth **time**, never by text) is unchanged.
* **one content "like"** ("I like this part.") that must survive — an
  unquoted `like` is a content word.
* **seven 2.0 s pauses** of digital silence; every other silence in the file
  is under `remove_silences`' 0.5 s threshold, so the only silences the tool
  can find are the planted ones (`test_media_synthesis` pins this with
  `silencedetect`: exactly seven, starts within 0.05 s, durations 2.09 s).
* **six lavfi scenes** (1920×1080@30) with hard cuts at known frames;
  `detect_shots` finds all five within 0.05 s. A 9:16 variant, 20 s of
  b-roll, a 200 s fixture for the music loop path and a 12-minute fixture for
  the run-time gate.
* **procedural beds** — 48 kHz, 180 s, −18 LUFS, a kick on an exact grid (100
  BPM for the session-uploaded bed; the four mood beds at 90/120/70/85). The
  fixture stores **librosa's own detected beats** beside the grid: case 10 is
  scored against what the detector sees (0.02 s after each kick on this bed),
  so a detector regression is distinguishable from an editor regression.

## Measurement rules

* Every assertion reads the **persisted** EDL (`edl.json`), the persisted
  transcript, ffprobe, or a 360p render made through the export audio path
  (loudnorm on) — never the in-memory store and never the app's `verify` event.
* Every source↔timeline conversion goes through `agent/timemap` directly.
  "Captions cover ≥ 90 % of speech" is measured in **timeline** seconds after
  the cuts; a caption laid at source time after a cut would fail.
* The app's own `verify` headline verdict must **agree** with the independent
  one; a disagreement is the `verifier_drift` assertion and fails the case
  in both directions. It is a finding about the verifier, not a pass.
* Loudness is measured on the render (the target applies at export, not
  preview — baseline finding 4). Tolerance is ±1 LU; ±1.5 LU is accepted for
  this short content with the reason recorded in the assertion detail: EBU
  R128 integrated loudness gates at −70 LUFS and a 60–85 s programme with 14 s
  of planted silence has few gated blocks, so ffmpeg's two-pass `loudnorm`
  lands within 1.5 LU rather than 1.0. The product check stays at ±1.
* Exported `.srt/.vtt/.ass` stay source-timed by design (baseline finding
  15); no case asserts an exported subtitle file against a cut render — the
  captions **track** is what is measured.
* **Two clocks.** The EDL stores every lane in **layout** time; a v1 cross-fade
  plays clip A's last *d* seconds and clip B's first *d* seconds in the same
  output window, so the picture and speech after a seam reach the screen
  *d* early. `render_time(t) = t − Σ{d : seam ≤ t}` is the render's clock, and
  the transition cases measure the overlay and audio lanes against **it**, on
  the render — a check that compares layout coordinates with layout
  coordinates (`captions_relaid_without_drift`, `edl_duration_agrees`) cannot
  see a lane that plays late.
* No network: a socket-level guard fails any non-loopback connection for the
  whole session; `huggingface_hub.snapshot_download` can only resolve from the
  local cache. `downloads` questions are answered **skip**, so a case that
  needs MADLAD (2; the Hindi variant of 20) skips with the reason unless the
  model is already on disk.

## Tiers

**Fast** (target < 12 min on the build Mac): 1 captions · 2 captions +
translate (Hindi fixture; MADLAD required) · 3 remove silences · 4 remove
fillers · 5 tighten + captions with the transcript deleted (exactly one
`transcribe`, zero `auto_caption`) · 9 music + duck · 10 beat sync · 11 hook ·
12 LUT after a silence cut · 13 audio clean-up to −14 LUFS · 14 speed ·
15 trim · 16 lower third · 17 brand kit + end card · 18 smooth transitions
(Basic) · 21 voiceover · 22 undo · 25 zoom transitions · 26 glitch transition.

**Slow**: 6 three shorts · 7 reframe to Reels with captions laid first ·
8 TikTok preset (implied reframe) · 19 "make it good for youtube" · 20 the
full Reels pipeline (one op; undo restores the hash) · 23 the run-time gate on
the 12-minute fixture (a `go` question before any step; `no` leaves the EDL
untouched) · 24 the pending-transcript wait.

Every case also asserts the run's shape: `brain{answered}`, the first
`text_delta` starts with `via `, one `done` per turn, no `error`, exactly one
`op` for a mutating case, only the eleven known event types, no egress.

## Transitions

Three cases apply transitions from three CapCut categories through the prompt
and measure the ops and the render: 18 "add smooth transitions between the
clips" (Basic: `crossdissolve` at all five seams, captions re-laid and in
sync within 0.1 s at every seam), 25 "smooth zoom between every clip" (Zoom
family), 26 "add a glitch transition at the hook" (Glitch/Stylised family at
the opening seam). Each asserts exactly one op, an overlap total of at least
0.1 s per applied transition (the catalog's shortest duration), and that the
render's **audio-stream** duration (sample-exact; the container duration is
frame-quantised) equals the clips' extent minus the overlaps within a frame
+ 20 ms — computed from the clips and transitions alone, not from
`EDL.transition_overlap()`; `seam_table_agrees` reports, by name, whether
that restated rule and the product's `EDL.v1_seam_table()` name the same
seams.

### The render-clock probes

Before the prompt runs, each transition case plants two **probe pairs**
through the real dispatch route: a canvas-covering pure-magenta sticker
(`add_sticker`, `scale = 1/0.22` so the 22 %-of-long-edge default covers the
frame) and a 0.5 s, 1 kHz tone as a clip on the `vo` lane (`add_clip`) — one
pair 3 s **before** the first seam and one pair 3 s **after** it, snapped
outward to the 0.1 s grid (exact in the renderer's three-decimal `enable=`
windows and on a frame at 30 fps). The pre-transition render is kept. After
the prompt, the transitioned render is sampled: every frame of the search
window is box-averaged to one pixel (`scale=1:1:flags=area`, stamped with its
own container pts via `-copyts` + `showinfo`, `fps_mode passthrough`) and
classified magenta or not; the audio is band-passed 40 Hz around 1 kHz and
`astats` gives an RMS level every 10 ms, the tone being the longest run within
8 dB of the window's peak provided that peak stands 12 dB above the median.

| assertion | measured how | passes when |
|---|---|---|
| `overlay_follows_render_clock` | first/last magenta frame of the post-seam sticker | window = `[render_time(t), render_time(t + 0.5))` ± 1 frame (+5 ms) |
| `audio_follows_render_clock` | onset/offset of the post-seam 1 kHz tone | same window ± 0.05 s |
| `probes_before_first_seam_unmoved` | both pre-seam probes | at their **layout** window (no seam precedes them, so render time = layout time) |
| `probes_visible_before_transitions` | the post-seam pair in the render kept from before the prompt | at their layout window — the guard against a probe that never renders passing the two checks above vacuously |
| `seam_table_agrees` | `M.seam_overlaps(edl)` vs `EDL.v1_seam_table()` | identical `(seam, seconds)` lists |

The probes never straddle a consumed span (3 s clear of any catalog
transition on either side), so the rule for an overlay that lies entirely
inside a cross-fade's consumed tail — its mapped window is empty and it is
**dropped**, never inverted — is the render-clock unit tests' to pin, not the
benchmark's. The detail of a failing probe assertion carries the measured
**lag** against the picture, so the report reads as a number, not a verdict.

## First-use downloads

The prompt path never downloads without a **yes**. Sizes from
`facts.FIRST_USE_BYTES`; cached/not-cached probed on the build Mac by the
product's own `Path.exists` checks (2026-09-10): faster-whisper small: cached;
faster-whisper large-v3: cached (3.1 GB when not); MADLAD-400 translation
model: 3 GB; Piper voice hi_IN-priyamvada-medium: 60 MB.

## Machine

Apple M4 Max (arm64), 36 GB, macOS 26.6.2, Python 3.11, ffmpeg 8.1.1.
Apple Intelligence is **off** in System Settings on this machine (the helper
reports `appleIntelligenceNotEnabled`), so the Apple Intelligence rung is
reported unavailable with that fix and the on-device band falls to the local
model; `mlx-community/Qwen2.5-7B-Instruct-4bit` is cached (warm load 2.4 s,
plan ≈ 6 s with schemas in the prompt).

## Latest report

**The report file is the claim.** Run

```
VAI_BRAIN=recipes uv run pytest -m benchmark tests/benchmark
```

and paste `<user cache>/Video AI Editor/bench/reports/latest.md` here,
verbatim, under this heading. `latest.md` / `latest.json` are refreshed
**only by a run that selected every case**: a `-k transitions`, `-x` or
tier-marker run writes a stamped `*_partial.md` whose headline says
`PARTIAL RUN (n/26 cases; not run: …)` and leaves `latest.*` alone, so this
section can never quote "Fast tier: 3/3".

No `latest.md` exists in this checkout's cache (the reports directory has
not been created), so **no headline number is claimed here yet**. The note
that used to sit in this section — that the benchmark route was registered
as `/api/sessions/None/prompt` and every case skip-failed with a 405 — was
wrong for this tree: `service.route(name)` returns the FastAPI template when
`sid` is None and the harness drives the real
`POST /api/sessions/{sid}/prompt`.

### Commentary on the last partial fast-tier run (2026-09-10, recipes brain)

Not a source of numbers — the findings the harness surfaced on the build Mac
with `-m "benchmark and not slow"`, each of which has since been turned into a
product change and a test:

* **Cases 3, 5 — `verifier_drift`.** The app's `speech_preserved` counted two
  sentence-initial "The" as lost: whisper starts a word after a 2 s pause
  ~0.3 s before the voice, and the silence cut trims that air. The verifier
  now also accepts a word whose END plays with ≥ 40% of its span
  (`_WORD_TAIL_RATIO`); a cut through the tail still fails.
* **Case 8 (and 19/20 partly) — transcript lost after `auto_reframe` /
  `noise_reduce`.** Every src-rewriting tool now records a `.origin` sidecar
  (`agent/media_origin.py`) and the transcript follows the footage;
  reframe-then-captions lays cues and `speech_preserved` measures.
* **Case 10 — `min_shot` 0.697 s.** Still open: the recipe used
  `auto_cut_to_beats` although a transcript existed.
* **Case 16 — lower third with empty text.** Still open in the `title`
  recipe; the app's `text_present` passed on it.
* **Case 18 — `captions_sync` 1.63 s.** The assertion measured whisper's
  segment start ("Um," inside the preceding pause), present with zero
  transitions; it now measures DRIFT — the per-seam cue-vs-word offset must
  not change through the re-lay (`captions_relaid_without_drift`).
* **Cases 18, 25, 26 — the overlay lanes play late by the accumulated
  overlap (2026-09-11).** The render-clock probes, run against the renderer
  before `render/clock.py` existed: with 0.4 s consumed at the first seam
  (case 18) the post-seam sticker appeared at 14.133 s for a picture-relative
  expectation of 13.667 s (**lag +0.467 s**) and the VO-lane tone at 14.080 s
  (**+0.413 s**); with 0.3 s consumed (25, 26) the lags were +0.367 / +0.313 s.
  The pre-seam pair did not move (measured 8.10 / 8.08 s for a layout start
  of 8.067 s — the renderer's three-decimal `enable=` window, which is why the
  probe times now snap to the 0.1 s grid). Every lane positioned by
  `enable=between(t,start,end)` or `adelay=start` — text, captions, stickers,
  PiP, music, voiceover — was placed in layout time while the v1 picture had
  been pulled left; the desktop timeline drew the same thing. The fix is the
  render clock (`render_time(t) = t − Σ overlap at or before t`) applied in
  the renderer and in the desktop's drawing; these assertions are what
  proves it landed.
* **Case 20 — Hindi captions dropped when MADLAD is skipped** instead of
  falling back to as-spoken captions; the reply said so. Still open.
* **Case 22 — "undo that" recognised, nothing dispatched.** Still open.

The wall-clock, egress and event-shape assertions are harness facts; they no
longer count toward `verifier_drift` (which compares the app's verdict with
the case's own measurements only).
