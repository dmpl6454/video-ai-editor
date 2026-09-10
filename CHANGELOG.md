# Changelog

All notable changes to Video AI Editor. Versioning follows the `VERSION` file
at the repo root, surfaced at `/api/version` and in the editor's top bar.

## 0.7.0

### Added
- **Edit with a prompt, no API key.** A Prompt bar above the preview (`/`
  focuses it) turns one sentence into a verified edit through a ladder of
  brains: a grammar-and-recipe planner (`agent/prompt/{grammar,slots,recipes,
  planner}.py`) answers instantly at confidence ≥ 0.75; an on-device language
  model normalises the 0.4–0.75 band — Apple Intelligence through a
  network-free Swift helper (`tools/fm-planner`, macOS 26+, only when the
  feature is on in System Settings) then `mlx-community/Qwen2.5-7B-Instruct-4bit`
  via MLX (`uv sync --extra local-llm`; tiered by RAM, loaded only from the
  local Hugging Face cache); Claude is a rung only when `ANTHROPIC_API_KEY` is
  set. Every reply says which brain answered and, when a better rung was
  unavailable, exactly why and how to fix it (`GET /api/prompt/brains`). The
  on-device brains receive recipe cards and emit an intent draft; the same
  recipe table expands it into a staged plan, so an LLM plan gets stages,
  postconditions, prerequisites and idempotence for free.
- **Plans are verified, not assumed.** Every run ends with measured
  postconditions read from the EDL, the transcript mapped through
  `agent/timemap`, ffprobe or one 360p verify render made through the export
  audio path: captions cover of speech in timeline seconds, no cue past the
  picture, loudness on the render, no overlay outside the platform safe zone,
  splits on beats, fillers remaining. Failed checks lead the reply with
  measured vs expected. A whole prompt run is **one op, one undo step**;
  execution outlives the SSE connection (a phone that locks does not roll
  back a caption pass) and `GET …/prompt/run` reconnects to a running or
  finished run. Five new SSE events — `brain`, `plan`, `step`, `verify`,
  `clarify` — beside the six the phone knows; the first `text_delta` of every
  turn starts with `via <Brain> —` so the iPhone app shows the brain with zero
  changes.
- **Clarifications instead of guesses.** The planner asks only when it cannot
  know (caption language, brand handle, voiceover text, a ratio on an
  already-vertical source), plus two gates: a first-use download (MADLAD 3 GB,
  large-v3 3.1 GB, a Piper voice 60 MB) and a run over 90 s. From the phone
  you answer with a whole word (`hi`, `first`, `haan`, `skip`); a partial
  match never counts.
- **All the transitions from CapCut.** `presets/transitions` names every look
  in `render/transitions.py` — all 58 native ffmpeg xfade transitions plus the
  custom ones — with a CapCut-style display name, category (Basic, Wipe,
  Slide, Zoom, Blur, Shape, Glitch/Stylised, Light), default duration and a
  one-line description; `add_transition`/`list_transitions` accept and
  advertise every catalog name; the compositor honours per-transition default
  durations; the desktop gains a Transitions panel (category tabs, grid,
  local hover preview, apply-to-selected-cut, keyboard); and the prompt takes
  transition intents — "smooth zoom between every clip", "add a glitch
  transition at the hook".
- **`transcribe`** — a non-mutating tool that persists the first v1 clip's
  transcript without laying captions; the prompt path's prerequisite, so
  "remove the ums" on a fresh upload never means a second transcription pass
  or an unwanted caption track. Refuses a model that is not on disk.
- **Presets**: six text styles, four transition looks, five edit templates,
  and four procedural music beds (`scripts/gen_music_beds.py` — 48 kHz,
  180 s, −18 LUFS, an exact kick grid with a `.json` sidecar; generated at
  build time, never committed).
- **The CapCut-parity benchmark** (`tests/benchmark/`, `docs/BENCHMARK.md`):
  26 prompts through the real route on synthesized media with ground truth to
  the millisecond, measured independently of the app's verifier, with a
  socket-level guard that fails any network egress. `pytest -m benchmark`
  opts in; the media pipeline and the guard are in the default run.
- iOS build number 2.

### Changed
- **One source↔timeline mapping for every transcript consumer** (the
  `d06d1c7` baseline of this release): `remove_fillers`, `add_caption_track`
  and `auto_caption` go through `agent/timemap.py`, and footage is removed by
  transcript/source time only via `_cut_source_ranges`, which re-maps through
  the live EDL after every cut. Before: after `remove_silences`, `remove_fillers`
  removed 1 of 4 fillers and cut two stretches of real speech, and captions
  generated after cuts were laid at source time — 9.7 s past the end of the
  video, rendering over black. `export_srt/vtt/ass` deliberately stay
  source-timed.
