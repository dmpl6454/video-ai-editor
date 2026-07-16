# Editor Bug-Fixes Round 2 — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Resolve the 11 issues reported in the 2026-07-11 Mac testing session — double-playhead, project delete, VO recording, media-bin delete, sticker AR burn-in, double subtitle, transform self-drift, panel resizing, export options, export download, and preview↔export WYSIWYG fidelity.

**Architecture:** Everything rides the existing single-mutation-path architecture (`dispatch()` → `commit()` → re-derive render) and the Zustand `store.dispatch()` → `refresh()` frontend mirror. Backend edits are in `agent/dispatch.py` / `render/` / `main.py` / `storage.py`; frontend edits are in `frontend/src/`. No architectural rewrite. Two fixes are operational (rebuild the `.app`), not code.

**Tech Stack:** Python 3.13 / FastAPI / Pydantic v2 (backend), React 19 / TypeScript / Zustand / WebCodecs / HTML5 Canvas (frontend), ffmpeg + Pillow (render), PyInstaller + pywebview (packaging).

**Baseline note:** The working tree currently has a large uncommitted fix pass (the `2026-07-10-editor-issues-verification-and-fixes.md` plan, mostly landed). This plan is written against the **current working tree**, and all line numbers below were re-verified against it on 2026-07-11. Land the current WIP (or at least keep it) before starting; do not revert it.

---

## Issue → Root-cause → Task map

| # | Reported symptom | Root cause | Task | Type |
|---|---|---|---|---|
| 1 | Two playheads; one frozen | `Marker.color` default `#ff4d6d` == playhead color; `refresh()` never resets transient UI state on session switch | Task 1, 2 | Bug |
| 2 | No way to delete a project | No `DELETE /api/sessions/{sid}` route, no UI | Task 3 | Feature |
| 3 | Record voiceover doesn't work | Fix is in source; the installed `.app` predates it | Task 4 | Operational |
| 4 | Media item can't be deleted | Never implemented (frontend or backend) | Task 5 | Feature |
| 5 | Sticker burn-in after AR change | Server sizes stickers off `max(w,h)`, client off `min(w,h)` | Task 6 | Bug (1-line) |
| 6 | Double subtitle | `add_super_text`/`add_text` append with no idempotency | Task 7 | Bug |
| 7 | Transform changes AR "on its own" | `auto_reframe` mutates canvas w/h without `_rescale_overlays_for_canvas_change()` | Task 8 | Bug |
| 8 | No panel resizing | Fixed CSS grid, no splitters | Task 9 | Feature |
| 9 | No export options / unknown format | Single fixed preset; `ExportRequest` accepted but never sent; `crf` is dead code | Task 10, 11 | Feature |
| 10 | Export can't be downloaded | `<a download>` correct but pywebview WKWebView doesn't surface a save; no native-dialog fallback | Task 12 | Bug (platform) |
| 11 | Export ≠ preview (WYSIWYG) | Two independent text renderers; frontend never loads the bundled font files (no `@font-face`) | Task 13 | Bug (fidelity) |

**Recommended order:** cheap high-confidence bug fixes first (6, 5, 7), then the medium bugs (1+2, 11), then features (3-operational, 4, 8, 9, 10). Each task is independently shippable.

---

## Task 1: Fix marker/playhead color collision

**Files:**
- Modify: `src/video_ai_editor/agent/dispatch.py` (add_marker, ~line 1539)
- Modify: `frontend/src/components/Timeline.tsx:306,317` (marker fallback color)
- Test: `tests/test_tools_dispatch.py`

**Root cause:** `add_marker` defaults `color` to `#ff4d6d` (the exact playhead stroke color, `Timeline.tsx` playhead overlay). A marker created with no explicit color is visually indistinguishable from the (static) playhead line, so a session with a leftover marker shows "two playheads, one frozen." The Timeline fallback (`m.color ?? '#fbbf24'`) is amber — the collision only happens because the *backend* stamps the red default.

**Step 1: Write the failing test**

In `tests/test_tools_dispatch.py`, add:

```python
def test_add_marker_default_color_is_not_playhead_red(edl_store):
    from video_ai_editor.agent.dispatch import dispatch
    res = dispatch(edl_store, "add_marker", {"time": 1.0})
    mid = res["marker_id"]
    marker = next(m for m in edl_store.edl.markers if m.id == mid)
    # Must not collide with the timeline playhead color (#ff4d6d).
    assert marker.color.lower() != "#ff4d6d"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tools_dispatch.py::test_add_marker_default_color_is_not_playhead_red -v`
Expected: FAIL (color equals `#ff4d6d`).

**Step 3: Change the default marker color**

In `dispatch.py` `add_marker`, change:
```python
        color=str(args.get("color", "#ff4d6d")),
```
to:
```python
        color=str(args.get("color", "#fbbf24")),  # amber — must differ from the playhead red (#ff4d6d)
```

Also update the schema default for consistency — in `src/video_ai_editor/edl/schema.py:176`:
```python
    color: str = "#fbbf24"
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tools_dispatch.py::test_add_marker_default_color_is_not_playhead_red -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_tools_dispatch.py src/video_ai_editor/agent/dispatch.py src/video_ai_editor/edl/schema.py
git commit -m "fix(timeline): marker default color no longer collides with playhead red"
```

