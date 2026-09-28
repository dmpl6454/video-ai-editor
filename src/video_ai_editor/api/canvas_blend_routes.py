"""CapCut Canvas backgrounds and overlay blend modes (wave E, lane F2).

* `GET /api/canvas-blend/presets` — the blend-mode menu (id, label, the CSS
  `mix-blend-mode` the live PiP composites with) and the canvas kinds, blur
  strengths and colour swatches, for the Inspector. The table is
  `edl/canvas_blend.py`; `agent/dispatch.set_canvas_background` /
  `set_blend_mode`, the agent tool schemas, the Prompt Editor's validator and
  both renderers read the same module. Read-only, session-independent,
  immutable for a build.
* `POST /api/sessions/{sid}/canvas-bg/upload` — the Inspector's "Choose
  picture…": the file lands in the session's `uploads/images/` (the folder
  the Prompt bar's facts read pictures from), is refused unless Pillow reads
  it as a picture, and its path comes back for `set_canvas_background`.
* `GET /api/sessions/{sid}/canvas-bg/{clip_id}.png?w=&h=` — the picture of a
  clip's IMAGE canvas background cover-fitted to w×h by the exact function
  the render uses (`render/canvas_bg.image_file`), so the engine's shader
  draws the export's pixels. The path is resolved through the session's own
  EDL (never a caller path), like the sticker artwork route.
"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from ..edl.canvas_blend import presets_payload

router = APIRouter(tags=["canvas"])

_RESOLVE_STORE: Callable[[str], Any] | None = None
#: Largest picture the route builds (the engine asks for the canvas size).
_MAX_SIDE = 7680


def configure(*, resolve_store: Callable[[str], Any]) -> None:
    global _RESOLVE_STORE
    _RESOLVE_STORE = resolve_store


@router.get("/api/canvas-blend/presets")
def canvas_blend_presets() -> dict:
    """`{blends: [{id, label, css, porter_duff}], canvas: {kinds, blur_levels,
    blur_default, blur_downscale, swatches, image_exts}}`."""
    return presets_payload()


@router.post("/api/sessions/{sid}/canvas-bg/upload")
async def canvas_bg_upload(sid: str, request: Request, file: UploadFile = File(...)) -> dict:
    if _RESOLVE_STORE is None:            # pragma: no cover - wiring error
        raise HTTPException(500, "canvas routes are not configured")
    from ..edl.canvas_blend import CANVAS_IMAGE_EXTS
    from .. import main as _main          # the upload routes' own naming rules (loaded: it serves us)
    from .uploads import assert_room_for, stream_upload_to
    store = _RESOLVE_STORE(sid)
    safe = _main._safe_filename(file.filename, "picture.png")
    if not safe.lower().endswith(CANVAS_IMAGE_EXTS):
        raise HTTPException(400, f"{safe} is not a picture (use {', '.join(CANVAS_IMAGE_EXTS)})")
    dest = store.dir / "uploads" / "images"
    dest.mkdir(parents=True, exist_ok=True)
    assert_room_for(request, dest)
    dst = _main._unique_upload_path(dest, safe)      # never overwrite (QA-001)
    await stream_upload_to(file, dst)
    from ..render.canvas_bg import FFMPEG_PICTURES, check_picture, pillow_readable
    try:
        if dst.suffix.lower() in FFMPEG_PICTURES:
            # review RE: Pillow here cannot read HEIC, so every iPhone photo was
            # refused. ffmpeg decodes it to a PNG once; the PNG is what the
            # project keeps (one picture in uploads/images, not two).
            png = pillow_readable(dst)
            final = _main._unique_upload_path(dest, dst.stem + ".png")
            png.replace(final)
            dst.unlink(missing_ok=True)
            dst = final
        check_picture(dst)
    except ValueError as e:
        dst.unlink(missing_ok=True)
        raise HTTPException(400, str(e)) from None
    return {"src": str(dst), "name": dst.name}


@router.get("/api/sessions/{sid}/canvas-bg/{clip_id}.png")
def canvas_bg_image(sid: str, clip_id: str, w: int = Query(..., ge=2, le=_MAX_SIDE),
                    h: int = Query(..., ge=2, le=_MAX_SIDE)):
    if _RESOLVE_STORE is None:            # pragma: no cover - wiring error
        raise HTTPException(500, "canvas routes are not configured")
    store = _RESOLVE_STORE(sid)
    hit = store.edl.get_clip(clip_id)
    bg = getattr(hit[1], "canvas_bg", None) if hit else None
    if bg is None or bg.type != "image" or not bg.image:
        raise HTTPException(404, "this clip has no image background")
    from ..config import assert_path_allowed
    from ..render.canvas_bg import image_file
    try:
        src = assert_path_allowed(bg.image)
    except ValueError as e:
        raise HTTPException(403, str(e)) from e
    out = image_file(src, int(w), int(h), cache_dir=store.dir / "cache" / "canvas_bg")
    if out is None:
        raise HTTPException(404, "the background picture is missing or unreadable")
    return FileResponse(str(out), media_type="image/png",
                        headers={"Cache-Control": "private, max-age=3600"})
