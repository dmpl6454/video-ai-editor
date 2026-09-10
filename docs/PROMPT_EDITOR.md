# Edit with a prompt — no API key

The Prompt bar (above the preview; `/` focuses it) turns one sentence into a
verified edit. It works with **no cloud key**: a grammar-and-recipe planner
answers most prompts instantly, an on-device language model normalises the
rest, and Claude is used only when you add a key. Every run tells you which
brain answered and why a better one was not available.

## The brain ladder

| Brain | What it is | When it answers |
|---|---|---|
| **Recipes** | intent grammar → slot extraction → recipe table (`agent/prompt/{grammar,slots,recipes,planner}.py`) | always tried first; answers when confidence ≥ 0.75 — every benchmark prompt |
| **Apple Intelligence** | the on-device Foundation Model through `tools/fm-planner` (a network-free Swift child process) | macOS 26+ with Apple Intelligence **on** in System Settings; used for prompts the grammar reads at 0.4–0.75 |
| **Local model** | `mlx-community/Qwen2.5-7B-Instruct-4bit` via MLX (`uv sync --extra local-llm`; needs ≥ 24 GB RAM, 3B tier at 12–24 GB) | same band, after Apple Intelligence; loads only from the local HF cache |
| **Claude** | the cloud model, may emit raw tool steps | only with `ANTHROPIC_API_KEY` set and `VAI_PROMPT_CLOUD` not `0` |

Fallback: recipes at ≥ 0.4 run with "I read that as: …"; below that the bar
asks which of three guesses you meant. `GET /api/prompt/brains` (the badge
popover) shows each rung's real state and the fix — "appleIntelligenceNotEnabled
— turn on Apple Intelligence in System Settings", "not installed (pip)",
"installed; model cached at ~/.cache/huggingface". Pin a rung with
`VAI_BRAIN=recipes|fm|mlx|cloud|auto`.

The on-device brains never see file paths, tool schemas or secrets: they
receive recipe cards and emit an intent draft, which the same recipe table
expands into a plan. **Every plan — from any brain — passes
`agent/prompt/validate.py` before a single tool runs**: allow-listed tools
only, no unknown arguments, enum and numeric bounds, and no file path a model
wrote. A plan may only read files the session already offers (its uploads,
the bundled presets, its own TTS cache).

## Prompts that work

```
add captions                              cut out the ums
remove the silences                       tighten it up and add captions
make it vertical for reels                turn this into a tiktok
add chill background music and duck it under my voice
cut to the beat of the music              add a hook in the first 3 seconds
give it a cinematic look                  clean up the audio and normalize to -14 LUFS
speed it up 1.5x                          cut the first 5 seconds
add a lower third for Priya Sharma @priya.codes at the start
apply my brand kit @quicksolutions.in with #techtips and add an end card
add smooth transitions between the clips  smooth zoom between every clip
add a glitch transition at the hook       make 3 shorts under 30 seconds for tiktok
make it good for youtube                  complete the video for instagram reels with hindi captions and upbeat music
add a voiceover saying 'Thanks for watching' at the end
undo that
```

Clauses combine (`, and, then, aur, phir`) and Hinglish verbs are understood
(`captions laga do`, `silence hata do`). Negation excludes a step
(`no captions`, `without music`, `bina music`).

## What a run looks like

1. **Plan** — the steps, in composition order (cuts → structure → look →
   transitions → reframe → captions → text → music → audio → export preset →
   audit), with the brain badge and an estimate. Transitions come before
   captions because a cross-fade shortens the timeline; captions are laid
   afterwards so no cue runs past the picture.
2. **Steps** — each tool with progress; a step that had nothing to do says
   `ok · no effect` ("remove_fillers: nothing to cut") rather than pretending.
3. **Verify** — measured postconditions, from the EDL, the transcript mapped
   through `agent/timemap`, ffprobe or a 360p render: "captions cover 96 % of
   speech", "−14.3 LUFS (target −14)", "no overlay outside the 9:16 safe
   zone". Failed checks come first in the reply, with measured vs expected.
