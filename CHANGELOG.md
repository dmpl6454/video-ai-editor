# Changelog

All notable changes to Video AI Editor. Versioning follows the `VERSION` file
at the repo root, surfaced at `/api/version` and in the editor's top bar.

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
