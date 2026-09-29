# Edit with a prompt — no API key

The Prompt bar (above the preview; `/` focuses it) turns one sentence into a
verified edit. It works with **no cloud key**: a grammar-and-recipe planner
answers most prompts instantly, an on-device language model normalises the
rest, and Claude is used only when you add a key. Every run tells you which
brain answered and why a better one was not available. Since 0.8.0 a key-free
run is **preview, then apply**: the bar shows what it would change and changes
nothing until you press Apply (see below).

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
remove the captions                       take off the black and white filter
remove the transition between clip 2 and 3   delete the title
trim the second clip to 2 seconds         make the first clip 3 seconds long
lower the volume of the second clip       make the music quieter by 6 dB
flip the second clip                      mirror this clip
add a zoom in animation to the first clip   make the sticker bounce in
slide the last clip out to the left         make the second clip shake
blur the background                       make the background black
use this image as the background          fill the black bars with white
set the overlay to screen                 multiply blend the top clip
undo that
```

Edits by name (wave E): captions, a filter (all of them, a named look, or one
clip's), a transition (by its seam, first/last, or all) and a text overlay (by
its words, its role, or all) are removed by what they are; a clip's length is
set through its speed or speed curve, moving its end ("off the start of" moves
its head); a clip's level moves from its CURRENT gain; flip / mirror toggles
(`upside down` is still a 180° rotation). When the words fit more than one
thing ("delete the title" with two titles) the reply asks which, and nothing
changes.

Clip animations (wave E): CapCut's In / Out / Combo on a clip, the overlay or
a sticker ("add a zoom in animation to the first clip", "make the sticker
bounce in", "make the second clip shake", "remove the animation from the
sticker"). A motion that is only an In or an Out is the In unless the words
say out / exit / leave / away; "slide … out" with no direction and "animate
the first clip" with no motion ask which. "zoom in on the second clip" is
still the Ken Burns push, "fade in the second clip" the video fade.

Canvas and blend (wave E): "blur the background" / "make the background
black" / "use this image as the background" fill the letterbox of every
main-track clip (CapCut's Apply to all) unless a clip is named; "remove the
background blur" puts black bars back ("remove the background" is NOT this —
it is the AI cut-out). "set the overlay to screen" / "multiply blend the top
clip" set an overlay clip's blend mode. The reply asks which picture when
several are imported, which overlay when several are on screen and none is
selected, and which mode when none is named.

Clauses combine (`, and, then, aur, phir`) and Hinglish verbs are understood
(`captions laga do`, `silence hata do`). Negation excludes a step
(`no captions`, `without music`, `bina music`).

## What a run looks like

1. **Plan** — the steps, in composition order (cuts → structure → look →
   transitions → reframe → captions → text → music → audio → export preset →
   audit), with the brain badge and an estimate. Transitions come before
   captions because a cross-fade shortens the timeline; captions are laid
   afterwards so no cue runs past the picture.
2. **Preview** (key-free brains, when "Ask before applying Prompt bar edits"
   is on) — the plan is dry-run on a throwaway copy of the project and the
   card lists every change it would make; nothing has happened yet. Apply
   runs the same plan for real; Change drops it. Details in the next section.
3. **Steps** — each tool with progress; a step that had nothing to do says
   `ok · no effect` ("remove_fillers: nothing to cut") rather than pretending.
4. **Verify** — measured postconditions, from the EDL, the transcript mapped
   through `agent/timemap`, ffprobe or a 360p render: "captions cover 96 % of
   speech", "−14.3 LUFS (target −14)", "no overlay outside the 9:16 safe
   zone". Failed checks come first in the reply, with measured vs expected.
5. **One undo step** — a whole prompt run is one op; ⌘Z takes all of it back.
   Sessions created by `make shorts` are kept.

## Preview, then apply (0.8.0)

Without a Claude key, pressing Enter no longer edits. The plan from Recipes,
Apple Intelligence or the local model is run as a **dry run** on a scratch
copy of the project (`agent/prompt/preview.py::scratch_store` — a throwaway
store beside the sessions holding a deep copy of the live timeline, transcript
and speakers; every file a tool writes lands there and is deleted; render-only
tools are skipped, `make shorts` saves no sessions, nothing is committed and
no undo step is written). The **K3 safety net** judges the dry run exactly as
it judges a real run, so a plan that would make a wrong edit becomes the same
"I undid that: … Which did you mean?" question, never a card.

**How the card is built.** Its lines come from the difference between the live
timeline and the scratch copy — never from the plan's text. `changes.py`
flattens both timelines under stable keys (a clip's gain, a transition at a
seam, the canvas size), `change_rules.py` turns groups of changed keys into
sentences (a clip's speed before and after, with its length before and
after; a transition's seam, look and length; a title's words, look and
times) and `change_words.py` holds the editor words; a rule may claim
only the keys it explains, and every key no rule claims gets a generic line,
so **the card never omits a change**. The card opens with "Nothing has changed
yet.", lists every line (scrolls; "and N more changes" opens the rest), and
says what it will do with a screen reader announcement of the same text. The
chat pane gets the same list as text with a yes/no question.

**Apply / Change.** **Apply** (↵, the focused default) runs the SAME plan on
the live project in one batch — one op, one ⌘Z. Before it commits, the result
must match the preview (same changes, or the very same lines); if the timeline
changed while the card was open, or the card expired (ten minutes, like any
clarification), nothing is applied and the prompt is planned again into a
fresh card — never a stale apply. **Change** (Esc) drops the card, puts the
cursor back in your sentence and commits nothing. Typing a new sentence while
a card is open replaces it; typing `yes` / `no` in the bar (or the chat pane)
applies or drops it; `undo` typed while a card is open drops the card and
undoes nothing. A preview that could not be made because the disk is full
says so instead of "Nothing to change".

**The setting.** Settings › Prompt bar › **"Ask before applying Prompt bar
edits"** — **on by default**. Off restores the pre-0.8.0 behaviour: the plan
runs as soon as it is planned. The value lives with the app settings
(`settings.json` → `prompt.confirm_before_apply`, `GET`/`PUT
/api/settings/prompt`, loopback + same-origin + JSON like every other app
setting); `VAI_PROMPT_CONFIRM=1|0` overrides it for a developer or a test
harness (`prompt_setting.py`). The Claude rung, and the chat assistant with a
key, are never previewed.

**The safety net does not depend on the card.** With the switch off, the K3
safety net still judges every run inside the same batch — a wrong edit is
still rolled back before it is committed — only the card is skipped. With it
on you get both: the net on the dry run, the card, the net again on the real
run, and the preview-versus-result check before the commit.

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
| **The preview card** ("Nothing has changed yet." + the change list) | a key-free plan, with "Ask before applying Prompt bar edits" on | **Apply** (↵) or **Change** (Esc); `yes` / `no` typed in the bar or the chat pane |
| **A model's own question** ("Rotate by how much?") | Apple Intelligence, the local model or Claude planned the edit but needs one more fact | type the answer in the card; the request is planned again with it ("… — 90 degrees"). A complete new request typed instead is planned on its own |

Nothing in the prompt path downloads without a **yes** — "no cloud key" is
not "no network", the rule is no network without your answer. Model
downloads themselves can only be started from the Mac (loopback-only routes),
never from a paired phone.

## Answering from the phone — temporarily unavailable in this build

The iPhone companion and local-network pairing are **turned off in 0.7.1**, so
in this build every clarification is answered on the Mac, in the Prompt bar or
the chat pane. Nothing was removed: the feature is gated behind one flag —
`PHONE_PAIRING_ENABLED` in `src/video_ai_editor/api/pairing.py`, read through
`phone_pairing_enabled()` and set by the environment variable
`VAE_PHONE_PAIRING` (`1`/`true`/`yes`). A developer who wants the phone back
runs the app with `VAE_PHONE_PAIRING=1` and gets exactly the 0.7.0 behaviour
described in the rest of this section; `GET /api/version` reports the live state
as `phone_pairing`. Existing pairing settings and remembered devices are left
untouched while it is off.

With the flag on, the phone shows the question as text ("Which language for the captions?
Reply **hi**, **en**, **hinglish** or **es**."). Reply with the **whole
word** — the value, its label, an ordinal (`first`, `the second one`),
`yes`/`no`/`haan`/`nahi`, or a number. A partial match never counts: `hi`
resumes the plan, `make it hi-res` drops the pending question and plans your
new request instead. A question expires after ten minutes, and any edit made
elsewhere in the meantime invalidates it.

On the phone the first line of every reply names the brain: `via Recipes — …`
(the Mac shows the same prefix, so nothing about the reply shape depends on the
flag).

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
VAI_PROMPT_CONFIRM=           # unset = Settings › Prompt bar › "Ask before applying Prompt bar edits" (on by default);
                              # 1 | 0 overrides it for this run (prompt_setting.py)
VAE_PHONE_PAIRING=            # unset/0 = the iPhone companion and LAN pairing are OFF (0.7.1 default);
                              # 1 | true | yes restores them (api/pairing.py::PHONE_PAIRING_ENABLED)
```

## Where things live

`agent/prompt/` — schema, facts, slots, grammar, recipes, planner, validate,
presets, executor, verify, pending, service, summary, runlog; preview, then
apply: `preview.py` (the dry run, the scratch store, the apply check),
`changes.py` + `change_rules.py` + `change_words.py` (the card's lines from
the timeline diff), `prompt_setting.py` (the switch);
`agent/prompt/brains/` — the four brains, the router, JSON repair, content
tasks; `tools/fm-planner/` — the Apple Intelligence helper;
`api/prompt_routes.py` — `/api/sessions/{sid}/prompt*`, `/api/prompt/*`,
`GET`/`PUT /api/settings/prompt`;
`frontend/src/components/PromptPreviewCard.tsx` + `lib/promptApplySetting.ts`
— the card and the Settings switch;
`presets/{text_styles,transitions,templates,music}` — the presets (beds are
generated by `scripts/gen_music_beds.py`); `tests/benchmark/` — the
CapCut-parity benchmark (`docs/BENCHMARK.md`).