- **Every argument a handler reads is advertised in its schema**
  (`tests/test_tool_schema_completeness.py` pins the census at zero). The
  day the rule was introduced, 13 tools read 22 arguments their schema never
  mentioned (`auto_reframe.subject_track`, `add_text.size`, `apply_lut.lut_path`,
  …); `add_music(gain_db)` was silently ignored. Unknown arguments are now a
  rejected plan, not a no-op.
- `add_music` gains `loop`: a bed shorter than the video is laid back-to-back
  until the extent is covered, only the last piece fading out.
- Caption and lower-third defaults sit higher on vertical canvases (the
  TikTok/Reels UI covers the bottom ~20 % and the right rail): `y` 0.76 /
  0.74 on 9:16, unchanged on 16:9. `SAFE_ZONES` is one table shared by the
  handlers and the verifier.
- Chat without a key no longer answers "ANTHROPIC_API_KEY is not set": it
  delegates to the same prompt service, and the startup message says the bar
  runs on local brains. `/dispatch` answers `409 prompt_running` while a prompt
  run holds the session lock (the desktop shows "Prompt running — wait or
  cancel"); chat history is written under a per-session history lock so the
  route's save and the run thread's finalize can land in either order.
- The feature report memo moved from `main.py` into `ai/features.py`
  (`cached_feature_report`), so the planner's facts never trigger the 2.2 s
  cold probe twice.

### Security
- Every plan — from any brain — is validated against an explicit allowlist
  before any dispatch: allow-listed tools only (`undo/redo`, `set_property`,
  `add_clip`, `add_sticker`, `add_effect`, `repair_*`, the export writers and
  a dozen more are denied), no unknown argument, enum whitelists for every
  free string that reaches the filesystem or the network (whisper models,
  Piper voices, fonts, LUT names, caption targets, preset names, transition
  names), numeric bounds, and **no model-authored file path** — a plan may
  read only files the session already offers.
- The prompt path never downloads a model or a voice without a **yes**;
  "no cloud key" is not "no network", the rule is no network without an
  answer. Model downloads are loopback-only routes (a paired phone cannot
  start a 4 GB download on the Mac). The Apple Intelligence helper is a
  network-free child process with a scrubbed environment and an empty working
  directory; its source is tripwired against `URLSession`, `Network`,
  sockets and `Data(contentsOf:)`, and `Package.swift` declares zero
  dependencies. The MLX loader sets `HF_HUB_OFFLINE=1` in-process and loads
  from a directory, never a repo id; its download path excludes `*.py`.
- The on-device brains receive no secrets, no absolute paths and no tool
  schemas; only the cloud brain may emit raw tool steps, and those are
  validated the same way.

### Docs
- `docs/PROMPT_EDITOR.md` (the ladder, the prompts, the questions, answering
  from the phone, transitions, environment), `docs/BENCHMARK.md` (ground
  truth, measurement rules, tiers, the claim procedure), `tests/benchmark/README.md`;
  README "Edit with a prompt, no API key"; CLAUDE.md sections for
  `agent/prompt/`, `brains/` + `tools/fm-planner/`, `agent/timemap.py` (two
  clocks) and `tests/benchmark/`; `.env.example` marks the key optional and
  documents `VAI_BRAIN`, `VAI_MLX_MODEL`, `VAI_PROMPT_CLOUD`.

## 0.6.0

### Added
- **iPhone companion app.** A real editing client for the phone: the timeline,
  the AI tool catalog, chat, import and export all live on iOS, while the Mac
  does every heavy thing — the 108 dispatch tools, the Claude agent loop, and
  every ffmpeg render. The phone never pretends it can do AI work on its own;
  each screen that needs the Mac says so, and the connection state is visible
  at all times.
- **LAN mode — opt-in, and off by default.** Reaching the Mac from a phone
  means putting the editor's HTTP socket on your local network, which is a real
  change in exposure, so it is a switch you turn on in the desktop app's Phone
  panel rather than something a release quietly does for you. With the switch
  off, the app binds `127.0.0.1` exactly as it always has and none of the code
  below runs. The setting is stored in `settings.json` alongside the app's logs
  (an environment variable would have been unreachable in a double-clicked
  `.app`) and changing it asks you to restart, because the bind address is
  chosen once at launch.
- **Pairing.** The Phone panel shows a QR code carrying a single-use, ten-minute
  claim code; scanning it gives that phone a bearer token. Devices are listed by
  name and can be revoked one at a time. Native media loaders, which cannot
  reliably carry an `Authorization` header, use a separate 60-second token that
  is accepted on media URLs only.
- **`GET /api/pair/*`** — `info`, `lan`, `new`, `claim`, `whoami`, `devices`,
  `revoke`, `media_token`. The four that change the security posture are
  loopback-only: a paired phone cannot pair a second phone.
- **An upload limit that actually exists.** `VAI_MAX_UPLOAD_BYTES` (4 GiB by
  default) is enforced from the declared `Content-Length` before a byte is read,
  again as a running total mid-stream, and once more as a free-space
  precondition — and is reported by `/api/health` so the phone can refuse an
  over-sized pick before spending your battery on it. There was previously no
  cap of any kind.

### Security
- **Closes an arbitrary file READ and an arbitrary file WRITE reachable through
  the tool dispatcher.** Six tool arguments — `import_srt.path`,
  `multicam.srcs`, `find_broll.bin` and the `path` of `export_srt`,
  `export_vtt` and `export_ass` — took a caller-supplied filesystem path and
  used it without ever consulting the path allowlist. Reachable from both
  `POST /api/sessions/{id}/dispatch` and `/mcp`. The read half could pull any
  file the user could read and hand it back through `/transcript`; the write
  half ran `mkdir(parents=True)` and then wrote to any destination, which on a
  path like `~/.zshrc` or `~/Library/LaunchAgents/` is code execution at the
  next login. `set_property`'s `value` was a seventh route to the same read.
  All seven now resolve through the allowlist, with a narrower list for writes
  than for reads, and `tests/test_path_guards.py` derives the set of
  path-typed arguments from `/api/tools` so a new tool cannot be added without
  a guard decision recorded for it.
- **Path restriction is forced on whenever the socket is not loopback-only.**
  Tools may then read inside the editor's workdir, `~/Movies`, `~/Downloads`
  and `~/Pictures`, and write only to the first three; `VAI_ALLOWED_ROOTS` adds
  more. Turning LAN mode off releases the restriction again, so nothing that
  worked on the desktop stops working.
- **DNS-rebinding defence, on every route in every posture.** The server
  answers only to a loopback name or a bare IP literal in `Host` — a page on
  `evil.com` that re-resolves its own name to your Mac's LAN address gets a 421
  instead of a session. It runs with LAN mode OFF too, which is the posture
  every default install ships in; only `/livez` and `/readyz` are exempt, so a
  monitor with its own `Host` header is unaffected. Requests labelled
  `Sec-Fetch-Site: cross-site` are refused, and API routes require an
  `X-VAE-Client` header, which a browser can only send after a preflight this
  app's CORS policy denies.
- **Closes an arbitrary file MOVE through `POST /api/load_project`.** A `.vae`
  is a zip, and its `manifest.json` named each bundled media file as a string
  that was joined onto the unpack directory — but `Path("/a/b") / "/etc/passwd"`
  discards the base entirely, so an absolute path in a hand-made manifest was
  enough to `shutil.move` any readable file on the Mac into a session's
  `uploads/imported/`, from where it could be downloaded over the media route.
  No traversal was needed and none of the tool-dispatcher path guards were in
  the way, because `load_project` never consulted them. Manifest entries that
  resolve outside the archive are now refused.
- **Closes an arbitrary `.json` write and read through the show templates.**
  `save_show_template.name` and `apply_show_template.name` were interpolated
  straight into a path; the most damaging target was the app's own
  `settings.json`, which parses as a template and comes back with an empty
  device list — silently unpairing every phone. Names are now a strict
  `[A-Za-z0-9._-]` leaf inside `presets/shows`. The guard table in
  `tests/test_path_guards.py` now derives `name` arguments too, since the
  reason this one was missed is that it was not called `path`.
- **Auth lockout** after 60 rejected requests a minute from one peer, with
  media paths excluded from the count — a single filmstrip paint is two dozen
  requests, and a phone with a stale token must not be able to lock itself out.

### Changed
- `/api/health` now also reports `max_upload_bytes`.
- `desktop.py` splits the bind address from the window URL: with LAN mode on it
  binds `0.0.0.0` while the webview and the voice-over bridge keep talking to
  `127.0.0.1`. A socket bound to a public interface requires authentication
  even if the LAN switch is later turned off — a toggle cannot un-bind a
  socket, so it must not be able to disarm the auth in front of one. That rung
  is now enforced by observation as well as by intent: a request that arrives on
  a non-loopback interface arms authentication and the path allowlist before it
  is answered, so a hand-rolled `uvicorn --host 0.0.0.0` with `VAE_LAN` unset is
  no longer wide open.
- `settings.json` is written `0600` inside a `0700` directory, through an
  unpredictable temp file rather than a predictable `.json.tmp`. It is the only
  record of which phones are paired; on a shared Mac the process umask had been
  making it world-readable.
- Every read-modify-write of the pairing settings now holds one lock end to
  end. Revoking a phone while it was still polling could be undone by the
  `last_seen` write from a request that had already loaded the old device list:
  the panel showed the phone gone and the phone kept working.
- The upload cap's second and third layers now cover `sticker_upload`,
  `subtitle_upload` and `load_project`, which still had raw read loops. The
  `Content-Length` middleware cannot see a body sent with
  `Transfer-Encoding: chunked`, so those three had no limit at all against one.

### Fixed (iPhone companion)
- A request that timed out was reported as a user cancellation, so a sleeping
  Mac left the connection bar reading "Connected" while edits, uploads and
  exports failed in silence. Timeouts are now their own failure and move the
  connection state.
- The automatic reconnect had no caller: `retrying`, `unreachable` and
  `throttled` were terminal states whose copy promised a recovery. One owner
  now drives them, and the app re-probes when it returns to the foreground.
- The phone asked for new ops with the last op's sequence number, but the Mac
  treats that value as a list index — so a fresh project reported its own
  `init` op as "the project changed while you were looking at it" every six
  seconds, permanently.
- Queue depth never worked: the phone asked `GET /api/jobs`, which is not a
  route. Every queued render said "waiting" instead of how many jobs were ahead.
- Signed media URLs are no longer cached past the life of the 60-second token
  inside them, and a thumbnail failure is confirmed as a real refusal before the
  clip's filmstrip is written off. Preview playback re-signs and resumes when a
  range request is refused mid-stream rather than stalling with no explanation.
- Cold start no longer holds a blank screen for up to twelve seconds waiting on
  a Mac that is not answering.
- The project list is virtualized and fetches posters only for the rows on
  screen.
- A `.local` address is refused up front with an explanation, because the Mac
  will always answer it with a 421; a Tailscale (100.64/10) address is now
  accepted with a note about the VPN instead of being reported as an attack.
- Both the desktop's Phone panel and the phone's Connect screen now say that
  the connection is not encrypted, which is the one exposure arming LAN mode
  adds that a user cannot see for themselves.

## 0.5.0

### Added
- **AI panel.** A second tab in the left pane ("Media | AI") lists every
  AI/auto-edit tool that was chat- or MCP-only (silence and filler removal,
  beat cuts, auto-reframe, shorts, diarization, translation, stems, upscale,
  stabilize, background removal, object erase, motion tracking, …) as a
  searchable catalog with generated forms, background jobs for the slow ones
  (Cancel where the backend can actually honour it — today `auto_caption`),
  tool-specific result views, and inline "what's missing and how to fix it"
  from the new `GET /api/features` (the `check_features` report over HTTP).
- **Safe-zone overlay.** Approximate TikTok / Reels / Shorts UI occlusion
  guides over the 9:16 preview so captions and lower-thirds land where the
  app's own chrome won't cover them.
- **`GET /api/tools` now reports `cancellable` / `reports_progress`** per tool,
  derived from the handler signature, so UIs stop promising a Cancel the
  backend can't deliver.
- **`POST /api/sessions/{sid}/subtitle_upload`** — stores a .srt/.vtt/.ass in
  the session so `import_srt` works from the browser without typing a path.

### Fixed
- **Composite AI tools (remove silences/fillers, beat cut, hook stack, templates) are now a single undo step.**
  Each of them used to commit once per internal edit, so one click needed a
  dozen undos to take back; `EDLStore.batch()` collapses the run into one op.
- **`build_app.sh` no longer overwrites `Video AI Editor.spec`.** PyInstaller's
  CLI mode wrote its generated spec into the repo root, clobbering the
  committed Windows spec on every macOS build. It now lands in
  `build/pyinstaller-spec/` (and `--add-data` sources are absolute, which
  `--specpath` requires).
- **Finder "Get Info" showed 0.0.0** for the macOS app: PyInstaller's CLI mode
  never sets a bundle version, so `build_app.sh` now stamps
  `CFBundleShortVersionString`/`CFBundleVersion` from `VERSION` before signing.
- **Version drift.** `pyproject.toml`, `package.json`, `__version__` and
  `uv.lock` had fallen to 0.3.7/0.1.0 while `VERSION` read 0.4.1; all agree
  now and a test keeps them that way. (0.4.x shipped without a changelog
  entry — its build-identity work is documented in CLAUDE.md "Release
  identity".)
- **Job errors no longer show a `RuntimeError:` prefix** in toasts and the AI
  panel.

### Notes
- Chat turns use Anthropic prompt caching: the tool schemas + static system
  prompt are one cached prefix and the trailing user/tool-result block is a
  second breakpoint, so each tool round re-reads the previous one from cache.
  The per-call live timeline context now travels as an extra block AFTER that
  breakpoint (inside the trailing user message, never in `system`), so a
  mutating tool round or a moved playhead no longer invalidates the cached
  conversation. Behaviour is identical; cached input tokens bill at ~10%.
  `VAI_PROMPT_CACHE=0` disables it; a backend that rejects `cache_control`
  disables it for the process automatically.
- `add_caption_track` and `get_transcript` now resolve the transcript the same
  way the subtitle exporters do: an imported `<session>/transcript.json` wins
  over Whisper's `ingest.json`, so "Import subtitles → Captions from
  transcript" lays the imported cues on the timeline.
- **Notarized macOS release pipeline.** `build_notarize.sh` takes
  `build_app.sh`'s ad-hoc-signed bundle, re-signs it inside-out with a
  Developer ID (every `.so`/`.dylib` first, then the bundle with
  `entitlements.plist`, no `--deep`), notarizes and staples the app, then
  builds, signs, notarizes and staples the DMG, and refuses to finish unless
  `spctl` reports `Notarized Developer ID` for both. `build_app.sh` is
  unchanged (still the ad-hoc dev path); `build_dmg.sh` gained `VAE_APP` /
  `VAE_DMG` overrides with the same defaults. `VAE_SIGN_IDENTITY=- … --sign-only`
  is a keychain-free dry run. Setup in CLAUDE.md → "Notarized release".

## 0.3.7

### Added
- **A freshly-recorded voiceover is now highlighted on the timeline.** When a
  voiceover finishes encoding, its clip is selected (selection ring) and briefly
  flashed (a white-over + blue border that clears after ~0.6s) so you can see
  where it landed. Generic `flashClip(id)` lives in the store for reuse.

### Notes
- The timeline is canvas-rendered (no DOM clip nodes), so the literal
  `scrollIntoView`/CSS-keyframe approach doesn't apply; the flash is drawn on the
  canvas instead. The VO track sits in the visible track stack, so no scroll is
  needed. The flash auto-clear is guarded by the flash timestamp so re-flashing
  the same clip restarts rather than self-cancels.
- Verified live by pixel-diffing the timeline canvas: the highlight draws over
  the clip during the window (Δ7.1M) and reverts exactly to baseline (Δ0) after.

## 0.3.6

### Fixed
- **Sticker/emoji picker now closes on outside click.** The picker stayed open
  until you toggled it again; it now dismisses when you click anywhere outside.
  The ref wraps the toggle button too, so clicking the toggle to close it
  doesn't fall through the outside handler and immediately re-open.
- Verified live: opens on toggle, closes on an outside mousedown, stays open
  when picking an emoji inside, and the toggle still closes cleanly.

## 0.3.5

### Added
- **Undo toast on delete.** Deleting a clip now shows a bottom-center toast —
  "Clip deleted" (or "N clips deleted") with an **Undo** button — that
  auto-dismisses after ~4s. The toast system gained inline action buttons, and
  the prompt fires from the store's dispatch on `ripple_delete` / `bulk_delete`,
  so it covers every delete path (keyboard, Properties Delete, timeline context
  menu) from one place. Undo uses the app's own backend undo.
- Toasts moved to bottom-center (out from under the chat panel) and gained an
  explicit ✕ dismiss.
- Verified live: delete (2→1 clips) → "Clip deleted" + Undo → click Undo →
  restored (1→2); toast auto-dismisses.

## 0.3.4

### Changed
- **Stale download links are now flagged "(outdated)" instead of vanishing.**
  Each export/`.vae` link is stamped with the history length (`ops.length`) it
  was made at. When you edit past that point, the link stays — you can still
  grab the last render — but shows **↓ MP4 (outdated)** / **↓ .vae (outdated)**
  in amber with a strike-through, so nobody ships a stale file by mistake.
  Re-exporting (or re-saving) refreshes the stamp and the link goes green again.
  Supersedes the 0.2.7 behavior, which hard-cleared the link on any edit.
- Verified live: export → fresh "↓ MP4" → an edit advances history → "↓ MP4
  (outdated)" (strike-through) → re-export → fresh again.

## 0.3.3

### Fixed
- **Voiceover recorder could get stuck / leave the mic hot.** The button now
  returns to idle (and releases the microphone) on every exit path:
  - `onstop` unconditionally tears down (stops the mic stream + elapsed ticker)
    and sets `recording = false` before any early-return, so a too-short or
    empty capture can't strand the UI — and a too-short capture now says so
    instead of failing silently.
  - If recording fails *after* the mic was granted (unsupported recorder, a
    throw past `getUserMedia`), the start handler releases the stream so the OS
    mic indicator doesn't stay lit looking like it's "still recording."
  - Added a `MediaRecorder.onerror` handler and mic release on unmount.
- Verified live: with mic permission denied, the button falls back to
  "🎙 Record voiceover" with an error instead of hanging on "Requesting mic
  access…".

## 0.3.2

### Fixed
- **Space didn't play/pause when a slider was focused.** The keymap skipped
  every focused `INPUT`, so after nudging a color/transform/zoom slider, Space
  was swallowed instead of toggling playback. The handler now only bows out for
  genuine **text-entry** fields (textarea, contentEditable, text/number/date/…
  inputs); for non-text controls (range, checkbox, button, select) global
  shortcuts win — above all Space → play/pause. It runs in the **capture phase**
  and `preventDefault`s, so the focused control doesn't also react (e.g. a
  button "clicking" on Space). A focused slider still keeps its own arrow / Home
  / End / PageUp / PageDown keys for stepping.
- Verified live: Space with a focused range slider plays; with a text field it's
  ignored (typing preserved); ArrowRight on a focused slider isn't hijacked;
  Space on the page still plays.

## 0.3.1

### Added
- **📂 Open .vae… inside the project switcher dropdown.** The `{project} ▾`
  switcher already had ＋ New project, the recent-sessions list (current one
  highlighted), and click-outside-to-close — it was just missing an in-dropdown
  way to open a saved `.vae` bundle (it existed only as a separate toolbar
  button). Added it next to ＋ New project, reusing the existing file picker.
- Verified live: dropdown shows current (highlighted) + ＋ New project + 📂 Open
  .vae… + recents; switching projects works; opens on click and closes on
  outside mousedown.

## 0.3.0

### Added
- **Direct sticker manipulation on the canvas.** Stickers are now interactive,
  not just rendered:
  - Click a sticker to select it (canvas hit-testing, rotation-aware).
  - Drag the body to move it; drag a corner handle to resize. A dashed bounding
    box + corner handles show on the selected sticker. Live feedback during the
    gesture; the server is hit once on release (`set_clip_transform`).
  - The Properties panel gains a **Sticker** inspector — X, Y, Scale, Rotation,
    Opacity, Start and Duration — all editable, with the canvas and panel kept
    in sync.
  - New `StickerLayer` owns sticker drawing + interaction; `TextLayer` is now
    text-only. Shared geometry/keyframe helpers live in `lib/overlay.ts` so the
    hit-box always matches the painted glyph.
- **Backend:** `set_clip_transform` now works on stickers (not just media
  clips); new `set_clip_timing` sets start/end on overlay clips (stickers/text).
- Verified live: insert → select → drag (Δx/Δy exact) → corner-resize (1×→2×) →
  edit Duration (3s→1.5s, start preserved), all committing to the EDL. Backend
  suite 259 passed.

> Stickers already inserted at the playhead with a 3-second default and rendered
> on a dedicated Stickers track; this release makes them directly editable.

## 0.2.9

### Added
- **Live value readouts on the Color sliders.** Brightness, Contrast,
  Saturation, Temp and Tint now show their current value (right-aligned, like
  the Speed and Audio sliders), updating live as you drag — the slider is
  controlled now, while still committing to the server only on release. Formats:
  Brightness `±0.00`, Contrast/Saturation `0.00×`, Temp `±N` (−100..+100), Tint
  `±0.00`. (Transform sliders already showed `scale 1.00 / rotation 0° /
  opacity 1.00`.)
- Verified live: each color slider shows the correct initial value, the readout
  tracks a drag (e.g. Brightness `+0.30` / `−0.24`), and the 280px panel stays
  overflow-free.

## 0.2.8

### Fixed
- **Properties panel overflowed its fixed-width column.** Several issues, all in
  the right sidebar:
  - The In/Out and Fade-in/out rows used flex with non-shrinking number inputs;
    they're now a `1fr 1fr` grid with `min-width: 0` inputs so they stay equal
    and fit.
  - `.props input { width: 100% }` was stretching the *Mute* checkbox and
    blowing out its label — the rule now excludes checkbox/radio.
  - Long filenames (`a_b_c.normalized.mp4`) and clip ids are unbreakable tokens
    that painted past the panel edge; `overflow-wrap: anywhere` lets them wrap.
  - Flex rows (sliders, Transform x/y, action buttons) wrap instead of
    overflowing; the sidebar is `min-width: 0` + `overflow-x: hidden`.
- Verified live by measuring the DOM: the Timing row is a grid of two equal
  125.5px columns, all inputs are `border-box`, and the 280px sidebar reports
  **zero** horizontal overflow with a clip selected.

## 0.2.7

### Added
- **Export progress modal.** Export now opens a modal with a **live progress
  bar** (real ffmpeg progress, not a fake animation), an **ETA**, a **Cancel**
  button, and a success **toast** + auto-download on completion. Progress comes
  from ffmpeg `-progress` streamed into the background job; the bar shows an
  indeterminate sweep until the first real sample lands.
- **Cancellable exports.** `POST /api/jobs/{id}/cancel` terminates the running
  ffmpeg and marks the job `cancelled` (no orphan processes, no partial files —
  the atomic `.part` write is cleaned up). The job system gained cooperative
  `set_progress` / `cancel_event` hooks, injected only into job fns that opt in.
- **Toast notifications** (`toast.success/error/info`) with a bottom-right host.

### Changed
- A new edit clears the previous export's stale **↓ MP4** download link, so the
  navbar never offers an out-of-date render.

### Notes
- Verified live: progress climbed 9 → 39 → 69%, ETA counted down ~10s → ~2s,
  Cancel produced a "cancelled" toast and closed the modal, completion fired the
  success toast + download, and the stale link cleared after an edit. Backend
  suite 258 passed; preview render path is untouched (progress is export-only).

## 0.2.6

### Changed
- **Narrow-window strategy: hold-and-scroll instead of reflow.** A pro 3-pane
  editor needs width, so rather than collapsing the panes into a single column
  on small windows, the layout now holds a **900px minimum width** and scrolls
  horizontally (`#root`) below that. The toolbar stays on one line and scrolls
  internally. A dismissible banner appears under 900px nudging the user to a
  wider window. This supersedes the 0.2.1 reflow (which stacked panes vertically
  under 1024px). Modal dialogs keep their `min(…, 92vw)` sizing.
- Verified live across widths: at 1280px the 3-pane grid is intact with no
  banner and no scroll; at 700px the layout holds at 900px (panes don't
  collapse), `#root` scrolls horizontally, and the banner shows and dismisses.

## 0.2.5

### Fixed
- **Playback froze when the preview couldn't decode.** The playhead clock was
  driven by `requestVideoFrameCallback`, which only fires when the `<video>`
  actually decodes a frame — so an undecodable preview stopped the timeline, the
  red playhead line, and the time readout even though playback was "running."
  The clock is now an independent rAF wall clock: it follows the video's
  `currentTime` for exact A/V sync when the video is advancing, and free-runs on
  wall time when the video stalls or can't render — clamping to the clip
  duration and stopping cleanly at the end. Per-frame work is wrapped so a
  render failure is non-fatal.
- Verified live: normal playback advances then freezes on pause; with the video
  deliberately sabotaged so it can never decode a frame, the playhead still
  advances (free-run) instead of freezing.

## 0.2.4

### Fixed
- **Preview scrubbing broke on torn/half-written files ("mp4box invalid box").**
  Two root causes:
  1. *Non-atomic renders.* ffmpeg wrote preview/export output straight to the
     served path; `-y` truncates first, so a render that was killed or fetched
     mid-write left a 0-byte or partial `.mp4` that mp4box rejected. Renders now
     go to a temp sibling and `os.replace()` into place atomically — readers
     only ever see a complete file or none. The serving endpoint also treats a
     0-byte leftover as missing and re-renders.
  2. *No demux fallback.* When mp4box/WebCodecs can't parse a file (edit lists,
     unusual codecs, a genuinely odd box), the `FrameScrubber` now falls back to
     a hidden `<video>` it seeks via `currentTime` and paints to the canvas on
     `seeked` — so scrubbing keeps working instead of silently disabling. The
     fallback `<video>` carries no `src` until mp4box actually fails (no
     double-fetch on the happy path).
