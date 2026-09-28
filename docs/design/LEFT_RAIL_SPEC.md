# Wave D: a left tool rail replaces the overflowing top bar (final spec)

**Status:** final. It is the draft with all 20 critique findings applied.
**Mock:** `rail-mock.html`, in this folder. It is rebuilt to this spec.
**Screenshots:** `rail-mock-{1024x768,1280x800,1440x900,1920x1080}.png`, plus these:
- `rail-mock-worst-1024x768.png` and `rail-mock-worst-{900,1280,1440}-topbar.png`: every top-bar item shown at once.
- `rail-mock-collapsed-1024x768.png`: both side panels closed.
- `rail-mock-tooltip-1024.png`
- `rail-mock-{text,captions}-1440x900.png`

**Probes:** `rail-mock-probe.py` (top-bar fit at 8 widths) and `rail-mock-probe-a11y.py` (names, focus, grid, tooltip, deep links, menu). Both use the Playwright headless shell and the repo `.venv`.
**Scope of this pass:** read-only on the repo. No repo file was edited. No pytest and no vite build were run.
**Line references:** they were read on 2026-09-26, in a tree that wave C is still editing. Treat them as approximate and re-check them in R0.

---

## 0. The design in one paragraph

The top bar keeps three jobs, in a `1fr auto 1fr` grid:
- **Left:** which project I'm in, and what is running. The running part is a new **activity chip** carrying recording Stop and captions Cancel.
- **Centre:** what canvas I'm making (Ratio, including the safe-zone overlay).
- **Right:** getting it out (Save, Open, the result links, Export).

Everything that *adds content* moves to a **left tool rail**: 64 px wide, 48 px below 1280. Each item is an icon over a label, in CapCut's order: **Media · Audio · Text · Stickers · Effects · Transitions · Captions · AI**. The rail drives one tool panel.
- **Help, Customize shortcuts and Settings** sit at the rail foot.
- **Export** stays the right-most primary control and is never clipped down to the 900 px floor.
- **The Prompt bar** stays above the picture.
- **Chat** stays docked with the Inspector on the right.
- The `⋯` menu, the separators and the QA-012 flex-shrink rules are deleted.

Rail items stay `role="tab"`. Their accessible names are exactly the label ("Media", "Transitions", "AI" and so on), with no tooltip text mixed in. The existing `get_by_role("tab", name=…)` helpers keep working. The **16 selectors that do break** are listed in §8 and are updated in the same phase that breaks them.

### How each critique finding was resolved

| # | Finding | Resolution | Where |
|---|---|---|---|
| H1 | Panel chords are dead inside `[data-keymap-ignore]` | A new `Command.scope` field. `'global'` commands run inside the ignore scope; the text-entry guard is kept. `'anywhere'` (F6 only) also runs in text fields | §4.2, R4 |
| H2 | Focus is stranded when a panel hides | Focus rescue: if focus was inside the outgoing region, it moves to the owning control (the selected rail tab, or the right rail's expand button). Otherwise focus is left alone | §5.3, R1 |
| H3 | Collapsing both panels leaves a dead column | Column **variables** per state, not whole templates. Verified: `48px 0px 0px 940px 0px 36px` | §1.1 |
| H4 | Tooltips are clipped and pollute names; Esc does nothing | One portalled `aria-hidden` tooltip: 400 ms delay, Esc dismisses it, you can hover onto it, and no `::after` sits on any role. Verified: exact-name match = 1 for all 8 tabs; tooltip at x 55–296, outside the 48 px rail | §2.6 |
| H5 | Recording Stop and caption Cancel are hidden with the panel | An always-rendered **activity chip** in the top bar (`role=status` live region, throttled). The rail dot stays as a secondary cue | §2.8 |
| H6 | "Nothing ever clips" was false | Measured **density steps** (`useTopBarFit`). Only the project name truncates. Verified worst case at 900, 1024, 1099, 1100, 1280, 1439, 1440 and 1920: no overflow, Export's right edge = viewport − 12, and no clipped activity button | §1.4 |
| H7 | Responsive widths can never apply | `leftW`/`rightW` are `number \| null` in a new `layoutStore`. Null means CSS defaults. A drag is seeded from the measured width | §6.1 |
| H8 | Mock dropped or invented Text and Captions content | The real `TEXT_STYLE_PRESETS`, the four TextTool templates, `TARGETS` and `SPEEDS` with their hints, the download badge and the consent dialog | §2.5, mock |
| H9 | Test breakage under-counted | The full list is in §8, each update tied to the phase that causes it | §8 |
| M1 | Help's name vs its dialog | The button **keeps** the name "Keyboard shortcuts" (the dialog's title) and `aria-keyshortcuts="?"`. Its tooltip says "Help and keyboard shortcuts" | §2.4 |
| M2 | The collapsed right panel loses a name | The collapsed rail's first button keeps "Show the Inspector and Chat panel" | §2.9 |
| M3 | MP4 drawn as `<a>` | It stays a `<button>` → `downloadExport()`. `.vae` stays the existing `<a download>` + `onSavedLinkClick` | §3 |
| M4 | Mount-contract details | The disclosure state is removed from StickerPanel and EffectsPanel. `active = leftOpen && tab === id`. The state moves to the store. A "Panels" category is added | §2.7 |
| M5 | Deep-link rows | One catalogue tool id per row, plus a "All ‹group› tools (n)" link. The jump clears the search. There is a back chip. Status is mirrored. Media and Effects get links. The catalogue has 42 tools | §2.5 |
| M6 | Invalid Ratio menu | `role=group` + `aria-label` per section, with visual headings `aria-hidden`. Safe-zone items are named "TikTok safe zone" and so on. Every pick closes the menu | §2.10 |
| M7 | Ratio not centred | The top bar is a `minmax(0,1fr) auto minmax(max-content,1fr)` grid. Verified centred at 1024 (512), 1440 (720) and 1920 (960); "Applying" no longer moves it | §1.4 |
| L1 | CapCut parity | Filters live in Effects ("Filters · LUT looks", tooltip). Adjustment stays in the Inspector's Color section. Ratio stays in the top bar and the rail stays vertical: both are deliberate, see §9 | §9 |

---

## 1. Layout

### 1.1 Grid

```css
.app {
  --rail-col:   var(--rail-w);                                   /* 64px; 48px below 1280 */
  --left-col:   minmax(var(--left-min /*180*/), var(--left-w));  /* tool panel */
  --lsplit-col: 6px;
  --rsplit-col: 6px;
  --right-col:  minmax(var(--right-min /*220*/), var(--right-w));/* Inspector | Chat */
  display: grid;
  min-width: 900px;                                              /* 48+180+6+440+6+220 = 900 */
  grid-template-rows: var(--topbar-h /*44px*/) minmax(0, 1fr) auto;
  grid-template-columns: var(--rail-col) var(--left-col) var(--lsplit-col)
                         minmax(var(--center-min /*440*/), 1fr) var(--rsplit-col) var(--right-col);
  grid-template-areas:
    "topbar topbar topbar  topbar topbar topbar"
    "rail   left   lsplit  center rsplit right"
    "foot   left   lsplit  center rsplit right";   /* rail foot = own grid item, last in DOM */
}
.app.left-collapsed  { --left-col: 0px;  --lsplit-col: 0px; }
.app.right-collapsed { --rsplit-col: 0px; --right-col: var(--right-rail-w /*36px*/); }
```

- **Why each state changes only its own variables (H3).** Two whole templates of equal specificity meant the later one won. With both panels collapsed, the tool panel's 220 px column came back empty.
- **Why `minmax(min, var)` plus `minmax(440px, 1fr)`.** In the grid algorithm's "maximize tracks" step, free space is measured with the fr track at its 440 px base. The side panels can grow to their max only out of what is left. So "the centre never drops below 440" is a CSS guarantee, not JS arithmetic.
- **Collapsed tool panel.** The `<section>` gets `hidden` (display: none). Its React subtree stays mounted, so VoRecorder's MediaRecorder survives (§2.7).
- **Right panel.** It collapses to a 36 px rail. Today that is `RIGHT_RAIL_W = 28` in `App.tsx`; it becomes 36 so it can hold three 28 px buttons.
- **Tokens.** Every new token is in the mock's `PROPOSED` block: `--rail-w`, `--rail-item-h`, `--rail-foot-btn`, `--left-w`, `--right-w`, `--left-min`, `--center-min`, `--right-min`, `--right-rail-w`, `--topbar-h`, `--panel-head-h`, `--dur-fast`, `--dur-normal`, `--ease-out-expo`. Existing tokens are unchanged.
- **The `hidden` attribute.** It must always win. Add `[hidden] { display: none !important; }` to `styles.css`. The mock found three components whose own `display` silently overrode `hidden`.

### 1.2 Column widths by breakpoint (verified in the mock)

These are first-run defaults from CSS media queries. A width the user has dragged, stored in `vai.leftW` / `vai.rightW`, wins over them. The clamp is [180, 640], and further clamped so the centre stays at least 440.

| Viewport | Rail | Tool panel | split | **Centre** | split | Right | Rail label |
|---|---|---|---|---|---|---|---|
| 900×724 (floor) | 48 | 180 | 6 | **440** | 6 | 220 | icon only |
| 1024×768 | 48 | 220 | 6 | **484** | 6 | 260 | icon only (label visually hidden, still the name) |
| 1280×800 | 64 | 240 | 6 | **684** | 6 | 280 | icon + 10.5 px label |
| 1440×900 | 64 | 280 | 6 | **804** | 6 | 280 | icon + label |
| 1920×1080 | 64 | 320 | 6 | **1204** | 6 | 320 | icon + label |

- Tool panel collapsed at 1024: 48 · 0 · 0 · **710** · 6 · 260 (1024 − 48 − 6 − 260; measured in Chromium and WebKit, review RD1).
- Both panels collapsed at 1024: 48 · 0 · 0 · **940** · 0 · 36.
- The panel default grows with width because Stickers, Effects and Transitions are now tile grids rather than disclosures. At 280 px the grid holds three tiles of at least 72 px.

### 1.3 Vertical budget

| Viewport | Top bar | Prompt | Preview | split | Timeline |
|---|---|---|---|---|---|
| 1024×768 | 44 | 46 | 392 | 6 | 280 |
| 1280×800 | 44 | 46 | 424 | 6 | 280 |
| 1440×900 | 44 | 46 | 524 | 6 | 280 |
| 1920×1080 | 44 | 46 | 704 | 6 | 280 |

- The Prompt bar is auto height, capped at 38vh, as today.
- **Rail height:** 8×52 + 6 + the foot (3×36 + gaps + padding ≈ 129) ≈ 551 px at ≥1280, and 8×44 + 6 + 129 ≈ 487 px below 1280. Both fit in 724 px, the shortest supported window.
- `.rail` scrolls itself if it ever has to. The foot is its own grid row, so it never scrolls away.

### 1.4 Top bar: grid, density steps and measured worst case

```css
.topbar     { display: grid; grid-template-columns: minmax(0,1fr) auto minmax(max-content,1fr); column-gap: 12px; }
.tb-left    { display: flex; gap: 8px; min-width: 0; overflow: hidden; }   /* brand · wordmark · project · Applying · activity */
.tb-center  { justify-self: center; }                                        /* Ratio */
.tb-right   { justify-self: end; display: flex; gap: 8px; padding-right: 12px; } /* .topbar-pinned: Save · Open · .vae · MP4 · error · Export */
```

- **Centring.** The Ratio trigger is exactly centred whenever the right cluster fits in half the bar, and "Applying" appearing on the left never moves it (M7). If the right cluster is wider than half, the right column takes its `max-content` and Ratio shifts left rather than anything clipping.
- **What can shrink.** The project chip (`min-width: 80px`, ellipsis) is the only item that truncates. Everything else is `flex-shrink: 0`.

**Density steps (H6).** `useTopBarFit` sets `data-density` on the header. It starts from the viewport's baseline and steps up until the left group stops clipping and the bar stops overflowing. Only the project name may truncate.