---

## Task 2: Reset transient UI state on session switch

**Files:**
- Modify: `frontend/src/store.ts` — `refresh()` (~line 228-233) and `switchSession` in `frontend/src/components/TopBar.tsx:102-107`
- Test: manual (frontend state; no unit harness for the store) — see verification note

**Root cause:** `refresh()` overwrites only `edl/ops/sessionName/redoAvailable`. `playhead`, `selection`, `multiSelection`, `inMark`, `outMark` carry over from the previous session — a stale playhead position from session A shows up on session B's timeline (compounding the marker collision above).

**Step 1: Add a `resetTransient` action and call it on session switch**

In `store.ts`, add to the store interface (near line 53):
```ts
  resetTransient(): void
```

Add the implementation (near `setPlayhead`, ~line 140):
```ts
  // Clears per-session view/selection state. Call when switching sessions so a
  // stale playhead/selection/marks from the previous project don't bleed onto
  // the new timeline (which read as "a second frozen playhead").
  resetTransient: () => set({
    playhead: 0,
    selection: null,
    multiSelection: [],
    inMark: null,
    outMark: null,
  }),
```

**Step 2: Call it from switchSession**

In `TopBar.tsx:102-107`, change:
```ts
  const switchSession = async (newId: string) => {
    setPickerOpen(false)
    if (newId === sid) return
    useStore.setState({ sessionId: newId, sessionName: newId })
    await refresh()
  }
```
to:
```ts
  const switchSession = async (newId: string) => {
    setPickerOpen(false)
    if (newId === sid) return
    useStore.getState().resetTransient()
    useStore.setState({ sessionId: newId, sessionName: newId })
    await refresh()
  }
```

Also call `resetTransient()` inside `store.ts` `init()` right before the first `refresh()` (defensive; harmless on first load).

**Step 3: Typecheck + manual verify**

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors.

Manual: open two sessions (one with clips, one empty), scrub the playhead in A, switch to B → playhead is at 0, no leftover selection. Switch back to A → still 0 (acceptable; per-session playhead persistence is out of scope).

**Step 4: Commit**

```bash
git add frontend/src/store.ts frontend/src/components/TopBar.tsx
git commit -m "fix(store): reset playhead/selection/marks on session switch"
```

---

## Task 3: Delete-project capability (backend route + frontend UI)

**Files:**
- Modify: `src/video_ai_editor/storage.py` — add `delete_session()`
- Modify: `src/video_ai_editor/main.py` — add `DELETE /api/sessions/{sid}`; evict from `_STORES`
- Modify: `frontend/src/api.ts` — add `deleteSession`
- Modify: `frontend/src/components/TopBar.tsx` — add a delete affordance per session row in the picker, with confirm
- Test: `tests/test_config_paths.py` or a new `tests/test_session_delete.py`

**Root cause:** No delete exists anywhere. Sessions are directories under `WORKDIR/s_*`; deletion = remove the dir + evict the `_STORES` cache entry.

**Step 1: Write the failing backend test**

Create `tests/test_session_delete.py`:
```python
import shutil
from fastapi.testclient import TestClient


def test_delete_session_removes_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VAI_WORKDIR", str(tmp_path))
    import importlib
    from video_ai_editor import config, storage
    importlib.reload(config)
    importlib.reload(storage)
    from video_ai_editor import main as m
    importlib.reload(m)
    try:
        client = TestClient(m.app)
        sid = client.post("/api/sessions").json()["id"]
        assert storage.session_exists(sid)
        r = client.delete(f"/api/sessions/{sid}")
        assert r.status_code == 200
        assert not storage.session_exists(sid)
    finally:
        # Restore module state so later tests aren't poisoned (see CLAUDE.md).
        monkeypatch.delenv("VAI_WORKDIR", raising=False)
        importlib.reload(config)
        importlib.reload(storage)
        importlib.reload(m)
```

> Note: confirm the env var name for WORKDIR override in `config.py` before running — if it isn't `VAI_WORKDIR`, adapt. If there's no override, build the session dir directly under the default `WORKDIR` and clean up in `finally`.

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_session_delete.py -v`
Expected: FAIL (405 Method Not Allowed / no route).

**Step 3: Implement `delete_session` in storage.py**

Add to `storage.py`:
```python
import shutil

def delete_session(session_id: str) -> bool:
    """Remove a session directory and all its media/state. Returns True if it
    existed. Idempotent — deleting a missing session is a no-op returning False."""
    d = WORKDIR / session_id
    if not d.exists():
        return False
    shutil.rmtree(d, ignore_errors=False)
    return True
```

**Step 4: Add the route in main.py**

After `get_session` (~line 288), add:
```python
@app.delete("/api/sessions/{sid}")
def delete_session_route(sid: str):
    from .storage import delete_session
    with _STORES_LOCK:
        _STORES.pop(sid, None)  # drop the cached store so it can't resurrect the dir
    existed = delete_session(sid)
    if not existed:
        raise HTTPException(404, {"code": "not_found", "message": "session not found"})
    return {"deleted": sid}
```

**Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_session_delete.py -v`
Expected: PASS.

**Step 6: Frontend api client**