- Verified live: atomic render emits a valid `ftyp` file with zero `.part`/
  0-byte leaks, and the fallback paints a real frame (100% non-black) from the
  served preview.

## 0.2.3

### Added
- **`ANTHROPIC_API_KEY` for the shipped app.** The dev server reads the repo
  `.env`, but a double-clicked `.app` can't (its project root is inside the
  read-only bundle). Config now also loads `.env` from a stable user-writable
  location — `~/Library/Application Support/Video AI Editor/.env` — so the
  in-app Claude chat works from the DMG. Repo `.env` still wins for dev; the
  shell env still wins over both. Keys are never bundled or committed.

## 0.2.2

### Fixed
- **The shipped `.app` couldn't find `ffmpeg` — so import, preview, scrubbing,
  export and captions all failed when launched by double-click.** A
  Finder-launched macOS app inherits launchd's minimal `PATH`
  (`/usr/bin:/bin:/usr/sbin:/sbin`), which excludes `/opt/homebrew/bin` where
  `ffmpeg`, `ffprobe` and `whisper-cli` live — every shell-out died with
  `FileNotFoundError: 'ffmpeg'`. (Running from a terminal masked it, because the
  shell supplies Homebrew's `PATH`.) The app now appends the common Homebrew /
  MacPorts / `~/.local/bin` locations to `PATH` at startup, so those binaries
  resolve no matter how it's launched. Verified end-to-end under a simulated
  launchd environment: upload, preview, and waveform all succeed.

> Note: this resolves the binaries on a machine that already has them
> (e.g. `brew install ffmpeg whisper-cpp`). A fully self-contained build that
> bundles `ffmpeg` for machines without Homebrew is tracked separately.

## 0.2.1

### Fixed
- **Responsive layout for iOS / iPadOS Safari.** The 3-column desktop grid had a
  hard ~1292px content floor that spilled off-screen on phones and tablets
  (902px overflow on iPhone, 458px on iPad). Below 1024px the editor now reflows
  to a single scrolling column — preview, then a horizontally-scrollable
  timeline, then media / properties / history — with zero horizontal overflow.
  The toolbar scrolls internally instead of widening the page; the Help modal is
  now `min(540px, 92vw)`.
- Verified clean across a 5-environment matrix: WebKit @ iPhone portrait +
  landscape, iPad, macOS Safari desktop, and Chromium desktop.
- **macOS `.dmg` packaging shipped stale code.** PyInstaller was reusing a
  cached analysis, so the bundle carried an old `main.py` (no `/api/version`)
  and an old frontend build. Builds now run `--clean`, the spec bundles the
  `VERSION` file, and `Info.plist` (`CFBundleShortVersionString`) tracks
  `VERSION` automatically. The shipped `.app` now reports 0.2.1 at runtime and
  in Finder, verified end-to-end from the mounted DMG volume.

## 0.2.0

The "make it real" release — everything since the first editable timeline.

### Added
- **Customizable keyboard shortcuts** with CapCut / Premiere Pro / Final Cut Pro
  presets, click-to-rebind, conflict detection, persisted to localStorage (⌨ in
  the top bar).
- **MCP server** at `/mcp` — drive the editor from Claude Code / Cursor / Codex.
- **Local CLIP visual search** (`search_media`) — find footage by visual content.
- **Best-quality Hindi/English auto-captions** (`auto_caption`, Whisper large-v3
  on Metal) with broadcast-grade cue formatting.
- **3-axis hook stack** (`apply_hook_stack`) + audit scoring.
- **~45-transition catalog** (was 7), all rendering correctly.
- **Full-window drag-and-drop import** — drop a video anywhere.
- **Parallel chunk rendering** (3–4× faster multi-clip cold renders).
- **Client-side live transform preview** (no render storm while dragging).
- **macOS `.app` + `.dmg`** build (AI-bundled), app versioning.

### Fixed
- Video import / preview failures return clean 422s instead of bare 500s.
- Silent-source videos (no audio track) now normalize + render.
- Corrupt chunk cache auto-detects + rebuilds.
- Bundle path resolution (frontend/presets/fonts/workdir) for the shipped `.app`.
- Whisper Hindi auto-detect (`-l auto`), 48 kHz preview audio, sharper preview.
- `_STORES` race + LRU bound; per-session data in a user-writable dir.

## 0.1.0
- Initial editable timeline, chat agent, ingest, render, export.