| Step | What changes | Baseline at |
|---|---|---|
| 0 | everything shown: wordmark, `· 1080×1920 · 30 fps`, "(outdated)" words, error text up to 220 px, Save/Open words, "Rec"/"Captions" words | ≥ 1440 |
| 1 | wordmark visually hidden (it stays the page's `h1`); ratio facts hidden (they are still in the trigger's name, its tooltip and the menu footer); error text max 180 | 1280–1439 |
| 2 | "(outdated)" becomes a 6 px `--warn` dot, with the words moved into the link's accessible name; error text max 140 | 1100–1279 |
| 3 | Save/Open icon-only (names kept); error chip icon-only, 28 px, with the full message in its `aria-label` and tooltip; "Applying" becomes a spinner with sr-only text; project chip max 160 | < 1100 |
| 4 | activity words hidden ("● 0:12 ■", "⟳ 42% ✕"); only reached when content demands it | never (content-driven) |

The hook is a `useLayoutEffect` in `components/topbar/useTopBarFit.ts`:
- It re-runs on window resize and when its inputs change: activity, stale flags, error, project name and pending ops.
- The loop is bounded at 5 steps and runs before paint, so nothing flickers.
- On resize it re-starts from the baseline, which gives it hysteresis.
- The step logic is a pure function, `nextDensity(base, fits)`, so it can be unit-tested.

**Measured in the mock.** The worst case is a 60-character project name, Applying, recording and captions running, both links outdated, and a long export error:

| Viewport | Density (normal / worst) | Top-bar overflow | Export right edge | Clipped activity buttons | Project chip (worst) |
|---|---|---|---|---|---|
| 900 | 3 / 4 | 0 | 888 | none | 113 px |
| 1024 | 3 / 4 | 0 | 1012 | none | 160 px |
| 1100 | 2 / 3 | 0 | 1088 | none | 86 px |
| 1280 | 1 / 2 | 0 | 1268 | none | 100 px |
| 1440 | 0 / 1 | 0 | 1428 | none | 121 px |
| 1920 | 0 / 0 | 0 | 1908 | none | 240 px |

- Every top-bar button is 28 px tall. Text is 12 px, except the project chip, which is the `pill` class at 11 px. So `test_top_bar_controls_share_one_height_and_type_size` keeps passing (§8).
- Fixed banners (ConnectionBanner, MediaToolsBanner) still clear the 44 px bar.

---

## 2. The rail and its panels

### 2.1 Structure

```html
<nav class="rail" aria-label="Tools">
  <div role="tablist" aria-orientation="vertical" aria-label="Tool panels"> 8 × <button role="tab"> </div>
</nav>
<section class="panel" id="tool-panel" aria-labelledby="panel-title" [hidden when collapsed]> 8 × <div role="tabpanel" hidden?> </section>
…centre…right…
<nav class="rail-foot" aria-label="Help and settings"> 3 × <button> </nav>   <!-- last in DOM -->
```

DOM order is rail, tool panel, centre, right, foot. So Tab from a rail tab goes into its panel, as the APG tabs pattern expects. Verified: the next stops are "From iPhone" (only with the flag), then "Hide the tool panel", then the dropzone.

### 2.2 Items (`components/rail/railModel.ts`: one list drives tabs, commands, the panel title and tooltips)

| # | id | Label = name | Icon (lucide 1.48) | Chord | Tooltip | Panel |
|---|---|---|---|---|---|---|
| 1 | `media` | Media | `Film` | ⌥1 | Footage, photos and the project bin | MediaBin (minus audio/stickers/effects) |
| 2 | `audio` | Audio | `Music` | ⌥2 | Music, voiceover and the mix | AudioPanel |
| 3 | `text` | Text | `Type` | ⌥3 | Titles, styles and templates | TextPanel |
| 4 | `stickers` | Stickers | `Sticker` | ⌥4 | Emoji and stickers | StickerPanel (always open) |
| 5 | `effects` | Effects | `Sparkles` | ⌥5 | Filters, LUT looks and effects | EffectsPanel (always open) |
| 6 | `transitions` | Transitions | `SquareSplitHorizontal` (new) | ⌥6 | Transitions for a cut | TransitionsPanel, unchanged |
| 7 | `captions` | Captions | `Captions` | ⌥7 | Auto captions and subtitles | CaptionsPanel |
| 8 | `ai` | AI | `WandSparkles` (new) | ⌥8 | Every AI tool (42) | AiPanel, unchanged |

**Rail foot** (36×36 icon buttons, in this order):

| Button | Name | Icon | Keys |
|---|---|---|---|
| Help | **"Keyboard shortcuts"** (unchanged: it opens the dialog titled "Keyboard shortcuts"; tooltip "Help and keyboard shortcuts") | `CircleQuestionMark` (`help`) | `aria-keyshortcuts="?"` |
| Customize | "Customize keyboard shortcuts" | `Keyboard` | ⌥⌘K |
| Settings | "Settings" | `Settings` | ⌘, |

**New `ICONS` names** (`lib/icons.ts`): `SquareSplitHorizontal`, `WandSparkles`, `PanelLeftClose`, `SlidersHorizontal` (Inspector), `MessageSquare` (Chat), `Clapperboard` (brand mark), `Search`, `Square` (Stop), `Smartphone`, `ChevronLeft`. Path data for all of them is in the mock's icon table, taken from `lucide-react@1.48.0`.

### 2.3 Item anatomy and states

- **Size.** 52 px tall (44 px in the compact rail), full rail width. A 32×26 chip holds the 16 px icon. The label below is 10.5 px, weight 500.
- **Idle:** `--text-dim` on `--bg-0`.
- **Hover:** chip `--bg-2`, text `--text`.
- **Pressed:** chip `scale(.94)`.
- **Selected:** chip `--bg-3`, text `--text`, plus one 3×28 px `--accent-2` indicator on the left edge. The indicator slides by `transform` only.
- **Selected but collapsed:** the chip goes transparent with a 1 px `--line` inset, and `aria-expanded="false"`. The indicator stays at full strength, which keeps 3:1 non-text contrast.
- **Focus:** a 2 px `--focus-ring` outline around the chip.
- **Motion:** only colour, `transform` and `opacity` animate. Under `prefers-reduced-motion`, `--dur-*` drop to 0 and the panel/menu/tooltip entrance animations are off.

**Live-state dot** (a secondary cue; the activity chip in §2.8 is the primary one):
- It is 7 px, at the chip's top-right, static (no pulse).
- `--accent` on **Audio** while recording.
- `--accent-2` on **Captions** while transcribing, and on **AI** while a tool run holds the session lock (`lib/promptStore.ts`).
- Its meaning goes into `aria-describedby`, pointing at sr-only text such as "Recording in progress". It is not in the name, and it does not use `aria-description`, whose WebKit support is uneven.

### 2.4 Collapse and selection behaviour

| Input | Result |
|---|---|
| Click another item, or its ⌥ key | Select it and open the panel |
| Click the **active** item, or its ⌥ key again | Toggle the panel collapsed / expanded |
| ⌥\ | Toggle the panel |
| Panel header `PanelLeftClose` button ("Hide the tool panel") | Collapse; focus goes to the selected rail tab |
| ↑ / ↓ / Home / End with focus in the rail | Move and activate (automatic activation), opening the panel if it is collapsed |
| Enter / Space on the active tab | Toggle, the same as a click |

Persistence:
- `vai.leftTab` gains 5 values. The old `media`, `transitions` and `ai` stay valid, so no migration is needed. An unknown value falls back to `media`.
- `vai.leftOpen` is new and defaults to `true`.

### 2.5 Panel contents: real content only (H8)

**Panel header (36 px):**
- An `h2` with the panel name at 13 px / 600.
- The chord hint (`⌥3`) in `--text-faint`, `aria-hidden`; the tab carries `aria-keyshortcuts`.
- An optional header action. Media has **From iPhone** (`Smartphone`), rendered only when `phone_pairing` is on. It has `aria-label="From iPhone"`, and its word hides when the panel is under 260 px (`@container toolpanel`).
- The collapse button.

This header replaces the old 34 px uppercase tab strip. MediaBin's hidden `<h2>Media</h2>` and the `aiPanel.css` rule that hid it are deleted.

