# Changelog

All notable changes to Video AI Editor. Versioning follows the `VERSION` file
at the repo root, surfaced at `/api/version` and in the editor's top bar.

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
