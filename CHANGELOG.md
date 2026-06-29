# Changelog

All notable changes to Video AI Editor. Versioning follows the `VERSION` file
at the repo root, surfaced at `/api/version` and in the editor's top bar.

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