In `frontend/src/api.ts`, add near `getSession`:
```ts
  deleteSession: (sid: string) => http<{ deleted: string }>('DELETE', `/sessions/${sid}`),
```

**Step 7: Frontend UI in the picker**

In `TopBar.tsx`, in the session-list rows (~line 160-185), add a small "×" delete button per row (not on "+ New project" / "Open .vae"). On click:
```ts
const removeSession = async (id: string, e: React.MouseEvent) => {
  e.stopPropagation()  // don't trigger switchSession
  if (!window.confirm(`Delete project ${id}? This removes its media and history permanently.`)) return
  await api.deleteSession(id)
  const list = await api.listSessions()
  setSessions(list.sessions)
  // If we deleted the active session, switch to the newest remaining, or create one.
  if (id === sid) {
    const next = list.sessions[0]?.id ?? (await api.createSession()).id
    await switchSession(next)
  }
}
```
Wire `onClick={(e) => removeSession(s.id, e)}` on the delete button; ensure the row's own `onClick` (switchSession) is on the row body, not the button.

**Step 8: Typecheck + commit**

Run: `cd frontend && npx tsc --noEmit`
```bash
git add src/video_ai_editor/storage.py src/video_ai_editor/main.py frontend/src/api.ts frontend/src/components/TopBar.tsx tests/test_session_delete.py
git commit -m "feat(sessions): delete-project route + picker UI with confirm"
```

---

## Task 4: Ship the VO-recording fix (operational — rebuild the app)

**Files:** none (code fix is already in `build_app.sh:67-74` and `VoRecorder.tsx`)

**Root cause:** `build_app.sh` already adds `NSMicrophoneUsageDescription` via PlistBuddy post-build, and `VoRecorder.tsx` already shows a clear message. But `dist/Video AI Editor.app` on disk was built **2026-07-04**, before the fix; its `Info.plist` has no such key. The user is testing a stale bundle.

**Step 1: Rebuild the app**

```bash
rm -rf "dist/Video AI Editor.app"
uv run bash build_app.sh
```

**Step 2: Verify the entitlement landed**

Run:
```bash
/usr/libexec/PlistBuddy -c "Print :NSMicrophoneUsageDescription" "dist/Video AI Editor.app/Contents/Info.plist"
```
Expected: `Record a voiceover track for your video.` (not "Does Not Exist").

**Step 3: Manual verify**

Launch the rebuilt `.app`, open Record Voiceover. macOS should now show the mic-permission prompt (first run). Grant it; recording works. (If it still fails, the fallback is the server-side `POST /api/sessions/{sid}/vo_record` endpoint — VO can be uploaded from a recorded blob; but the in-window capture should now work.)

**Step 4: No commit** (build artifact is gitignored). Optionally note the rebuild requirement in release docs.

---

## Task 5: Media-bin item delete

**Files:**
- Modify: `frontend/src/components/MediaBin.tsx:105-123` (the `.item` block)
- Test: manual (frontend)

**Root cause:** `sources` is derived purely from clips on the timeline (`MediaBin.tsx:34-39`) — a media-bin item *is* "N clips referencing this src." There is no separate upload registry. So "delete this media" = remove every clip with that `src`. The `bulk_delete` dispatch tool already exists (`store.ts` exposes it). No backend change needed.

**Step 1: Add a remove button to each media item**

In `MediaBin.tsx`, inside the `.item` block, after the "×N on timeline" line, add a button. First collect the clip ids per source (extend the `sources` map to also track ids):

Change the sources derivation (~line 34-39) to:
```ts
  // Unique source paths → the clip ids that reference them (for delete + count).
  const sources = new Map<string, string[]>()
  for (const t of edl?.tracks ?? []) {
    for (const c of t.clips) {
      if (isMediaClip(c)) {
        const arr = sources.get(c.src) ?? []
        arr.push(c.id)
        sources.set(c.src, arr)
      }
    }
  }
```

Update the render map (`[...sources.entries()].map(([src, n]) => ...`) to `([src, ids]) =>`, use `ids.length` where `n` was used, and add:
```tsx
          <button
            className="media-remove"
            title="Remove this media and all its clips from the timeline"
            onClick={async (e) => {
              e.stopPropagation()
              if (!window.confirm(`Remove ${src.split('/').pop()} and its ${ids.length} clip(s) from the timeline?`)) return
              await dispatch('bulk_delete', { clip_ids: ids })
            }}
          >×</button>
```

> Confirm `bulk_delete`'s arg key — check `dispatch.py` for whether it expects `clip_ids` or `ids`; adapt. If `bulk_delete` doesn't exist for this shape, loop `ripple_delete` per id back-to-front (the same pattern `remove_silences` uses) so timeline coords stay valid.

**Step 2: Style the button**

In `frontend/src/styles.css`, add:
```css
.media-bin .item { position: relative; }
.media-remove {
  position: absolute; top: 6px; right: 6px;
  border: none; background: transparent; color: var(--text-dim);
  cursor: pointer; font-size: 14px; line-height: 1; padding: 2px 4px;
}
.media-remove:hover { color: var(--bad, #ff4d6d); }
```

**Step 3: Typecheck + manual verify + commit**