| Panel | Contents |
|---|---|
| **Media** | The dropzone. "Add imports to the timeline". Upload well and rows. The empty hint. Media rows: thumb · name · meta · add · remove · Relink, with `data-media-row` and `data-keymap-ignore` kept. *As built (Final QA r3):* the rows, and the Transitions panel, are `data-keymap-own` scopes now (a row keeps Enter, Delete and Backspace; the panel its grid keys: arrows, Home/End, Backspace/Delete, [ ]): as ignore scopes they swallowed ⌘Z, J/K/L and N, so ⌘Z right after "+" or a transition tile did nothing. Then **Find & search:** rows "Search footage" (`search_media`) and "Find b-roll" (`find_broll`), plus "All Find & search tools (5)". |
| **Audio** | "Add music…" (moved from MediaBin). **VoRecorder**: Record voiceover · Import audio file as voiceover, and while recording the timer and **Stop**. "Music on the timeline" (MusicPanel: Vol, Duck under speech). **AI audio:** Reduce noise (`noise_reduce`), Isolate vocals (`vocal_isolate`), Isolate instrumental (`instrumental_isolate`), AI voiceover (`tts_voiceover`). |
| **Text** | A wrapper `div[data-text-presets]` whose first button is **Add text at playhead** (`--accent-2-fill`, ⌥T). The field "Text, #hashtag or @handle". **Styles** grid from `TEXT_STYLE_PRESETS`: Title box, Subtitle band, Yellow pop, Side label, Quote, Neon. **Templates** from TextTool `PRESETS`: "3 · 2 · 1", "Callout →", "#Hashtag", "@Handle". The last two are disabled until the field has text (`needsField`), with the existing titles and a one-line hint. **AI text & brand:** Hook overlay, Suggest hooks, Lower third, Brand kit, plus "All Text & brand tools (7)". |
| **Stickers** | StickerPanel content, always open: search, skin tones, Recent, groups. `.sticker-picker` is kept. |
| **Effects** | EffectsPanel content, always open: the target line; **Filters · LUT looks** (names from `list_luts`) with Look intensity; the Effects grid (Blur, Sharpen, Vignette, Grain, Vintage, VHS, Glow, RGB Split, Flip H, Flip V); applied chips. **Cutout & effects (AI):** Remove background, Chroma key, plus "All Cutout & effects tools (4)". |
| **Transitions** | TransitionsPanel, unchanged. |
| **Captions** | **Generate captions** (primary). While running: a progress card with %, ETA and **Cancel**, which becomes "Stopping…". **Language** is a radio list from `TARGETS`, each with its hint: As spoken, English, हिंदी Hindi, Hinglish, Español. **Speed** is a radio list from `SPEEDS`: Best quality, Fastest. Fastest carries its "1.6 GB download" badge while its model is missing, and the existing download-consent dialog is unchanged. "Caption style…" opens CaptionStylePanel, unchanged; it also still opens from a caption cue. **Captions & speech (AI):** Translate captions, Detect speakers, Name speakers, Import subtitles, Export .srt, plus "All Captions & speech tools (10)". |
| **AI** | AiPanel, unchanged: all 8 groups and 42 tools, the search box ("Search 42 AI tools") and the forms. When the user arrived through a deep link, a back chip "‹ Captions" (or the origin panel's name) sits above the search. |

**Deep-link rows (M5).**
- **One target per row.** Each row names exactly one catalogue tool id from `lib/aiCatalog.ts`, and its label comes from the catalogue. A "All ‹group› tools (n)" link targets the group heading.
- **What a click does:**
  - selects AI and clears the AI search box;
  - opens the group and expands that one card in the existing AiPanel (it never mounts a second `AiToolCard`, because that would split form state);
  - scrolls the card to the top and moves focus to the card's toggle;
  - shows the back chip.
- **The back chip** returns to the origin panel, restores its scroll and focuses the originating row. It disappears on the next manual tab change.
- **Status.** Each row mirrors the card's status (installed / "Needs a 1.1 GB model" / running) from the same source `AiToolCard` uses.
- **Test.** A unit test asserts that every deep-link id exists in the catalogue and that the group counts are correct.

### 2.6 Tooltips (H4)

- There is **one** `<div class="tip" aria-hidden="true">`, portalled to `<body>` (`components/rail/RailTooltip.tsx`). It is driven by `data-tip` (text) and `data-kbd` (chord) on any control.
- **When it shows:** after 400 ms of hover, or of `:focus-visible` focus.
- **When it hides:** on Esc, pointerdown, scroll, blur or pointer-leave. Leaving has a 120 ms grace period so the pointer can move onto the tip (WCAG 1.4.13 "hoverable").
- **Placement:** to the right of rail and foot controls, and below top-bar controls, clamped to the viewport.
- **Names:** no `::after` content and no `title` attribute sits on any element with a role. The accessible name stays exactly the label, and verified exact-name matches are 1 for each of the 8 tabs. In the 48 px compact rail the visible name is the tooltip, and the label stays in the DOM, visually hidden, as the name.
- **Esc:** the tooltip handles it in the bubble phase. The keymap engine already leaves Escape propagating (`engine.ts`, the "EXCEPT Escape" rule).

### 2.7 Mount contract (M4)

- All 8 tabpanels stay **mounted**. Inactive panels get `hidden`, and a collapsed tool panel hides the whole `<section>`.
- The reason is VoRecorder. It owns a live MediaRecorder (or the native capture) and must never unmount mid-recording, as `LeftPane.tsx`'s existing comment explains.
- **`active` prop:** `active = leftOpen && leftTab === id` is passed to:
  - `AiPanel`, which takes its bbox guide rectangles off the preview when not active;
  - `TransitionsPanel` (catalogue fetch);
  - `StickerPanel` (artwork warm-up);
  - `EffectsPanel` (`list_luts` fetch).
- **StickerPanel:** delete its `open` state, the disclosure button and the outside-click close (`StickerPanel.tsx` ~64, ~115–133, ~171).
- **EffectsPanel:** delete its `open` state and the disclosure button (`EffectsPanel.tsx` ~203, title "Filters, effects & LUT looks").
- **State:** the chosen tab and the open state move from `LeftPane`'s local state into `lib/layoutStore.ts` (§6.1), so keyboard commands can drive them.

### 2.8 Activity chip (H5)

`components/topbar/ActivityChip.tsx` sits in the top bar's left group, after "Applying". It is **always rendered**: its `<span class="sr-only" role="status" aria-live="polite">` exists from the first paint, even when nothing is running.

| While… | Chip (28 px buttons, 12 px text) | Body click | Second button |
|---|---|---|---|
| recording a voiceover | `● Rec 0:12` (a static `--accent` dot, tabular digits) | opens Audio | **Stop** (`Square`, "Stop recording") → `activity.recording.stop()` |
| auto-captions running | `⟳ Captions 42% · 0:31` | opens Captions | **Cancel** (`X`, "Cancel captions"); shows "Stopping…" while the job acknowledges |

- **Announcements.** The live message is throttled. It announces state changes only: "Recording a voiceover", "Recording stopped", "Captions started", captions at 25 / 50 / 75 %, "Captions done", "Captions cancelled". Timer ticks go only into the button's `aria-label`.
- **Why a chip is needed.** A hidden live region stops announcing, which is exactly why the in-panel progress cannot be the only one.
- **Source of truth.** Everything comes from `lib/activityStore.ts` (§6.2):
  - VoRecorder publishes `recording`;
  - the captions run, extracted from `CaptionsButton` into `lib/captionRun.ts`, publishes `captions`;
  - both the chip and the panels read the same run, so there are never two Cancel paths.

### 2.9 Right panel

- **Header:** the tablist "Right panel" with **Inspector** (`SlidersHorizontal`, ⌥9) and **Chat** (`MessageSquare`, ⌥0), then the toggle "Hide the Inspector and Chat panel" (`ChevronRight`, a ≤ 32 px icon button with one svg, as `test_c2_panels_ui.py:311` requires).
- **Collapsed:** a 36 px rail with:
  1. **"Show the Inspector and Chat panel"** (`ChevronLeft`; keeps the name from M2; restores the last tab);
  2. a separator;
  3. "Show the Inspector";
  4. "Show the Chat", which also focuses the chat input.
- **Contents:** Properties + OpsLog "History" (Inspector) and ChatOverlay (Chat) are unchanged. Both stay mounted.
- **Code:** extracted from `App.tsx` into `components/RightPanel.tsx`.
- **Inspector jump list (wave E, review RE, as built).** The media clip Inspector is one long scroll (about 2,200 px; Canvas and Transform sit two screens down at 1440×900). `components/inspector/SectionIndex.tsx` puts a sticky `nav` "Jump to an Inspector section" under the media header. It lists the sections actually rendered (each Properties `Section` carries `data-section`, an id from `sectionId(label)` and `tabIndex=-1`) and appears only when there are at least 4. A chip ("Jump to <section>") scrolls the section to the top and moves focus into it; the scroll is instant when the viewer reduces motion (`lib/useReducedMotion.ts`). Section headings also carry the CapCut names people look for (`SECTION_AKA` in Properties.tsx): Canvas "Background", Voice effects "Voice changer", Color "Adjust", Blend "Blend mode". Tested by `tests/test_review_e_ui_e2e.py` (Chromium and WebKit).

### 2.10 Ratio menu (M6)

- **Structure:** `role="menu"` "Canvas ratio" holds three `role="group"` sections, each with an `aria-label`. The visual headings inside them are `aria-hidden`:
  1. **Aspect ratio:** 9:16 · 16:9 · 1:1 · 4:5
  2. **Platform presets:** Reels · Shorts · TikTok · IG 1:1 · IG 4:5
  3. **Safe-zone overlay:** Off · TikTok · Reels · Shorts, named "TikTok safe zone" and so on via sr-only text
- **Footer:** the canvas facts.
- **Every pick closes the menu** and returns focus to the trigger. Keys: ↑/↓/Home/End, Esc, Tab closes.
- **Trigger:** it shows the glyph and the current value ("9:16", or the active preset name). At density 0 it adds the facts. Its name is "Canvas ratio: 9:16, 1080 by 1920, 30 fps".
- **Safe zones:** `SafeZoneToggle`'s `<select>` is deleted. The overlay component (`SafeZones.tsx`, `lib/safeZones.ts`) is unchanged and is driven by the same store field.

---

## 3. Complete mapping: every current control → its new home

| Current control (file ~line) | New home | Notes |
|---|---|---|
| `h1.topbar-brand` "Video AI Editor" (TopBar ~260) | Top bar left: a `Clapperboard` mark in a rail-width cell (`--accent`, `aria-hidden`), plus the `h1` wordmark | The `h1` is visually hidden from density 1 on; it is never `display:none` |
| Project chip `button.pill.topbar-session`, with rename field "Project name" and `role=menu` "Projects" (New, Open .vae…, Rename…, rows with ✓ and meta, per-row delete, ConfirmDialog) (~261–379) | Top bar left, **unchanged** | Classes, menu name and rename kept (c3 test at `:225`). `min-width: 80px`, `max-width` 240, or 160 at density 3 |
| "Applying" pill (~380–384) | Top bar left, after the chip | `role=status`; spinner-only at density 3 |
| `.topbar-canvas` facts pill (~385–390) | Ratio trigger (density 0), its name and tooltip, and the Ratio menu footer | The pill is deleted |
| RatioMenu (~405) | Top bar **centre** | Groups per §2.10 |
| SafeZoneToggle `<select>` in `.topbar-wide` (~406) | Ratio menu, "Safe-zone overlay" group | A canvas overlay belongs with the canvas |
| Separators (~404, ~407) | Removed | |
| TextTool quick-add "Text" (~408) | Text panel "Add text at playhead" (inside `div[data-text-presets]`), plus the new command `addText` ⌥T | |
| TextTool "Text presets" caret popover: field, 6 styles, 4 templates (TextTool ~27–193) | Text panel body, inline (no popover) | Real presets; `needsField` gating kept |
| CaptionsButton "CC Captions" run (~409) | Captions panel "Generate captions" | |
| CaptionsButton live progress / ETA / **Cancel** in the top bar (CaptionsButton ~320–346) | Captions panel progress card **and** the top-bar activity chip | One run, from `lib/captionRun.ts` |
| CC caret menu: language (5, with hints), speed (2, with hints), download badges (~369–460) | Captions panel radio lists | |
| Download-consent dialog (~355–362) | Unchanged, opened from the Captions panel | |
| CC caret "Caption style…" → CaptionStylePanel | Captions panel "Caption style…" | The dialog is unchanged and also still opens from a cue |
| Help `?` button, `aria-label="Keyboard shortcuts"` (~412) | Rail foot, **same name**, `aria-keyshortcuts="?"`, tooltip "Help and keyboard shortcuts" | The dialog title "Keyboard shortcuts" is unchanged (c2 `:181`, `:199`) |
| Shortcuts keyboard icon, "Customize keyboard shortcuts" (~415) | Rail foot; new ⌥⌘K | |
| Settings gear, "Settings" (~419) | Rail foot, name exact (c3 `:210`) | ⌘, unchanged |
| Phone button (flag `phone_pairing`) (~439–447) | Media panel header action "From iPhone" | Absent from the markup when the flag is off; `TopBar.test` "no phone wording" moves to `ToolPanel.test` |
| TopBarMore `⋯` "More options" (~451) | **Deleted** | Safe zones → Ratio menu; version → Help |
| Version text `.topbar-wide` (~453) | Help dialog about line (already there) | Not the Settings footer: `SettingsDialog.tsx` belongs to instant preview Phase 1 |
| Save (~463) · Open (~478) | Top bar right, unchanged | Words hide at density 3; names "Save"/"Open" kept |
| Saved `.vae` link `<a download>` + `onSavedLinkClick` (~484) | Top bar right, **unchanged element** | "(outdated)" → dot at density 2; add an `aria-label` that carries "(outdated)" |
| Export result "↓ MP4" **`<button>` → `downloadExport()`** (~495) | Top bar right, **unchanged element** (M3) | Same stale-dot treatment |
| Export error chip `button.topbar-export-error` (~512) | Top bar right, before Export | Max 220/180/140 by density; icon-only at 3, keeping its `aria-label` |
| ExportButton `.primary` "Export ▾" (~539) | Top bar right-most, unchanged; + ⌘E | `.topbar-pinned button.primary` kept (a11y tests) |
| LeftPane tabs Media / Transitions / AI (LeftPane ~12–17) | Rail items 1, 6, 8, same names | Still `role=tab`; vertical arrows replace ←/→ |
| MediaBin dropzone, checkbox, upload rows, empty hint, media rows, Relink | Media panel | |
| MediaBin "Add music…" | Audio panel | |
| VoRecorder (in MediaBin) | Audio panel (stays mounted) | Its Stop is also in the activity chip |
| MusicPanel (in MediaBin) | Audio panel "Music on the timeline" | |
| StickerPanel disclosure "Emoji & sticker picker" (in MediaBin) | Stickers panel, open, no disclosure | |
| EffectsPanel disclosure "Filters, effects & LUT looks" (in MediaBin) | Effects panel, open, no disclosure | |
| MediaBin hidden `<h2>Media</h2>` + the `aiPanel.css` rule hiding it | Deleted (the panel header `h2` replaces it) | |
| TransitionsPanel (all) | Transitions panel, unchanged | |
| AiPanel (8 groups, 42 tools) | AI panel, unchanged; plus deep links (§2.5) and the back chip | |
| Right toggle `.right-panel-toggle` (App ~154–163) | Right panel header, after the tabs; same names | Collapsed: the 36 px rail of §2.9 |
| Right tabs Inspector / Chat (App ~164–172) | Unchanged, plus icons and ⌥9 / ⌥0 | `data-keymap-ignore` kept; ⌥9/⌥0 are `global` |
| Properties (all sections), OpsLog "History" | Inspector tab, unchanged | |
| ChatOverlay (docked) | Chat tab, unchanged | |
| PromptBar (form, BrainBadge, Run/Cancel, ClarifyCard, PromptRunLog) | **Stays** in the first row of `.center` | When the centre is under 620 px, BrainBadge goes icon-only (container query, `promptBar.css`) |
| Preview layers, top-left error chip | Unchanged | |
| Preview "Rendering…" text chip (Preview ~1226) | **Not in this spec.** It becomes the corner spinner with the last good frame in instant preview Phase 1, which owns `Preview.tsx` | The mock only shows it for context |
| Timeline toolbar, canvas, bowtie → TransitionPopover, context menu, markers | Unchanged | |
| Dialogs: Export, ExportModal, Settings, Help, ShortcutsSettings, CaptionStylePanel, ConfirmDialog | Unchanged; opened from their new homes | Help and ShortcutsSettings gain the "Panels" group automatically (via `CATEGORIES`) |
| Narrow warning (< 900), ConnectionBanner, MediaToolsBanner, FileDropOverlay, ToastHost | Unchanged | |
| Dead `index.css`, `App.css` | Deleted in R6 | |

Nothing is lost. The only deletions are:
- the `⋯` menu (both of its items re-homed);
- the separators;
- the canvas facts pill (its facts re-homed);
- the MediaBin `h2`;
- the SafeZone `<select>` (re-homed into the Ratio menu);
- the two disclosure buttons.

---

## 4. Keyboard

### 4.1 New commands (`keymap/commands.ts`, new category "Panels", the same chords in all three presets)

| Command id | Chord | Scope | Behaviour |
|---|---|---|---|
| `panelMedia` … `panelAI` (8) | ⌥1 … ⌥8 | global | Show that panel, opening it if collapsed. **Its own chord again collapses it.** Showing a panel puts focus **on the panel** (its `tabpanel`, `tabindex=-1`; as built, review RD3 — focus used to stay where it was, and "Generate captions" was 17 Tab stops from the timeline): Tab goes on into its controls and Space still plays. Hiding it leaves focus to the focus-rescue rule (§5.3). |
| `toggleToolPanel` | ⌥\ | global | Collapse / expand the tool panel |
| `showInspector` / `showChat` | ⌥9 / ⌥0 | global (+ text fields in `#right-panel`) | Expand the right panel on that tab; Chat focuses its input. As built (RD2): they also run from a text field inside the right panel, so ⌥0 then ⌥9 goes Chat → Inspector (focus lands on the Inspector tab) |
| `openShortcuts` | ⌥⌘K | global | Opens ShortcutsSettings (Premiere's Keyboard Shortcuts chord) |
| `exportVideo` | ⌘E | global | Opens the Export dialog (CapCut and Final Cut use ⌘E) |
| `addText` | ⌥T | global | Adds a text clip with the default style at the playhead, selected (same path as the button). As built (RD3): focus then goes to the new text's Inspector field with its words selected, so typing replaces them (it stayed on the timeline, where L shuttled and K stopped); Esc there returns to the timeline |
| `selectClipAtPlayhead` | C (CapCut, Final Cut), D (Premiere) | default | Select the clip under the playhead (the selected clip's lane first, then the main track, then the other lanes); announced (review RD3) |
| `selectNextClip` / `selectPrevClip` | ↓ / ↑ | default | Select the next / previous clip on the selected clip's lane (the main track when nothing is selected) and move the playhead to its first frame; announced (review RD3: a keyboard user could select only every clip) |
| `cycleRegion` / `cycleRegionBack` | F6 / ⇧F6 | anywhere | Focus the next / previous region: top bar → rail (the selected tab) → tool panel → Prompt bar → timeline (the timeline canvas) → right panel → rail foot. As built (RD2): the centre is two stops; as one it always landed in the Prompt textarea and never reached the timeline |

- **Collision check against `presets.ts`:**
  - CapCut has Mod+Backslash = zoom to fit, and Premiere has bare Backslash = zoom to fit. Both are distinct chords from ⌥\.
  - CapCut and FCP have Mod+KeyK = focus Prompt, distinct from ⌥⌘K.
  - The existing Alt chords are only ⌥[ / ⌥] and ⌥← / ⌥→.
  - None of the new chords is bound today. `commands.lock.test.ts` is unaffected.
- **Code-based matching.** Chords match on `KeyboardEvent.code`, so ⌥1 works on every layout, even though ⌥1 types "¡" in text. In text fields the engine leaves the key to typing, as it does today — except (as built, RD2) a `global` command on a ⌘ chord that types nothing and is not a native text chord (⌘E, ⌥⌘K run from the Prompt bar; ⌘A/C/V/X/Z, ⌘-arrows and ⌘⌫ stay the field's), and ⌥9/⌥0 inside the right panel. **Esc in an empty Prompt bar or Chat box** (after its own clarify / busy / clear-text handling) hands focus back to the timeline canvas.
- **Unchanged:** `?` (Help), ⌘, (Settings), `/` and ⌘K (Prompt), and every transport and editing chord.

### 4.2 Engine change (H1)

- **New field:** `Command.scope?: 'default' | 'global' | 'anywhere'`.
- **Order of checks in `engine.ts` `onKey`** (as built; R4 and review RD2):
  0. An open `aria-modal` dialog: nothing runs.
  1. Text entry returns early, unless the command is `'anywhere'`, or a `'global'` ⌘ chord that is not a native text chord, or the field is inside the command's `alsoInText` region.
  2. `[data-keymap-ignore]` returns early, unless the command is `'global'` or `'anywhere'`.
  3. A `[data-keymap-own="…"]` target keeps exactly the keys it names, with any modifiers (a focused speed-curve point keeps Delete/Backspace, so ripple delete never fires there).
  4. The `CONTROL_NAV_KEYS` guard: a focused NAVIGABLE control — a slider, a select, a radio (input or role), a tab, a menu item, an option — keeps its arrows, Home/End, PageUp/PageDown. As built (review RD3): a plain button or checkbox has no arrow behaviour and no longer keeps them (Shift+→ from a toolbar button or the import drop zone did nothing).
  5. A focused `role="tab"` keeps unmodified Space and Enter (APG).
  6. As built (review RD3): a button, checkbox, radio, switch, menu item or link focused WITHOUT the pointer keeps Space (it activates the control: Play backwards, Keep pitch, Mute, Solo, Snapping were out of a keyboard user's reach), and a native button Enter. Focus a pointer press gave (Chromium leaves it on a clicked button) does not: Space still plays there. `:focus-visible` cannot tell the two apart (the Space keydown itself turns it on before any listener runs), so the engine records focus that follows a `pointerdown` on the same control.
- **Consequence:** the chord must be resolved to a command *before* the scope checks. Split this out as a pure `shouldRun(cmd, target)` so it can be unit-tested (§8.2).
- **Why not "the ignore scope swallows only unmodified chords":** ⌘Z inside an AI form would then undo the timeline behind the user's back. Explicit per-command scope is the smaller behaviour change.

### 4.3 In-component keys

| Where | Keys |
|---|---|
| Rail | ↑/↓/Home/End move and activate; Enter/Space on the active tab toggles; Tab goes into the panel; Shift+Tab from the panel returns to the selected tab |
| Right tabs | ←/→/Home/End (unchanged; step 4 of §4.2, not an ignore scope since RD2); Space/Enter activate the focused tab (step 5) |
| Ratio menu | ↑/↓/Home/End, Enter/Space picks, Esc closes and returns focus, Tab closes |
| Tooltip | Esc hides it |
| Activity chip | Standard buttons |

---

## 5. Accessibility

### 5.1 Landmarks and names

- **Landmarks:** `header` "Project", `nav` "Tools", `section` labelled by the panel `h2`, `main` (centre), `aside` "Inspector and Chat", `nav` "Help and settings".
- **Exact names that tests rely on:**

| Kind | Names |
|---|---|
| Tabs | Media, Audio, Text, Stickers, Effects, Transitions, Captions, AI, Inspector, Chat |
| Buttons | Keyboard shortcuts, Customize keyboard shortcuts, Settings, Hide the tool panel, Hide the Inspector and Chat panel, Show the Inspector and Chat panel, Show the Inspector, Show the Chat, Stop recording, Cancel captions, Save, Open, Export |
| Menu | "Canvas ratio", with groups Aspect ratio / Platform presets / Safe-zone overlay |

- **ARIA on the rail:** `aria-keyshortcuts` on every rail tab and foot button. `aria-expanded` sits on the **selected** rail tab only (it is a supported state on `tab`), and on the collapse and right-toggle buttons.

### 5.2 Contrast

The idle label is `--text-dim` (#9b9ba5) on `--bg-0` (#0e0e10), about 7:1. The selected indicator is `--accent-2` on `--bg-0`, above 3:1 non-text. `test_rendered_text_meets_aa_contrast` walks every rail tab (§8).

### 5.3 Focus rescue (H2)

`ToolPanel` and `RightPanel` track focus-within (`focusin` / `focusout` → a ref).

- A `useLayoutEffect` keyed on `[leftTab, leftOpen]` (and `[rightOpen, rightTab]`) checks whether focus *was* inside the outgoing region and the focused element is now disconnected, inside `[hidden]`, or has no client rects.
- If so, it moves focus to the owning control. For the tool panel, that is the **selected rail tab** (the newly chosen one on a switch). For the right panel, it is **"Show the Inspector and Chat panel"**.
- Otherwise focus is not touched, so ⌥2 from the timeline leaves focus on the timeline.
- The rule does **not** assume the browser moves `activeElement` to `body`. WebKit and Chromium differ on focus fix-up, so the check is by element state.
- **Verified in the mock:**

| Start | Action | Focus ends on |
|---|---|---|
| Media dropzone | ⌥3 | the Text tab |
| a Text tile | ⌥\ | the Text tab |
| an Inspector slider | Collapse right | "Show the Inspector and Chat panel" |
| Undo | ⌥2 | still Undo |
| a media row (`data-keymap-ignore`) | ⌥8 | AI is selected (the H1 fix) |

### 5.4 Engine support in WKWebView (macOS 26–27)

The page uses `inert`, `:has()`, container queries, `clip-path: inset(50%)`, `aria-keyshortcuts` and CSS custom properties inside `grid-template-columns`. WebKit has shipped all of them since 16.x at the latest. The mock was verified in headless Chromium only. R1 exit requires one manual pass in the packaged app: focus rescue, the VoiceOver name of each tab, and the tooltip Esc.

---

## 6. State

### 6.1 `lib/layoutStore.ts` (new zustand store; `store.ts` is not touched until R6)

```ts
type RailId = 'media'|'audio'|'text'|'stickers'|'effects'|'transitions'|'captions'|'ai'
interface LayoutState {
  leftTab: RailId;            leftOpen: boolean          // vai.leftTab (legacy values valid), vai.leftOpen
  leftW: number | null;       rightW: number | null      // vai.leftW / vai.rightW; absent → null → CSS default (H7)
  rightOpen: boolean;         rightTab: 'inspect'|'chat' // vai.rightPanelOpen; lib/rightTab (unchanged keys)
  aiJump: { tool?: string; group?: string; from: RailId; nonce: number } | null
  showTab(id: RailId, opts?: { toggle?: boolean }): void
  setLeftOpen(open: boolean): void
  setPanelWidth(side: 'left'|'right', px: number | null): void   // clamp [180, 640] and centre ≥ 440
  setRightOpen(open: boolean): void
  showRight(tab: 'inspect'|'chat'): void
  jumpToAi(req: Omit<NonNullable<LayoutState['aiJump']>, 'nonce'>): void
  clearAiJump(): void
}
```

- **Inline widths.** `App.tsx` writes `--left-w` / `--right-w` inline **only when the value is non-null**. So the §1.2 media-query defaults apply until the user drags.
- **Drag seeding.** `Splitter` gains an optional `onStart`. At pointerdown, App seeds the drag base from `panelEl.getBoundingClientRect().width`. The stored width can be larger than the drawn width (because of `minmax`), and without seeding the first pixels of a drag would feel stuck.
- **Clamp.** Also clamp against `innerWidth − rail − other side − 12 − 440`.
- **Legacy values.** Stored values below 180 clamp to 180. `timelineH` stays in `store.ts`.
- **One writer.** From R1 on, `App.tsx` stops reading `store.leftW`, `rightW` and `rightPanelOpen`. Nothing else reads them (grep: only `App.tsx` and `store.ts`), so the dead fields are deleted in R6, after instant preview Phase 1 has merged `store.ts`.

### 6.2 `lib/activityStore.ts` (new)

```ts
interface ActivityState {
  recording: { startedAt: number; stop(): void } | null           // published by VoRecorder
  captions: { progress: number | null; etaS: number | null; cancelling: boolean; cancel(): void } | null // by lib/captionRun.ts
  liveMessage: string                                             // throttled announcements (§2.8)
}
```

- **The captions run** moves verbatim out of `CaptionsButton`'s local state into `lib/captionRun.ts`: start, poll, cancel, the "Stopping…" state, the model substitution notice and the consent flow. It becomes one module-level run that both `CaptionsPanel` and `ActivityChip` read.
- **Cancel latency.** The known up-to-58 s cancel acknowledgement keeps its "Stopping…" state.

---

## 7. Files

### 7.1 New

| File | Purpose | Phase |
|---|---|---|
| `frontend/src/lib/layoutStore.ts` (+ `.test.ts`) | §6.1 | R1 |
| `frontend/src/components/rail/railModel.ts` (+ `.test.ts`) | The 8 items, chords and tooltips, as one list | R1 |
| `frontend/src/components/rail/ToolRail.tsx` (+ `.test.ts`, SSR) | Rail, indicator, dots, foot | R1 (foot: R3) |
| `frontend/src/components/rail/ToolPanel.tsx` | Replaces `LeftPane.tsx`: header, 8 tabpanels, focus rescue, `active` props | R1 |
| `frontend/src/components/rail/RailTooltip.tsx` | The portal tooltip (§2.6) | R1 |
| `frontend/src/components/rail/rail.css` | Rail, panel header, tooltip; imports nothing else | R1 |
| `frontend/src/components/panels/AudioPanel.tsx` | Add music · VoRecorder · MusicPanel · AI audio rows | R1 (rows: R5) |
| `frontend/src/components/RightPanel.tsx` | Extracted from `App.tsx`, plus the collapsed rail and focus rescue | R1 |
| `frontend/src/components/panels/TextPanel.tsx` | TextTool content, inline | R2 |
| `frontend/src/components/panels/CaptionsPanel.tsx` (+ reuses `CaptionsButton.css` → renamed `captionsPanel.css`) | §2.5 | R2 |
| `frontend/src/lib/captionRun.ts` (+ `.test.ts`) | The captions run, lifted out of CaptionsButton | R2 |
| `frontend/src/lib/activityStore.ts` (+ `.test.ts`) | §6.2 | R2 |
| `frontend/src/components/topbar/ActivityChip.tsx` | §2.8 | R2 |
| `frontend/src/components/topbar/useTopBarFit.ts` (+ `topBarFit.test.ts` for the pure step function) | §1.4 | R3 |
| `frontend/src/components/rail/DeepLinkRow.tsx` (+ `deepLinks.test.ts`) | §2.5 | R5 |
| `tests/test_wave_d_rail_ui.py` | Playwright end-to-end tests for the rail (§8.3) | R1 → R5 (grows per phase) |

### 7.2 Changed

| File | Change | Phase |
|---|---|---|
| `frontend/src/App.tsx` | Grid children (rail, ToolPanel, foot), layoutStore wiring, RightPanel extraction, `RIGHT_RAIL_W` → 36 | R1 |
| `frontend/src/styles.css` | `.app` grid variables, `[hidden]` rule, remove `.left-tabs` / `.sidebar.left` tab rules, PROPOSED tokens | R1; top-bar rules R3 |
| `frontend/src/components/LeftPane.tsx` | **Deleted** (→ ToolPanel) | R1 |
| `frontend/src/components/MediaBin.tsx` | Loses Add music, VoRecorder, MusicPanel, StickerPanel, EffectsPanel and its hidden `h2` | R1 |
| `frontend/src/components/StickerPanel.tsx` | Disclosure removed; `active` prop | R1 |
| `frontend/src/components/EffectsPanel.tsx` (+ `EffectsPanel.css`) | Disclosure removed; `active` prop | R1 (**gate: before instant preview Phase 2**) |
| `frontend/src/components/aiPanel.css` | Drop the rule hiding MediaBin's `h2` | R1 |
| `frontend/src/components/Splitter.tsx` | Optional `onStart` | R1 |
| `frontend/src/lib/icons.ts` | 10 new icon names | R1 |
| `frontend/src/components/VoRecorder.tsx` | Publishes `activity.recording` | R2 |
| `frontend/src/components/TextTool.tsx` | **Deleted** (content → TextPanel; `PRESETS` exported from `lib/textPresets.ts`) | R2 |
| `frontend/src/components/CaptionsButton.tsx` | **Deleted** (logic → `captionRun.ts`, UI → CaptionsPanel) | R2 |
| `frontend/src/components/TopBar.tsx` | R2: remove TextTool and CaptionsButton, add ActivityChip. R3: grid `tb-left`/`tb-center`/`tb-right`; remove canvas pill, separators, SafeZoneToggle, TopBarMore, Help/Shortcuts/Settings (→ foot) and the phone button (→ Media header); stale dot; `useTopBarFit` | R2, R3 |
| `frontend/src/components/RatioMenu.tsx` | Groups, safe-zone group, close-on-pick | R3 |
| `frontend/src/components/SafeZones.tsx` | Remove `SafeZoneToggle` (the overlay stays) | R3 |
| `frontend/src/components/TopBarMore.tsx` | **Deleted** | R3 |
| `frontend/src/keymap/commands.ts` | "Panels" category and `CATEGORIES`, 14 commands, `scope` field | R4 (**gate: before instant preview Phase 4**) |
| `frontend/src/keymap/presets.ts` | The same chords in all 3 presets | R4 |
| `frontend/src/keymap/engine.ts` | `shouldRun` and the scope rule (§4.2) | R4 |
| `frontend/src/components/AiPanel.tsx` | Consume `aiJump` (clear the query, open the group, expand the card, scroll, focus), back chip, `active` from layoutStore | R5 |
| `frontend/src/store.ts` | Delete the dead `leftW` / `rightW` / `rightPanelOpen` / `setRightPanelOpen` (`setPanelSize` keeps `timelineH`) | R6 (**after instant preview Phase 1 merges**) |
| `frontend/src/index.css`, `frontend/src/App.css` | Delete (dead) | R6 |

**This spec never edits:**
- `Preview.tsx`, `TextLayer.tsx`, `StickerLayer.tsx`, `pipDraw.ts`
- `Properties.tsx`, `SettingsDialog.tsx`, `settingsModel.ts`
- `api.ts`, `types.ts`
- `timelineLayout.ts`, `frameStep.ts`, `overlayGate.ts`, `dragResolve.ts`, `dragVisuals.ts`, `FrameScrubber.tsx`
- `lib/preview/**`
- any server file

---

## 8. Tests

### 8.1 Existing tests to update (each in the phase that breaks it)

| File:line | Today | Change | Phase |
|---|---|---|---|
| `tests/test_c2_panels_ui.py:287` | `get_by_role("button", name="Stickers", exact=True)` | `get_by_role("tab", name="Stickers", exact=True)` | R1 |
| `tests/test_c2_panels_ui.py:296-300` | button "Effects", `aria-expanded == "true"`, sticker count == 0 | tab "Effects": assert `aria-selected == "true"`, and `.sticker-picker` **not visible** (hidden panels stay mounted, so `count()` stays > 0). QA-126's intent (one click switches) is kept | R1 |
| `tests/test_c2_panels_ui.py:311,316` | Hide / Show "the Inspector and Chat panel" | **No change** (names kept). Add: "Show the Inspector" and "Show the Chat" exist while collapsed | R1 |
| `tests/test_c2_panels_ui.py:357` | `get_by_role("button", name="Text", exact=True)` | press `Alt+T`, or tab "Text" then "Add text at playhead" | R2 |
| `tests/test_frontend_design_system.py:62-66` `_open_media_panels` | disclosures by `title` | click tab "Effects", then tab "Stickers" (Stickers last, for the `.sticker-picker input` fill) | R1 |
| `tests/test_frontend_design_system.py:46,79,82,224` | tab names | Add `exact=True` | R1 |
| `tests/test_frontend_design_system.py:88,300` | `button[aria-label='Keyboard shortcuts']` | **No change** (name kept; now in the rail foot) | none |
| `tests/test_frontend_design_system.py:90` | `button[aria-label='Text presets']` surface | Replace with the "text panel" surface (tab "Text") | R2 |
| `tests/test_frontend_design_system.py:91` | `button.cc-caret` surface | Replace with the "captions panel" surface (tab "Captions") | R2 |
| `tests/test_frontend_design_system.py:92` | `button.ratio-trigger` | Keep; the surface now includes the safe-zone group | R3 |
| `tests/test_frontend_design_system.py:278-293` | at 1440/1024, ≥ 8 top-bar controls, all 28 px / 12 px | Widths 900, 1024, 1280, 1440. **≥ 5** controls (project, Ratio, Save, Open, Export). Include `.topbar a`. Seed the recording chip so its 28 px buttons are measured | R3 |
| `tests/test_frontend_a11y.py:276` `_add_text_clip` | `[data-text-presets] > button` | Select tab "Text" first; the selector is kept (the wrapper and its first button are kept) | R2 |
| `tests/test_frontend_a11y.py:287` | loop Transitions, AI, Media | Loop all 8 rail tabs | R1 (Audio, Stickers, Effects), R2 (Text, Captions) |
| `tests/test_frontend_a11y.py:292` | trigger list incl. `Text presets` | Drop it | R2 |
| `tests/test_frontend_a11y.py:303-310` | dropzone within 4 Tabs of the Media tab | **No change** (verified: From iPhone? → Hide the tool panel → dropzone) | R1 verify |
| `tests/test_frontend_a11y.py:319-323` | popover params incl. `Text presets`, `button.cc-caret` | Drop both; add `("button.ratio-trigger", "menu")` | R2 / R3 |
| `tests/test_frontend_a11y.py:388-397` `test_more_menu_at_laptop_width` | `[data-topbar-more]` | Replace with `test_ratio_menu_groups_at_laptop_width`: 3 groups by name, "TikTok safe zone" exists once, Esc returns focus to the trigger | R3 |
| `tests/test_frontend_a11y.py` contrast test (~404) | walks Transitions, AI, Media | Walk all 8 panels | R2 |
| `frontend/src/components/TopBar.test.ts` "no phone wording" | TopBar SSR | Keep for TopBar; add the same assertion to `ToolPanel` SSR with the flag off | R3 |
| `TopBar.test.ts` "leaves no trailing separator" | separators | **Delete** (none left) | R3 |
| `TopBar.test.ts` "the TopBar tools" (`topbar-tools` slice) | the tools section | Slice `tb-center`; keep "holds one Ratio menu" | R3 |
| `TopBar.test.ts` "keeps Text, Captions, Help and Shortcuts inline" | inline | **Invert:** TopBar has no `data-icon="text"`, no "Captions", no "Keyboard shortcuts" and no "Settings". The Help/Shortcuts icon assertions (lucide `help` and `keyboard`, no ⌨ glyph) move to `ToolRail.test.ts` | R2 (Text, Captions), R3 (Help, Shortcuts) |
| `TopBar.test.ts` pinned-cluster tests | Export right-most | **No change** | none |
| `tests/test_c3_settings_ui.py:210,225` | button "Settings" exact; `.topbar-session` | **No change** | none |
| `tests/test_c_fix_ui.py:47,173` | tab "Media" | **No change** | none |

### 8.2 New unit tests (vitest)

| Test file | What it asserts |
|---|---|
| `lib/layoutStore.test.ts` | legacy `vai.leftTab` values load; an unknown value → `media`; toggle semantics (same id toggles, another id opens); `leftW` is null by default and when absent; clamp [180, 640]; persistence keys; `jumpToAi` bumps the nonce |
| `components/rail/railModel.test.ts` | 8 ids in order; labels unique; ⌥1–⌥8 unique; every icon exists in `ICONS` |
| `components/rail/ToolRail.test.ts` (SSR) | 8 `role=tab`, each with no `title` and no generated text; the foot names and lucide icons; `aria-keyshortcuts` |
| `lib/activityStore.test.ts` | start/stop recording; captions progress; the live message announces only at thresholds and state changes |
| `lib/captionRun.test.ts` | cancel → "Stopping…" until the job reports `cancelled`; the Fastest → consent gate (ported cases from CaptionsButton) |
| `components/topbar/topBarFit.test.ts` | `baselineFor(width)` at 899/1024/1099/1100/1279/1280/1439/1440; the step loop stops at the first fit and never exceeds 4 |
| `keymap/commands.panels.test.ts` | each panel command toggles on a repeat; ⌥\ toggles; the chords exist in all 3 presets; no chord collides with an existing binding |
| `keymap/engine.scope.test.ts` | `shouldRun`: a global command runs inside `[data-keymap-ignore]`; a default command does not; text entry blocks all but `anywhere` |
| `components/rail/deepLinks.test.ts` | every deep-link tool id exists in `aiCatalog.ts`; group counts (Find & search 5, Text & brand 7, Cutout & effects 4, Captions & speech 10); total 42 |

### 8.3 New end-to-end tests (`tests/test_wave_d_rail_ui.py`, Playwright; mirrors `rail-mock-probe*.py`)

1. **Names.** Each of the 8 tabs matches exactly one element with `exact=True`, and the tooltip text is not part of any name.
2. **Grid.** At 1024 the columns are `48 220 6 484 6 260`. With the left collapsed they are `48 0 0 710 6 260`. With both collapsed they are `48 0 0 940 0 36` (**no hole**). The same check runs at 1280, 1440 and 1920 against §1.2.
3. **Top bar worst case.** Seed a long name, the stale links, an export error (route stubs), a fake recording (Chromium `--use-fake-device-for-media-stream --use-fake-ui-for-media-stream`) and a captions job (a `page.route` stub of the job poll). Then, at 900, 1024, 1280 and 1440:
   - `.topbar` has `scrollWidth == clientWidth`;
   - Export's bounding box is fully inside the viewport;
   - "Stop recording" and "Cancel captions" are fully inside `.tb-left`;
   - Ratio is centred within 1 px when the right cluster fits.
4. **Activity.**
   - With the Media tab selected **and** the tool panel collapsed, "Stop recording" is visible and stops the recording.
   - "Cancel captions" shows "Stopping…".
   - The `role=status` region exists before any activity.
5. **Keyboard (H1).**
   - With focus on a media row (`data-keymap-ignore`), ⌥8 selects AI.
   - Inside the AI panel, ⌥1 selects Media.
   - With focus on the right tablist, ⌘E opens the Export dialog.
   - In the Prompt textarea, ⌥1 does **not** switch.
6. **Focus rescue (H2):** the five cases verified in §5.3.
7. **Tooltip (H4).** Hover a rail tab for 450 ms: the tip is visible, starts at x ≥ the rail's right edge, and carries the chord. Esc hides it. Moving the pointer onto the tip keeps it.
8. **Deep link (M5).** Captions → "Detect speakers": AI is selected, the search is empty, `#…diarize` is expanded and focused, and the back chip is visible. Clicking the back chip returns to Captions with focus on the row.
9. **Ratio menu (M6):** as described in §8.1.
10. **Tab order:** Media tab → Tab → (From iPhone) → Hide the tool panel → dropzone.

**Visual regression.** Screenshots at 320 (the narrow warning), 1024, 1280, 1440 and 1920, plus the worst-case state at 900 and 1024.

**WKWebView.** One manual pass in the packaged app per phase (R1 exit criterion): VoiceOver reads each tab name once; focus rescue works; ⌘E and ⌥⌘K reach the page.

---

## 9. CapCut parity notes (L1): deliberate divergences

- **Filters.** CapCut has a Filters tab. Ours are the Effects panel's first section, "Filters · LUT looks", and the Effects tooltip says "Filters, LUT looks and effects". A ninth rail item would push the compact rail to 9×44 = 396 px, which still fits; it is deferred until there is a filters catalogue beyond LUTs.
- **Adjustment.** CapCut has an Adjustment tab. Ours is the Inspector's Color section, per clip, and it becomes live in instant preview Phase 2.
- **Ratio.** CapCut shows Ratio under the player. We keep it centred in the top bar. Our player toolbar is the timeline toolbar and is already dense at 484 px (container queries hide the zoom slider under 640 px). The canvas is a project-level choice that sits well next to the project name.
- **Rail direction.** CapCut desktop uses a horizontal tab row across the top of the left panel. We use a vertical rail. Horizontal overflow was the original defect (QA-012), and a vertical rail holds 8+ items at 1024 with room to spare. It also matches CapCut web and mobile.

---

## 10. Phased implementation plan

Every phase:
- ships on its own;
- keeps `main` green;
- names the files it owns;
- updates the tests it breaks (§8.1) in the same change.

Instant preview (IP) works in `lib/preview/**`, `store.ts`, `Preview.tsx`, the layers, `Properties.tsx`, `EffectsPanel.tsx` (IP2), `keymap/*` (IP4), `SettingsDialog.tsx` and the server. The only files both efforts need are `EffectsPanel.tsx`, `keymap/*` and `store.ts`. Each of them is handled by an ordering gate, not by a merge.

| Phase | Owns (edits) | Gate | Exit |
|---|---|---|---|
| **R0 Preconditions** | nothing | Wave C has landed. Re-read every "~line" in §3 and §8.1 against the post-wave-C tree. Confirm with the IP lead: IP puts its CSS in its own file(s), not `styles.css`, and IP2 starts only after R1 merges | Checklist signed |
| **R1 Grid, rail, panel shell** | New: `layoutStore.ts`, `rail/railModel.ts`, `rail/ToolRail.tsx`, `rail/ToolPanel.tsx`, `rail/RailTooltip.tsx`, `rail/rail.css`, `panels/AudioPanel.tsx`, `RightPanel.tsx`. Changed: `App.tsx`, `styles.css` (grid block only), `MediaBin.tsx`, `StickerPanel.tsx`, `EffectsPanel.tsx`(+css), `aiPanel.css`, `Splitter.tsx`, `lib/icons.ts`. Deleted: `LeftPane.tsx`. Tests: c2 `:287/:296/:311`, design_system `_open_media_panels` and exact names, a11y tab loop | **Must merge before IP Phase 2 branches** (`EffectsPanel.tsx`) | The rail shows **6** items: Media, Audio, Stickers, Effects, Transitions, AI. Text, Captions, Help and the rest are still in the top bar. §8.3 cases 1, 2, 6, 7 and 10 pass. One manual WK pass |
| **R2 Text, Captions, activity** | New: `panels/TextPanel.tsx`, `panels/CaptionsPanel.tsx`, `lib/captionRun.ts`, `lib/activityStore.ts`, `topbar/ActivityChip.tsx`, `lib/textPresets.ts`. Changed: `VoRecorder.tsx`, `TopBar.tsx` (remove the two components, add ActivityChip, nothing else), `railModel.ts` (+2 items). Deleted: `TextTool.tsx`, `CaptionsButton.tsx` (css renamed). Tests: c2 `:357`, a11y `_add_text_clip`, `:292`, `:319-323`, design_system `:90/:91`, TopBar.test (Text, Captions) | none | 8 rail items. §8.3 case 4 passes. The captions cancel and consent flows behave exactly as before |
| **R3 Top-bar diet** | New: `topbar/useTopBarFit.ts`. Changed: `TopBar.tsx` (grid, density, stale dot, remove the canvas pill, separators, SafeZoneToggle, TopBarMore mount, Help/Shortcuts/Settings, phone button), `ToolRail.tsx` (foot), `ToolPanel.tsx` (From iPhone header action), `RatioMenu.tsx`, `SafeZones.tsx`, `styles.css` (top-bar block). Deleted: `TopBarMore.tsx`. Tests: a11y `:388-397`, design_system `:278-293`, TopBar.test (separator, tools slice, Help/Shortcuts) | none | §8.3 cases 3 and 9 pass at 900, 1024, 1280 and 1440 |
| **R4 Keyboard** | `keymap/commands.ts`, `keymap/presets.ts`, `keymap/engine.ts`; `layoutStore.ts` (commands call it) | **Must merge before IP Phase 4 branches** (`keymap/*`) | §8.3 case 5 passes. Help and ShortcutsSettings list the "Panels" group. ⌘E and ⌥⌘K verified in the packaged app |
| **R5 Deep links** | New: `rail/DeepLinkRow.tsx`. Changed: `AiPanel.tsx`, `panels/AudioPanel.tsx`, `panels/TextPanel.tsx`, `panels/CaptionsPanel.tsx`, MediaBin's Find & search section, the Effects panel's Cutout rows (in `ToolPanel.tsx`, not in `EffectsPanel.tsx`, so IP2 is not touched) | none | §8.3 case 8 and `deepLinks.test.ts` pass |
| **R6 Cleanup** | `store.ts` (the dead panel fields), `index.css`, `App.css` | **After IP Phase 1 has merged** (`store.ts`) | `rg "leftW\|rightPanelOpen" frontend/src/store.ts` finds nothing, and the build and tests are green |

R2 and R3 both edit `TopBar.tsx`, so they are sequential. R4 and R5 are independent of R2 and R3 and can run in parallel with them, provided the gates hold.

### 10.1 R1 as built: recorded deviations (review RD1)

1. **Space on the rail.** §4.1 says Enter/Space on the active tab toggles the panel. In R1 the rail is *not* a `[data-keymap-ignore]` scope: that scope made every global shortcut (⌘Z, J/K/L, N, Space play) dead while a rail tab had focus, which the LeftPane tabs never did. So until R4's `Command.scope` lands, **Enter** toggles and **Space** stays the global play/pause, as it was on LeftPane. A mouse click on a tab does not move focus onto it (mousedown `preventDefault`), so the next Space plays instead of collapsing the panel (measured in Chromium and WebKit, `test_wave_d_rail_ui.py`). R4 restores Space-toggles with the scope rule.
2. **Chords.** The rail shows a chord only while the live keymap binds one; R1 binds none, R4 lights them up.
3. **The AI dot** lights while a Prompt-bar run holds the session lock (`lib/promptStore`: planning/running/verifying) **or** an AI tool card's job runs (`lib/aiRuns`). Its sr-only description is `hidden` (still the `aria-describedby` target), so it is not read in the Tools nav when idle.
4. **Transition tile artwork** (`lib/transitionPreview.ts` wave polygon, `transitionsPanel.css` clock still frame at `animation-delay: -0.375s`) changed so the QA-119 "every tile's still frame is distinct" check still holds at the wider R1 panel: with the old artwork at the R1 panel width the two closest pairs measured 1.5 and 3.97 mean-pixel difference, under the test's threshold of 4 (`test_c2_panels_ui.py`). Neither file is in the R1 ownership row, and §2.5 says "TransitionsPanel, unchanged"; this is a visual change to the still frames only, recorded here rather than moved.
5. **Timeline toolbar.** The narrower centre column (484 at 1024, was 512) needs the zoom steps to drop below 490 px instead of 440 px, or "Zoom to fit" is clipped between ~941 and 1024 px (`styles.css`, outside the grid block).

### 10.2 R2–R5 as built

The phases landed out of order (R4 first, then R2, R3, R5); their records are below in phase order, followed by the review RD2 fixes that changed R4's rules. Each phase's "manual pass in the packaged app" (VoiceOver names; ⌘E and ⌥⌘K in the packaged app) is still owed.

#### 10.2.1 R2 as built (Text, Captions, activity)

The rail shows all eight items. `TextTool.tsx` and `CaptionsButton.tsx` are deleted; `CaptionsButton.css` became `components/panels/captionsPanel.css`. The Text and Captions flows were walked in Chromium and Playwright WebKit before and after the change against a real backend: every `add_text` / `apply_text_template` request body, the resulting clips, the consent dialog, the cancel request sequence, the "was cancelled" toast and the success toast are identical (`qa-fix/R2-text-captions/walk-*.json`).

1. **Files outside the R2 row.** `rail/ToolPanel.tsx` mounts the two panels (`CaptionsPanel` gets `active` and re-reads `/api/downloads` each time it shows, as the old menu did on open). `rail/ToolRail.tsx` takes the Audio (`rec`, "Recording in progress") and Captions (`busy`, "Captions are being generated") dots from `lib/activityStore`. `lib/icons.ts` gains `stop: Square`: §2.2 lists `Square` among R1's icons, but R1 did not add it.
2. **TopBar.** Removing the two components left two separators side by side; one is removed with them. Nothing else in the bar changed (R3 owns it).
3. **One live region.** The Captions panel's progress card is not a live region: the chip's throttled region is the one that speaks (§2.8), and a second, per-second region would chatter. The chip's region also says "Stopping captions" when Cancel is pressed and "Captions failed" when a run fails, so a failure is never announced as "Captions done".
4. **Choices during a run.** The language and speed radios are disabled while a run is live. The old menu could not be opened during a run at all, and the radios describe the job on screen, whose language cannot change.
5. **Chip clock.** The captions chip reads "Captions 42% · 0:31 left" once an honest ETA exists and "Captions · 5s" (elapsed) before; the mock's bare "· 0:31" could not tell the two apart. While cancelling it reads "Captions Stopping…" and "Cancel captions" stays named but disabled.
6. **Labels kept from the product, not the mock.** The download badge keeps the existing `downloadBadge()` text ("Downloads 1.6 GB first"), and the Text field has a visible label "Text, #hashtag or @handle" (the mock had a placeholder only) with the old placeholder.
7. **Not yet.** The AI deep-link rows in Text and Captions are R5; the chip's density step 4 is R3.
8. **WKWebView.** `tests/wk/test_wk_activity.py` runs the real chip, rail and tool panel in a real WKWebView over a stubbed fetch and the native voiceover bridge: the take survives its panel hiding and stops from the chip, focus returns to "Generate captions" after the consent dialog, and Cancel shows "Stopping…" until the job acknowledges. The manual pass in the packaged app is still the exit criterion.

#### 10.2.2 R3 as built (top-bar diet)

The top bar is the §1.4 grid: `.tb-left` (brand mark over the rail, the `h1` wordmark, the project chip, "Applying", the activity chip), `.tb-center` (Ratio) and `.tb-right.topbar-pinned` (Save, Open, the .vae and MP4 links, the export error, Export). Help, Customize keyboard shortcuts and Settings are `RailFoot` (`nav.rail-foot`, "Help and settings"), mounted by `App.tsx` last in the DOM; the iPhone action is the Media panel's header action. `TopBarMore.tsx` and `SafeZoneToggle` are deleted; the safe zones are the Ratio menu's third group. `tests/test_wave_d_topbar_ui.py` measures §8.3 cases 3 and 9 in Chromium and Playwright WebKit, and `tests/wk/test_wk_topbar.py` measures the worst case in real WKWebView.

Measured worst case (a 67-character name, recording, captions at 42 %, "Applying", both links outdated and a long export error; real WKWebView): 1440 → step 1, 1280 → 2, 1024 → 4, 900 → 4, Export's right edge at width − 12 each time, nothing clipped, and back at 1440 → step 1 again. At 1440 that error makes the right cluster wider than half the bar, so Ratio shifts left (its centre at 695) instead of anything clipping, as §1.4 allows.

1. **Every step is CSS on the header's classes only.** The first build rendered the ratio facts only while React's density was 0; a re-fit that started from a denser step then measured a narrower centre than it went on to render and settled on a step that overflowed (Chromium, measured: 657 px of content in a 599 px left group at 1440). The facts span is now always rendered and `.tb-d1` hides it. `topBarFit.test.ts` checks that every `display: none` in a density rule targets words only.
2. **Baseline from the bar's width.** `useTopBarFit` takes its baseline from the header's own width, which is the viewport width in the app (the bar stays 900 px below the 900 px floor, where `#root` scrolls). This lets the WKWebView page host the bar in a 1440, 1280, 1024 or 900 px box inside the harness's 800 px web view.
3. **Re-fit triggers.** A layout effect keyed on the bar's content (name, pending ops, activity, stale flags, error, exporting, canvas) re-fits before paint. A `ResizeObserver` on the bar, the activity chip and the right cluster re-fits one frame later for widths that change with no React input in TopBar (the recording clock gaining a digit, the Export button's elapsed counter). Doing this inside the observer callback would resize observed elements during delivery.
4. **Names keep what a step hides.** The .vae link is named "Download the saved .vae project" and adds "(outdated)" when stale. The MP4 button is "Save exported MP4 (outdated)". The error chip keeps "Export failed: ‹message›. Dismiss". The Ratio trigger is "Canvas ratio: 9:16, 1080 by 1920, 30 fps". Its visible value is the one preset in effect (Shorts, IG 4:5…), or the aspect when none is in effect or the spec is shared: Reels and TikTok are one spec, so naming either would be a guess.
5. **Safe-zone items.** They are named "Safe zones Off" and "TikTok safe zone" etc. On a canvas that is not 9:16, the visible "9:16 only" hint is `aria-hidden` and said as the item's description (`aria-describedby`), so the name stays exactly "TikTok safe zone" on every canvas. The items keep a native `title` for their detail, because the shell tooltip opens below top-bar controls, where it would cover the next menu item.
6. **From iPhone.** The flag moved from TopBar to `rail/phonePairing.ts`: one `/api/version` question per page, answered strictly (only a literal `true` is on). `PhonePanel` is lazy-imported on the first open, so with the flag off no pairing code is fetched and nothing reaches `/api/pair/*` (Playwright checks both request logs).
7. **Files outside the R3 row.** `App.tsx` mounts `RailFoot` (one line; the foot must be its own grid item, last in the DOM). `rail/rail.css` holds the foot and header-action styles, next to the rail's own. `lib/icons.ts` gains `brand: Clapperboard`: §3 names the mark, but R1 did not add it. `lib/ratioMenu.ts` gains the trigger's pure helpers. `rail/railModel.ts` `ariaKeyshortcuts` names punctuation by key value, because the foot's Settings chord was "Meta+Comma" and must be "Meta+,". `safeZones.css` loses the dead `<select>` styles. Beyond §8.1, R3 also broke and updated two tests: R4's F6 test (the foot is now the sixth region) and `test_no_emoji_or_text_glyph_used_as_an_icon` (it opened the deleted "⋯" menu).
8. **Not yet.** The manual pass in the packaged app is still the exit criterion: VoiceOver on the foot and the Ratio groups.

#### 10.2.3 R4 as built (keyboard)

Deviations 1 and 2 of §10.1 are closed: the rail's tooltips, panel header and `aria-keyshortcuts` show ⌥1…⌥8 in every preset, and Space toggles the focused rail tab again.

1. **Space and Enter on a rail tab** come from a fifth step in `engine.ts` `shouldRun`, not from an ignore scope on the rail: *a focused `role="tab"` keeps unmodified Space and Enter* (the APG tab keys). Every other chord (⌘Z, J/K/L, N, ⌥1…⌥8) still runs with focus on a rail tab. The rule first applied only to the rail (the right panel's tabs and the Transitions family tabs sat inside `[data-keymap-ignore]`); since RD2 the right panel's tabs use it too (§10.2.5).
2. **A click on a rail tab while keyboard focus is on another rail tab** moves focus to the clicked tab (`ToolRail.tsx`). Otherwise focus stays on a tab that is no longer selected and the next Space activates *that* tab. A click from anywhere else still does not focus the tab, so Space keeps playing.
3. **Modal dialogs.** No command runs while an `aria-modal` dialog is open, whatever its scope (the §4.2 order gains a step 0). Without it a `global` chord (⌥5, ⌘E, F6) acted behind the Export or Settings dialog.
4. **Groups.** "Panels" holds the eight panel commands, ⌥\, ⌥9/⌥0 and F6/⇧F6. `openShortcuts` (⌥⌘K) and `exportVideo` (⌘E) are listed under Navigation beside Open Settings, and `addText` (⌥T) under Editing.
5. **⌘E, ⌥⌘K and ⌥T press the existing control** (`keymap/uiTargets.ts`: `.topbar-pinned button.primary`, the button named "Customize keyboard shortcuts", the first button of `div[data-text-presets]`). This is the "same path as the button" of §4.1, so the Export trigger's disabled rule and focus return are the button's own. A disabled Export says why in a toast (its tooltip).
6. **Panel commands come from `RAIL_ITEMS`**, so a panel the rail does not show has no command and Help lists no dead key. `presets.ts` `PANEL_KEYS` binds all eight chords in all three presets.
7. **Mac key caps** use Apple's modifier order (⌥⌘K, ⇧⌘Z; `engine.ts` `formatChord`), which the backend's "Redo with ⇧⌘Z" already used. Before R4 the caps followed the chord string (⌘⇧Z).
8. **WKWebView.** `tests/wk/test_wk_keymap.py` sends native NSEvents to a real WKWebView running the real engine and command registry: ⌘ chords through `performKeyEquivalent:` and the rest through `keyDown:`. WebKit takes ⌘E, ⌥⌘K and ⌘Z, and the page resolves and handles each chord. A static check reads the installed pywebview: its main menu (⌘Q/H/⌥H/⌃⌘F/X/C/V/A) and `WebKitHost.keyDown_` (⌘X/C/V/A/Z/Q/W) claim neither ⌘E nor ⌥⌘K, so risk 3's fallback chords are not needed. The manual pass in the packaged app is still the R4 exit criterion.

#### 10.2.4 R5 as built (deep links)

The rows are `rail/DeepLinkRow.tsx` (`DeepLinks`, `DeepLinkRow`, `DeepLinkGroupLink`, `AiBackChip`) over the pure `rail/deepLinks.ts` (the per-panel table, counts, status, return point). Media (Find & search, last in the panel), Audio (AI audio), Text (AI text & brand), Effects (Cutout & effects (AI), mounted under `EffectsPanel` by `ToolPanel.tsx`) and Captions (Captions & speech (AI)) carry 17 rows and 4 "All ‹group› tools (n)" links. `AiPanel.tsx` consumes `aiJump` in a layout effect: it clears the search, presses the card's own toggle (the same path as a click, so the form seeds from the playhead then), scrolls the card to 6 px under the sticky search head and focuses the toggle. `tests/test_wave_d_deeplinks_ui.py` (Chromium and Playwright WebKit) runs case 8 and all 21 jumps and back, and runs at least one landed card per panel against the real backend (Find b-roll, Reduce noise, Hook overlay, Lower third, Brand kit, Chroma key, Import subtitles → Export .srt); `tests/wk/test_wk_deeplinks.py` runs every jump in real WKWebView.

1. **The back chip's name.** It shows "‹ Captions" (a `ChevronLeft` and the panel name) and is named "Back to Captions": the words "Back to" are sr-only. A bare "Captions" button would be a second control with a rail tab's exact name.
2. **Where the chip sits.** In the AI panel's sticky head, above the search box, so it stays on screen while the landed card scrolls.
3. **Status.** A row mirrors the card's own words from the same sources (`lib/aiRuns`, `featureCopy`, `downloadBadge`): "Running 42%", "Stopping…", "Failed", "Cancelled", "Done", "Not installed" / "Not set up", "Downloads 1.1 GB first" (the product's badge, not the mock's "Needs a 1.1 GB model"), and "Not available" for a tool the backend does not advertise. The status is the row's description (`aria-describedby`), never part of its name, and it sits under the label: beside it, "Translate captions" truncated at 220 px.
4. **Catalogue loading.** A panel with rows loads the AI catalogue (`/api/tools`, `/api/features`, `/api/downloads`) the first time it is shown, so its rows can say what the card says. The Media panel shows at launch, so its rows wait for pointer or focus instead of adding the ~2 s feature probe to every start.
5. **Group links and unknown tools.** "All ‹group› tools (n)" lands on the group's `h3` (`tabindex="-1"`, script focus only). A row whose tool this backend does not advertise lands on its group's heading too.
6. **Landing near the end of the list.** A card that cannot scroll up to the head lands fully scrolled and on screen. The card's form renders one update after the toggle's click, so the landing aligns once more before that frame paints, and again while the feature reports grow the cards above it, as long as focus is still on the landed toggle.
7. **Returning.** The back chip commits the tab switch with `flushSync`, focuses the originating row with `preventScroll` and restores the origin panel's scroll. WKWebView still revealed a partly hidden row in the next rendering update after a panel was `display: none`, so the scroll is put back once more in the next frame (measured in `test_wk_deeplinks.py`).
8. **Tests file.** The R5 end-to-end tests live in `tests/test_wave_d_deeplinks_ui.py` rather than growing `tests/test_wave_d_rail_ui.py` (§7.1), because other rail phases edited that file concurrently. It reuses `test_frontend_a11y`'s harness.
9. **Carried from R1.** The stale `LeftPane` mentions in `AiPanel.tsx`, `AiToolForm.tsx` and `lib/aiRuns.ts` now name the tool panel.
10. **Not yet.** The manual pass in the packaged app (VoiceOver name of the chip and of a row with a status).

#### 10.2.5 Review RD2 (milestone 2 fixer)

Measured in Chromium and Playwright WebKit (`tests/test_wave_d_rail_keys_ui.py`, `tests/test_wave_d2_fixer_ui.py`) against a real backend with no API key:

1. **⌘Z, J/K/L and N work with focus in the Inspector's Speed section and on the Inspector tab.** The two Speed radiogroups, the speed-curve editor and the right panel's tablist were `[data-keymap-ignore]` scopes, which drop every `default` command: picking a preset with the mouse left focus on its radio (Chromium), and ⌘Z then did not undo it (undo depth 36 → 37 → 37). The scopes are gone. The radios' arrows are theirs by step 4; Space plays from a radio as from any button and Enter (bound to nothing) chooses it; a curve point claims Delete/Backspace with `data-keymap-own`; the Inspector tab keeps Space/Enter by step 5. Both "global shortcuts work with focus anywhere" cases gain `speed-preset` and `inspector-tab`, and a test presses ⌘Z right after picking a preset (it fails with the old scope, measured).
2. **Text fields no longer trap the panel chords** (§4.1 as built): ⌘E and ⌥⌘K from the Prompt bar, ⌥0 → ⌥9 from the Chat box, and Esc in an empty Prompt bar or Chat box returns to the timeline.
3. **F6 reaches the timeline** (§4.1 as built): seven stops, the timeline stop focusing the timeline canvas.
4. **A stale clarify question** (it survives a reload) no longer swallows a new sentence: Enter with a different, non-empty text drops the question and runs the text (`lib/promptFocus.supersedesClarify`); the same text still sends the user to the card.
5. **A disabled icon-only toolbar button looks disabled.** Split, Freeze frame, Delete and Duplicate in an empty project measured 1.17:1 luminance against an enabled icon; they now use `--icon-disabled` (#5c5c66), ≥ 2:1 dimmer than an enabled icon in both engines. WCAG exempts disabled controls; the name and tooltip still say why.
6. **The Normal speed slider on a curve clip** starts at the curve's mean speed (0.93× for a curve that read 1.00×), so the first nudge keeps the clip's length near the curve's.
7. **The key-free Prompt bar** reads "add a hero speed ramp" as the Hero curve (it committed a constant 1.25×), "freeze frame at 5 seconds" as a freeze (it became a title question) and "split at 3 seconds" as a split (it was not understood). A ramp with no name asks which curve. New grammar intents `freeze` and `split`; the `speed` recipe gains a `preset` slot.

#### 10.2.6 R6 as built (wave D3, lane E3)

`store.ts` no longer declares or initialises `leftW`, `rightW`, `rightPanelOpen` or `setRightPanelOpen`; `setPanelSize` takes `'timelineH'` only (the timeline splitter is its one caller). `lib/layoutStore.ts` is the only reader and writer of `vai.leftW` / `vai.rightW` / `vai.rightPanelOpen` (risk 8 closed). `frontend/src/index.css` and `frontend/src/App.css`, the Vite template's leftovers that nothing imported, are deleted. Exit check: `rg "leftW|rightPanelOpen" frontend/src/store.ts` finds nothing; `tsc -p tsconfig.app.json` and the full vitest suite (176 files) pass, and `tests/test_wave_d_rail_ui.py` + `tests/test_wave_d2_fixer_ui.py` pass in Chromium and Playwright WebKit against a real backend.

**Exit check re-run by the fixer (review RD3):** `rg "leftW|rightPanelOpen" frontend/src/store.ts` finds nothing; `tsc` and vitest pass; the rail suites (`test_wave_d_rail_ui.py`, `test_wave_d_rail_keys_ui.py`, `test_wave_d_deeplinks_ui.py`) pass in Chromium and Playwright WebKit. The review's rail findings closed with it:
1. **The back chip restores the origin panel's scroll for good**: WebKit revealed a partly hidden row one or two rendering updates after a `preventScroll` focus (the Text panel landed at 27 instead of 0 in 2 of 3 WK runs; one rAF re-apply was not enough); for 400 ms, while focus stays on the row and the user does nothing in the panel, any scroll is put back (`DeepLinkRow.holdScroll`). WK: 5/5.
2. **⌥1 … ⌥8 put focus on the panel** (§4.1 as built above).
3. **The Media panel's names below the app's 1100 px minimum** (a browser window only): one line with an ellipsis, never broken mid-word.
4. **Tab reaches every control in the app window**: `desktop.enable_tab_to_all_controls` turns on WKPreferences.tabFocusesLinks (Safari's "Press Tab to highlight each item"); by default WebKit tabs only to text fields unless the Mac's Keyboard navigation is on, and the editor had six Tab stops (WK: `tests/wk/test_wk_tab_focus.py`, both ways).

**Wave E (lane F4b), follow-ups 26 and 27 (the editor in a browser):**
1. **The timeline zoom −/+ steps stay** at every window width the grid makes. §10.1 item 5 dropped them below a 522 px pane (every window under 1100 px); now the toolbar tightens instead: gaps 6 → 3 px, no separator margins, icon buttons 28 → 24 px wide (still a 24 × 24 target). Measured in Chromium and Playwright WebKit: the pane is 440 px at any window ≤ 960 px, 484 at 1024, 559 at 1099; the tightened bar is 424 px with both steps, so Zoom out, Zoom in and Zoom to fit are inside the bar and nothing scrolls at 860-1180 px. Only a pane ≤ 430 px (narrower than the grid ever makes) drops the steps (`tests/test_f4b_ui_e2e.py::test_the_zoom_steps_stay_reachable_below_1100px`).
2. **Help says how Tab reaches every control in Safari**: hold Option (⌥Tab), or turn on "Press Tab to highlight each item" in Safari Settings › Advanced; the app window needs neither (item 4 above).

**Wave E (lane F2): Canvas and Blend in the Inspector.** A main-track clip's Inspector has a **Canvas** section (after Framing): a radiogroup "Canvas background" None · Colour · Blur · Image, CapCut's 14 colour swatches plus a custom colour, four blur strengths (Light … Heavy), "Choose picture…" (uploaded to the session's `uploads/images`), and **Apply to all** (one `set_canvas_background {all: true}`, one undo step); Reset puts black bars back. An overlay clip has a **Blend** section (after PIP shape): a native `<select>` "Blend mode" in CapCut's order, with a note where this browser's live blend is not the export's. Arrow keys / Home / End move inside every radiogroup, Space or Enter picks; lucide glyphs through `lib/icons` (`canvasNone`, `canvasColor`, `canvasBlur`, `canvasImage`, `applyAll`, `blendMode`); tokens only; transitions only under `prefers-reduced-motion: no-preference`. **Not in the Ratio menu:** §2.10 fixes that menu to three project-level groups whose every pick closes it, and the canvas background is per clip (CapCut's own Canvas lives on the clip, with Apply to all), so it stays in the Inspector. Measured in Chromium and Playwright WebKit (`tests/test_f2_canvas_blend_ui.py`).

---

## 11. Risks

1. **Wave C is still editing the same files.** It is changing `App.tsx`, `MediaBin.tsx`, `AiPanel.tsx`, `CaptionsButton.tsx`, `EffectsPanel.tsx` and several tests right now. Line references will drift. **Mitigation:** R0 re-reads them; no rail code starts before wave C lands.
2. **`EffectsPanel.tsx` is shared with IP Phase 2, and `keymap/*` with IP Phase 4.** **Mitigation:** the ordering gates in §10. If IP2 has to start first, R1 keeps the disclosure behind an `embedded` prop and removes it in a follow-up after IP2 merges.
3. **⌘E and ⌥⌘K may never reach the page.** The pywebview app menu or WKWebView could claim them. **Mitigation:** verify in the packaged app in R4. The fallback is ⇧⌘E and ⌥⇧K, rebindable anyway.
4. **`useTopBarFit` could thrash.** A measure → restyle → measure loop can cause layout thrash or oscillation on resize. **Mitigation:** a bounded 5-step loop in a layout effect; restart from the baseline only on resize; the pure step function is unit-tested; the worst case is tested at 4 widths.
5. **Lifting the captions run can regress edge cases.** The cancel acknowledgement (up to 58 s), the model-substitution notice and the 1.6 GB consent gate all live in CaptionsButton's local state today. **Mitigation:** move the code verbatim into `captionRun.ts`, and port its behaviours into `captionRun.test.ts` before deleting the component.
6. **Hidden-but-mounted panels behave differently.** They still match Playwright locators, and a component that measures itself while `display:none` gets 0. **Mitigation:** tests assert visibility, not count, and the `active` prop gates measuring and fetching.
7. **WebKit's focus fix-up differs from Chromium's.** WebKit may leave `activeElement` on a `display:none` element. **Mitigation:** the rescue checks element state (§5.3), not `activeElement === body`; one manual WK pass per phase.
8. **Two stores share persistence keys until R6.** `vai.leftW` / `vai.rightW` / `vai.rightPanelOpen` are read by both `store.ts` and `layoutStore`, and a second writer would fight. **Mitigation:** from R1 nothing calls `setPanelSize('leftW'|'rightW')` or `setRightPanelOpen`; R6 deletes them.
9. **Muscle memory.** Text and CC leave the top bar. **Mitigation:** ⌥T; the Text and Captions rail items sit in the same visual column; the tooltips carry the chords. A one-time "Text and Captions moved to the left rail" toast on first launch after upgrade is optional.
10. **Live-region chatter or silence.** Too many captions progress messages annoy VoiceOver users, and too few leave them blind. **Mitigation:** threshold announcements only (§2.8), checked with VoiceOver in the R2 WK pass.
11. **Test churn.** About 16 selectors across 4 Python files, plus `TopBar.test.ts`. They collide with any wave C test edits. **Mitigation:** each change is in the phase that breaks it (§8.1), and R0 rebases the list.
12. **Deep links into AiPanel's internal state.** Expanding a card and opening a group from outside needs a new API. If someone "simplifies" this by rendering an `AiToolCard` inside Captions, form state splits. **Mitigation:** only the `aiJump` request exists, and `deepLinks.test.ts` asserts no `AiToolCard` import outside `AiPanel`.