4. **One undo step** — a whole prompt run is one op; ⌘Z takes all of it back.
   Sessions created by `make shorts` are kept.

## The questions you may get

The planner asks only when it cannot know:

| Question | Why | Answer |
|---|---|---|
| **Which language for the captions?** | translate without a target | `hi`, `en`, `hinglish` or `es` |
| **Which handle?** | brand kit and no handle on the timeline | `@yourhandle` |
| **What should the voiceover say?** | voiceover without quoted text | the sentence |
| **Which ratio?** | the source is already vertical and no platform was named | `9:16`, `16:9`, `1:1`, `4:5` |
| **First use downloads … Download or skip?** | a model or voice is not on disk (MADLAD 3 GB, large-v3 3.1 GB, a Piper voice 60 MB) | `download` or `skip` — skipping drops the dependent steps and the reply says so |
| **This will take about N minutes. Start?** | the estimate is over 90 s | `yes` or `no` |

Nothing in the prompt path downloads without a **yes** — "no cloud key" is
not "no network", the rule is no network without your answer. Model
downloads themselves can only be started from the Mac (loopback-only routes),
never from a paired phone.

## Answering from the phone

The phone shows the question as text ("Which language for the captions?
Reply **hi**, **en**, **hinglish** or **es**."). Reply with the **whole
word** — the value, its label, an ordinal (`first`, `the second one`),
`yes`/`no`/`haan`/`nahi`, or a number. A partial match never counts: `hi`
resumes the plan, `make it hi-res` drops the pending question and plans your
new request instead. A question expires after ten minutes, and any edit made
elsewhere in the meantime invalidates it.

On the phone the first line of every reply names the brain: `via Recipes — …`.

## Transitions

`add_transition` and the Transitions panel accept every look in
`render/transitions.py` — all 58 native ffmpeg xfade transitions plus the
custom ones — grouped CapCut-style (Basic, Wipe, Slide, Zoom, Blur, Shape,
Glitch/Stylised, Light) with a default duration per look. From the prompt:
`add smooth transitions between the clips`, `smooth zoom between every clip`,
`add a glitch transition at the hook`. A transition overlaps the two clips
for its duration, so the timeline (and the render) gets exactly that much
shorter; the run summary says so.

**Two clocks.** Every clip `start`/`end`, caption cue, marker, transition `at`
and tool argument is in *layout* time — the EDL's own coordinates, which
never move. `edl.duration`, ffprobe and the frames of the rendered file are
in *render* time: `render_time(t) = t − Σ{d : v1 seam ≤ t}` over the
transitions the renderer actually cross-fades (`EDL.v1_seam_table()`, one
per seam, never more than the shorter clip). The renderer places every
other lane — captions, text, stickers, PiP, music, voiceover — at
`render_time(start)` and the desktop draws and plays them there, so an
overlay authored on a word stays on that word after any number of
transitions. `add_transition` stores the duration that will render (floored
at 0.1 s to the look's default, capped at 2.0 s and at the shorter
neighbour), so the stored number, the transport and the file agree.

## Environment

```
ANTHROPIC_API_KEY=            # optional — enables the Claude rung only
VAI_BRAIN=auto                # recipes | fm | mlx | cloud | auto
VAI_MLX_MODEL=                # override the RAM-tier model id
VAI_PROMPT_CLOUD=1            # 0 disables the Claude rung even with a key
VAI_FM_HELPER=                # path to a built fm-planner (dev; the .app bundles it)
```

## Where things live

`agent/prompt/` — schema, facts, slots, grammar, recipes, planner, validate,
presets, executor, verify, pending, service, summary, runlog;
`agent/prompt/brains/` — the four brains, the router, JSON repair, content
tasks; `tools/fm-planner/` — the Apple Intelligence helper;
`api/prompt_routes.py` — `/api/sessions/{sid}/prompt*`, `/api/prompt/*`;
`presets/{text_styles,transitions,templates,music}` — the presets (beds are
generated by `scripts/gen_music_beds.py`); `tests/benchmark/` — the
CapCut-parity benchmark (`docs/BENCHMARK.md`).