Run: `cd frontend && npx tsc --noEmit`
Manual: media item shows "×"; clicking it (after confirm) removes all clips for that source; the item disappears from the bin (since `sources` is re-derived from a now-empty clip set).
```bash
git add frontend/src/components/MediaBin.tsx frontend/src/styles.css
git commit -m "feat(media-bin): remove media (and its clips) from the timeline"
```

---

## Task 6: Fix sticker size mismatch (client uses min-edge, server uses max-edge)

**Files:**
- Modify: `frontend/src/lib/overlay.ts:74`
- Test: manual (visual) + a small unit assertion if a test harness for `stickerGeom` exists

**Root cause:** Server (`text_overlay.py:275,350`) sizes stickers off `max(canvas_w, canvas_h)` (long edge). Client (`overlay.ts:74`) uses `Math.min(canvasW, canvasH)` (short edge). They agree only on near-square canvases; `set_aspect_ratio` swaps which edge is long/short, so the draggable client glyph and the server-baked PNG diverge in size → "a second differently-sized emoji burned in."

**Step 1: Make the client match the server**

In `overlay.ts:74`, change:
```ts
  const baseSize = Math.min(canvasW, canvasH) * 0.22 * scale
```
to:
```ts
  // Match the server's sticker sizing (render/text_overlay.py: base = max(w,h)).
  // Using min() here made the client glyph and the server-baked PNG diverge in
  // size after an aspect-ratio change (they only agreed on square canvases).
  const baseSize = Math.max(canvasW, canvasH) * 0.22 * scale
```

**Step 2: Typecheck + manual verify**

Run: `cd frontend && npx tsc --noEmit`
Manual: place an emoji at 9:16, switch to 16:9 (Reels/landscape) → the draggable glyph and the rendered video sticker are now the same size, no second "burned-in" copy visible.

**Step 3: Commit**

```bash
git add frontend/src/lib/overlay.ts
git commit -m "fix(stickers): size off canvas long edge to match server render"
```

---

## Task 7: Make `add_super_text` / `add_text` idempotent

**Files:**
- Modify: `src/video_ai_editor/agent/dispatch.py` — `add_super_text` (~557-575), `add_text` (~2443-2476)
- Test: `tests/test_tools_dispatch.py`

**Root cause:** Both handlers `track.clips.append(clip)` unconditionally. Two calls with overlapping time produce two stacked TextClips at different role/font sizes — the "RIPPLE TEST" + "RIPPLE TEST 2" double-subtitle. `apply_hook_stack` (line 609+) already models the fix: it purges prior clips of the same role before adding.

**Design decision:** Do NOT auto-dedupe on text *content* (users legitimately want two different captions). Instead, add an explicit `replace: bool` arg (default False) that, when True, removes prior clips of the same role on the same track whose time window overlaps. Additionally, guard against the accidental exact-duplicate: if an incoming clip has identical `text`, `role`, `start`, and `end` to an existing one on the track, skip the append (idempotent re-run). This kills the accidental double-add without blocking intentional distinct captions.

**Step 1: Write the failing test**

```python
def test_add_super_text_dedupes_exact_duplicate(edl_store):
    from video_ai_editor.agent.dispatch import dispatch
    args = {"text": "RIPPLE TEST", "role": "super", "start": 0.0, "end": 3.0}
    dispatch(edl_store, "add_super_text", dict(args))
    dispatch(edl_store, "add_super_text", dict(args))  # identical re-run
    supers = [c for t in edl_store.edl.tracks for c in t.clips
              if getattr(c, "text", None) == "RIPPLE TEST"]
    assert len(supers) == 1, "identical add_super_text must not stack duplicates"


def test_add_super_text_allows_distinct_text(edl_store):
    from video_ai_editor.agent.dispatch import dispatch
    dispatch(edl_store, "add_super_text", {"text": "A", "role": "super", "start": 0.0, "end": 3.0})
    dispatch(edl_store, "add_super_text", {"text": "B", "role": "super", "start": 0.0, "end": 3.0})
    supers = [c for t in edl_store.edl.tracks for c in t.clips if getattr(c, "text", None) in ("A", "B")]
    assert len(supers) == 2, "distinct captions must both survive"
```

**Step 2: Run tests to verify the first fails**

Run: `uv run pytest tests/test_tools_dispatch.py -k add_super_text -v`
Expected: `test_add_super_text_dedupes_exact_duplicate` FAILS (2 clips), the "distinct" one passes.

**Step 3: Add the dedupe guard**

In `add_super_text`, before `track.clips.append(clip)` (~line 572), insert:
```python
    # Idempotency: an identical re-run (same text/role/start/end on this track)
    # must not stack a second overlay — that produced the "double subtitle" bug.
    def _same(existing) -> bool:
        return (
            isinstance(existing, TextClip)
            and getattr(existing, "text", None) == clip.text
            and getattr(existing, "role", None) == clip.role
            and abs(existing.start - clip.start) < 1e-6
            and abs(existing.end - clip.end) < 1e-6
        )
    if any(_same(c) for c in track.clips):
        summary = f"Text already present: {clip.text!r} @ {clip.start:.2f}s"
        return {"summary": summary}  # no commit — nothing changed
    # Optional explicit replace: drop prior same-role overlapping clips.
    if bool(args.get("replace", False)):
        track.clips = [
            c for c in track.clips
            if not (isinstance(c, TextClip) and getattr(c, "role", None) == clip.role
                    and c.start < clip.end and c.end > clip.start)
        ]
    track.clips.append(clip)
```

Apply the same exact-duplicate guard to `add_text` (~line 2443-2476), keyed on its own text/start/end fields (no `role` if `add_text` clips don't carry one — check the TextClip constructed there and adapt the predicate).

**Step 4: Run tests to verify both pass**

Run: `uv run pytest tests/test_tools_dispatch.py -k add_super_text -v`
Expected: both PASS.

Run the full smoke suite to ensure no regression:
Run: `uv run pytest tests/test_all_tools_smoke.py -v`
Expected: PASS.

**Step 5: Add the `replace` arg to the tool schema (optional but recommended)**

In `agent/tools.py`, find the `add_super_text` / `add_text` schema (via `_t(...)`) and add an optional `replace` boolean property so Claude can opt into replacement. Keep the name string matching the DISPATCH key exactly (CLAUDE.md: two-file edit, no shared source of truth).

**Step 6: Commit**

```bash
git add src/video_ai_editor/agent/dispatch.py src/video_ai_editor/agent/tools.py tests/test_tools_dispatch.py
git commit -m "fix(text): dedupe identical text overlays + optional replace (double-subtitle)"
```

---

## Task 8: Make `auto_reframe` rescale overlays like `set_canvas`

**Files:**
- Modify: `src/video_ai_editor/agent/dispatch.py` — `auto_reframe` (~2689-2722, canvas mutate at 2699-2700)
- Test: `tests/test_canvas_rescale_overlays.py` (exists) or `tests/test_tools_dispatch.py`

**Root cause:** `auto_reframe` sets `store.edl.canvas.w/h` directly but — unlike `set_canvas` (line 503) and `set_aspect_ratio` (line 519) — never calls `_rescale_overlays_for_canvas_change()`. Any overlay/transform authored against the old canvas is left un-rescaled, so framing "changes on its own" after a reframe (which the user may not even realize changes the canvas).

**Step 1: Write the failing test**

In `tests/test_canvas_rescale_overlays.py` (or a new test), add:
```python
def test_auto_reframe_rescales_overlay_positions(edl_store_with_sticker):
    """A sticker at a given (x,y) must be proportionally repositioned when
    auto_reframe changes the canvas dimensions, not left at old coords."""
    from video_ai_editor.agent.dispatch import dispatch
    store = edl_store_with_sticker  # fixture: canvas 1080x1920, sticker at x=540,y=1440
    old_w, old_h = store.edl.canvas.w, store.edl.canvas.h
    sticker = next(c for t in store.edl.tracks for c in t.clips if type(c).__name__ == "Sticker")
    old_x = sticker.transform.x
    dispatch(store, "auto_reframe", {"ratio": "16:9"})  # -> 1920x1080
    new_w = store.edl.canvas.w
    # x should scale by new_w/old_w
    assert abs(sticker.transform.x - old_x * (new_w / old_w)) < 1e-3
```

> Adapt to whatever fixture builds a sticker in the existing test file; if none exists, dispatch `add_sticker` first. Confirm `auto_reframe`'s arg name (`ratio` vs `aspect`) from `dispatch.py`.

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_canvas_rescale_overlays.py -k auto_reframe -v`
Expected: FAIL (x unchanged).

**Step 3: Add the rescale call**

In `auto_reframe`, around lines 2699-2700, change:
```python
    store.edl.canvas.w = w
    store.edl.canvas.h = h
```
to:
```python
    old_w, old_h = store.edl.canvas.w, store.edl.canvas.h
    store.edl.canvas.w = w
    store.edl.canvas.h = h
    _rescale_overlays_for_canvas_change(store.edl, old_w, old_h, w, h)
```
(`_rescale_overlays_for_canvas_change` is defined at line 149 in the same module — no import needed.)

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_canvas_rescale_overlays.py -k auto_reframe -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add src/video_ai_editor/agent/dispatch.py tests/test_canvas_rescale_overlays.py
git commit -m "fix(auto_reframe): rescale overlay/clip transforms on canvas change"
```

---

## Task 9: Resizable panels (left, right, timeline height)

**Files:**
- Modify: `frontend/src/styles.css:35-48` (`.app` grid) and `:107-113` (`.center`)
- Modify: `frontend/src/App.tsx` (render splitter handles, wire CSS vars)
- Modify: `frontend/src/store.ts` (persist widths to localStorage)
- Create: `frontend/src/components/Splitter.tsx`
- Test: manual (frontend)

**Root cause:** Layout is a hardcoded CSS grid (`220px 1fr 280px` columns, `1fr 280px` rows for the center). No splitter code exists. This is net-new feature work.

**Step 1: Convert the grid to CSS variables**

In `styles.css:35-48`, change:
```css
  grid-template-columns: 220px 1fr 280px;
```
to:
```css
  grid-template-columns: var(--left-w, 220px) 1fr var(--right-w, 280px);
```
In `.center` (~line 107-113):
```css
  grid-template-rows: 1fr var(--timeline-h, 280px);
```

**Step 2: Create a reusable Splitter component**

Create `frontend/src/components/Splitter.tsx`:
```tsx
import { useCallback, useRef } from 'react'

type Props = {
  orientation: 'vertical' | 'horizontal'   // vertical = drags left/right; horizontal = drags up/down
  onDelta: (deltaPx: number) => void
  onCommit?: () => void
}

/** A thin drag handle. Uses window-level listeners so a drag that leaves the
 *  handle bounds still resolves (same pattern as the timeline playhead drag). */
export function Splitter({ orientation, onDelta, onCommit }: Props) {
  const startRef = useRef(0)
  const onDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    startRef.current = orientation === 'vertical' ? e.clientX : e.clientY
    const move = (ev: MouseEvent) => {
      const pos = orientation === 'vertical' ? ev.clientX : ev.clientY
      onDelta(pos - startRef.current)
      startRef.current = pos
    }
    const up = () => {
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', up)
      onCommit?.()
    }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
  }, [orientation, onDelta, onCommit])

  return (
    <div
      className={`splitter splitter-${orientation}`}
      onMouseDown={onDown}
      role="separator"
      aria-orientation={orientation === 'vertical' ? 'vertical' : 'horizontal'}
    />
  )
}
```

**Step 3: Add panel-width state to the store (persisted)**

In `store.ts`, add to state + initial values:
```ts
  leftW: number
  rightW: number
  timelineH: number
  setPanelSize(key: 'leftW' | 'rightW' | 'timelineH', px: number): void
```
Initial values (read localStorage, clamp):
```ts
  leftW: Number(localStorage.getItem('vai.leftW')) || 220,
  rightW: Number(localStorage.getItem('vai.rightW')) || 280,
  timelineH: Number(localStorage.getItem('vai.timelineH')) || 280,
```
Setter:
```ts
  setPanelSize: (key, px) => {
    const clamped = Math.max(160, Math.min(640, px))
    localStorage.setItem(`vai.${key}`, String(clamped))
    set({ [key]: clamped } as Partial<State>)
  },
```

**Step 4: Render splitters and apply CSS vars in App.tsx**

In `App.tsx`, read `leftW/rightW/timelineH` and set them as CSS custom properties on the `.app` element's `style`, e.g.:
```tsx
<div className="app" style={{
  '--left-w': `${leftW}px`,
  '--right-w': `${rightW}px`,
  '--timeline-h': `${timelineH}px`,
} as React.CSSProperties}>
```
Place a `<Splitter orientation="vertical" onDelta={(d) => setPanelSize('leftW', leftW + d)} />` between the left sidebar and center, another between center and right sidebar (its delta is negated: `setPanelSize('rightW', rightW - d)`), and a `<Splitter orientation="horizontal" onDelta={(d) => setPanelSize('timelineH', timelineH - d)} />` between the preview and timeline panes. (Exact placement depends on the current JSX tree — the splitter must sit on the grid gap; you may need a 4px grid gap or absolutely-positioned handles.)

**Step 5: Style the splitters**

In `styles.css`:
```css
.splitter { background: transparent; z-index: 5; }
.splitter:hover { background: var(--accent, #5b8dff); }
.splitter-vertical { width: 5px; cursor: col-resize; }
.splitter-horizontal { height: 5px; cursor: row-resize; }
```

**Step 6: Typecheck + manual verify + commit**

Run: `cd frontend && npx tsc --noEmit && npx vite build`
Manual: drag each handle; panels resize; reload → widths persist. Canvas/timeline re-measure correctly (they use `ResizeObserver` already).
```bash
git add frontend/src/components/Splitter.tsx frontend/src/App.tsx frontend/src/store.ts frontend/src/styles.css
git commit -m "feat(layout): drag-resizable left/right/timeline panels, persisted"
```

---

## Task 10: Wire the export options UI (resolution + quality)

**Files:**
- Modify: `frontend/src/components/TopBar.tsx` — add an export-options popover before calling `doExport`
- Modify: `frontend/src/store.ts` — `doExport(opts)` already accepts `{ height, crf }`; pass through
- Test: manual + confirm the request body reaches the backend

**Root cause:** `ExportRequest{height, fps, crf}` exists on the backend (`main.py:175-178`) and `store.ts doExport(opts)` accepts `{ height, crf }`, but `TopBar.tsx` calls `doExport()` with no args, so it's always defaults. Also `crf` is currently dead code on the backend (see Task 11b). Add a small popover so the user can pick resolution + quality.

**Step 1: Add export options state + popover in TopBar**

Add local state:
```ts
const [showOpts, setShowOpts] = useState(false)
const [exportHeight, setExportHeight] = useState<number>(edl?.canvas?.h ?? 1080)
const [exportCrf, setExportCrf] = useState<number>(18)
```
Change the Export button to open the popover instead of exporting directly; the popover has:
- a resolution `<select>` (e.g. 2160/1440/1080/720/480, plus "Source" = `edl.canvas.h`),
- a quality `<select>` or slider mapping to CRF (e.g. High=18, Medium=23, Small=28),
- an "Export" confirm button that calls `doExport({ height: exportHeight, crf: exportCrf })` and closes the popover.

Render it in a portal to `document.body` (per the R7 dropdown-portal pattern in the prior plan) so it isn't clipped by `overflow:hidden`.

**Step 2: Confirm doExport forwards the opts**

In `store.ts doExport` (line 354), verify the POST body includes `height` and `crf` from `opts`. If it currently drops them, add them to the request payload sent to `POST /api/sessions/{sid}/export`.

**Step 3: Manual verify + commit**

Manual: open export options, pick 720p / Small, export → the output file is 720p and visibly smaller. (Requires Task 11b so `crf` actually affects the encode.)
```bash
git add frontend/src/components/TopBar.tsx frontend/src/store.ts
git commit -m "feat(export): resolution + quality options popover"
```

---

## Task 11: Preview↔export fidelity — fonts + honor `crf`

This is two sub-fixes. **11a** is the WYSIWYG-critical one (fonts); **11b** removes the dead `crf` code path so Task 10's quality control works.

### Task 11a: Load the bundled fonts in the browser so preview matches export

**Files:**
- Modify: `frontend/src/styles.css` (or a new `fonts.css`) — add `@font-face` rules
- Modify: `frontend/index.html` or the Vite build so the TTFs are served
- Copy/reference: `fonts/Anton-Regular.ttf`, `fonts/BebasNeue-Regular.ttf`, `fonts/Montserrat-Bold.ttf`, `fonts/Inter-*.ttf`
- Test: manual (visual A/B of preview vs export)

**Root cause:** The server bakes text with the real bundled TTFs (Pillow). The client's `TextLayer.tsx` sets `ctx.font` to CSS family names (`'Anton'`, `'Bebas Neue'`, `'Montserrat'`) that are **never loaded in the browser** (no `@font-face` anywhere in the frontend), so the live preview silently falls back to system sans-serif — a guaranteed preview↔export mismatch.

**Step 1: Make the TTFs available to the frontend build**

Copy the font files into `frontend/public/fonts/` (Vite serves `public/` at the web root), OR add a Vite asset alias. Files needed (match `text_overlay.py ROLE_STYLES`): `Anton-Regular.ttf`, `BebasNeue-Regular.ttf`, `Montserrat-Bold.ttf`, `Inter-Black.ttf`, `Inter-Bold.ttf`.

```bash
mkdir -p frontend/public/fonts
cp fonts/Anton-Regular.ttf fonts/BebasNeue-Regular.ttf fonts/Montserrat-Bold.ttf fonts/Inter-Black.ttf fonts/Inter-Bold.ttf frontend/public/fonts/
```

**Step 2: Add @font-face rules**

Create `frontend/src/fonts.css` (import it from the app entry, e.g. `main.tsx`):
```css
@font-face { font-family: 'Anton';      src: url('/fonts/Anton-Regular.ttf') format('truetype');      font-weight: 400; font-display: block; }
@font-face { font-family: 'Bebas Neue'; src: url('/fonts/BebasNeue-Regular.ttf') format('truetype');  font-weight: 400; font-display: block; }
@font-face { font-family: 'Montserrat'; src: url('/fonts/Montserrat-Bold.ttf') format('truetype');     font-weight: 700; font-display: block; }
@font-face { font-family: 'Inter';      src: url('/fonts/Inter-Bold.ttf') format('truetype');          font-weight: 700; font-display: block; }
@font-face { font-family: 'Inter';      src: url('/fonts/Inter-Black.ttf') format('truetype');         font-weight: 900; font-display: block; }
```

**Step 3: Ensure the canvas waits for fonts before drawing**

Canvas `ctx.font` doesn't trigger font loading. In `TextLayer.tsx` (or `Preview.tsx` where the canvas draws), before the first text draw, `await document.fonts.ready` (or `document.fonts.load('700 56px Montserrat')` for each role) and re-draw once fonts resolve. Add a `fontsReady` state that flips true on `document.fonts.ready`, and include it in the draw effect deps so the overlay repaints with the real fonts.

**Step 4: Reconcile sizing math (verify, adjust if needed)**

Server sizes are fixed px per role (Anton 140, Bebas 170, Montserrat 56, Inter caption 64) against the canvas; client uses `fontPx = Math.round(s.size * height)` (fractions of preview height). These must resolve to the same on-canvas proportion. Verify one role visually; if they differ, align the client fraction to `serverPx / canvas.h` so preview and export match. Document the mapping in a comment.

**Step 5: Manual verify + commit**

Manual: add a hook + caption, compare the live preview against an exported frame at the same timecode — fonts, size, and position should now match closely.
```bash
git add frontend/public/fonts frontend/src/fonts.css frontend/src/main.tsx frontend/src/components/TextLayer.tsx
git commit -m "fix(preview): load bundled fonts in browser so preview matches export"
```

### Task 11b: Honor `crf` in the export encoder args

**Files:**
- Modify: `src/video_ai_editor/render/compositor.py` — `render_export` (~797-816) and `_render` / `_video_encoder_args` (~128-163)
- Test: `tests/` (assert crf threads through) or manual

**Root cause:** `render_export` accepts `crf`/`preset` (from `ExportRequest`) but never forwards them into `_render` → `_video_encoder_args`, which hardcodes its own quality values. So the export quality knob is dead.

**Step 1: Thread `crf` through**

In `render_export` (compositor.py:813), change the `_render(...)` call to forward `crf`. In `_render`'s signature add `crf: int | None = None`, and in `_video_encoder_args` accept an optional `crf` override that replaces the hardcoded `-crf 20` / `-cq` / `-q:v` value for the software (libx264) path (map crf to the HW-encoder equivalent where sensible, or only honor it for libx264 and document the HW limitation).

**Step 2: Manual/unit verify + commit**

Verify a lower crf (higher quality) yields a larger file and vice versa.
```bash
git add src/video_ai_editor/render/compositor.py
git commit -m "fix(export): honor ExportRequest.crf in encoder args (was dead code)"
```

---

## Task 12: Export download fallback for the pywebview window

**Files:**
- Modify: `src/video_ai_editor/desktop.py` (add a `js_api` bridge with a native save dialog)
- Modify: `frontend/src/store.ts` `triggerDownload` (~441-452) — detect pywebview and call the bridge
- Test: manual (packaged app)

**Root cause:** `<a download>` is implemented correctly (created, appended, clicked, valid same-origin URL) and works in a real browser. But `desktop.py` passes no `js_api` to pywebview and never calls `window.create_file_dialog(...)` — the packaged WKWebView has no reliable way to surface an OS save dialog for a `blob:`/download anchor, so nothing appears to happen. Browser-dev mode is unaffected.

**Design:** Expose a Python `save_export(url)` (or `reveal(path)`) over pywebview's `js_api`. The frontend, when it detects it's inside pywebview (`window.pywebview`), calls the bridge instead of the anchor click; the bridge uses `webview.windows[0].create_file_dialog(webview.SAVE_DIALOG, ...)` to prompt, then copies the exported file from the session `exports/` dir to the chosen path (and/or reveals it in Finder/Explorer).

**Step 1: Add the js_api bridge in desktop.py**

```python
import shutil
import webview
from .config import WORKDIR  # or the storage helper for session dirs

class _Api:
    def save_export(self, session_id: str, filename: str) -> str | None:
        """Copy an exported file to a user-chosen location via the native save
        dialog. Returns the chosen path, or None if cancelled."""
        src = WORKDIR / session_id / "exports" / filename
        if not src.exists():
            return None
        win = webview.windows[0]
        dest = win.create_file_dialog(
            webview.SAVE_DIALOG, save_filename=filename,
        )
        if not dest:
            return None
        dest_path = dest if isinstance(dest, str) else dest[0]
        shutil.copy2(src, dest_path)
        return dest_path
```
Pass it to the window:
```python
    webview.create_window(
        title="Video AI Editor",
        url=url,
        width=1480, height=920,
        min_size=(1100, 700),
        easy_drag=False,
        js_api=_Api(),
    )
```

> Note: confirm the exact session-dir path helper (`storage.session_dir`) and the exports filename shape (`export_{h}.mp4`) so `src` resolves. Also confirm pywebview 6.2.1's `create_file_dialog` signature.

**Step 2: Call the bridge from the frontend when in pywebview**

In `store.ts` `triggerDownload` (or where `doExport` completes), detect pywebview and prefer the bridge:
```ts
async function triggerDownload(url: string, filename: string, sessionId: string | null) {
  const py = (window as unknown as { pywebview?: { api?: { save_export?: (sid: string, fn: string) => Promise<string | null> } } }).pywebview
  if (py?.api?.save_export && sessionId) {
    const saved = await py.api.save_export(sessionId, filename)
    if (saved) { toast.success(`Saved to ${saved}`); return }
    // fall through to anchor if cancelled/failed
  }
  const a = document.createElement('a')
  a.href = url; a.download = filename; a.style.display = 'none'
  document.body.appendChild(a); a.click(); a.remove()
}
```
Pass `sessionId` from `doExport`'s call site.

**Step 3: Rebuild + manual verify (packaged app only)**

```bash
uv run bash build_app.sh
```
Launch the rebuilt `.app`, export → a native Save dialog appears; choosing a location writes the mp4 there. In browser-dev mode the anchor path still works unchanged.

**Step 4: Commit**

```bash
git add src/video_ai_editor/desktop.py frontend/src/store.ts
git commit -m "feat(desktop): native save dialog for exports in the packaged app"
```

---

## Final verification pass (after all tasks)

Run the exact checks CI runs (CLAUDE.md — note `npm run build` is stricter and NOT what CI runs; run both to be safe):

```bash
# Frontend (CI's checks)
cd frontend && npx tsc --noEmit && npx vite build && npm run lint && cd ..
# Backend (full suite)
uv run pytest
# Stricter frontend build (catches tsc -b project-reference errors CI misses)
cd frontend && npm run build && cd ..
```

Then push and watch the **windows-latest** CI job (the source of truth for cross-platform behavior). New backend behaviors (session delete, text dedupe, auto_reframe rescale, crf threading) need their tests green on both ubuntu and windows runners. `test_all_tools_smoke.py` auto-covers any new DISPATCH tool.

**Before committing anything:** revert any incidental `uv.lock` drift (`git checkout uv.lock` if `uv run` rewrote it) so diffs stay scoped — see the project memory on uv.lock drift.

---

## Out of scope (explicitly deferred)

- Realtime GPU preview / eliminating the render-then-poll model.
- Per-session playhead persistence (Task 2 resets to 0 on switch — acceptable).
- HW-encoder CRF parity (Task 11b honors crf for libx264; HW encoders keep their quality ladder).
- Chunk cache under transitions (unchanged, still disabled when xfade present).
