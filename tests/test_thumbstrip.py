"""GET /api/sessions/{sid}/thumbstrip — filmstrip sprites (QA-059).

One JPEG carries `n` filmstrip tiles side by side, tile i of page p showing
the frame at (p*n + i) * step seconds. The timeline used to fetch one /thumb
per tile (one ffmpeg spawn each). These tests decode the real sprite and read
each tile's colour back, so a tile showing the wrong second fails.
"""
from __future__ import annotations
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor.main import app

# One colour per second of the source, in order.
COLOURS = {
    "red": (255, 0, 0), "lime": (0, 255, 0), "blue": (0, 0, 255),
    "yellow": (255, 255, 0), "cyan": (0, 255, 255), "magenta": (255, 0, 255),
    "white": (255, 255, 255), "gray": (128, 128, 128),
}
ORDER = list(COLOURS)


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    from video_ai_editor import storage as _storage, main as _main
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    _main._STORES.clear()
    return TestClient(app)


def _blocks_video(p: Path, colours: list[str]) -> None:
    """A 320x180 30 fps clip: second k is solid `colours[k]`."""
    p.parent.mkdir(parents=True, exist_ok=True)
    args = ["ffmpeg", "-v", "error", "-y"]
    for c in colours:
        args += ["-f", "lavfi", "-i", f"color=c={c}:s=320x180:d=1:r=30"]
    ins = "".join(f"[{i}:v]" for i in range(len(colours)))
    args += ["-filter_complex", f"{ins}concat=n={len(colours)}:v=1:a=0[v]",
             "-map", "[v]", "-pix_fmt", "yuv420p", "-g", "15", str(p)]
    subprocess.run(args, check=True, capture_output=True)


def _decode(jpeg: bytes, tmp: Path) -> tuple[int, int, bytes]:
    f = tmp / "sprite.jpg"
    f.write_bytes(jpeg)
    wh = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height",
                         "-of", "csv=p=0", str(f)], check=True, capture_output=True,
                        text=True).stdout.strip().split(",")
    w, h = int(wh[0]), int(wh[1])
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(f), "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], check=True, capture_output=True).stdout
    return w, h, raw


def _tile_rgb(w: int, h: int, raw: bytes, n: int, i: int) -> tuple[int, int, int]:
    tw = w // n
    x, y = i * tw + tw // 2, h // 2
    o = (y * w + x) * 3
    return raw[o], raw[o + 1], raw[o + 2]


def _near(rgb, want, tol=40) -> bool:
    return all(abs(a - b) <= tol for a, b in zip(rgb, want))


def _get(client, sid, src, **params):
    return client.get(f"/api/sessions/{sid}/thumbstrip", params={"src": str(src), **params})


def test_sprite_tiles_show_their_own_seconds(client, tmp_path: Path):
    sid = client.post("/api/sessions").json()["id"]
    src = tmp_path / sid / "uploads" / "blocks.mp4"
    _blocks_video(src, ORDER)
    # Tiles sit half a second into each block, so each must show that block.
    r = _get(client, sid, src, step=0.5, page=0, n=16, h=54)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/jpeg"
    w, h, raw = _decode(r.content, tmp_path)
    assert h == 54
    assert w % 16 == 0 and w // 16 == 96, f"16 tiles of 96x54 expected, got {w}x{h}"
    for i in range(16):
        want = COLOURS[ORDER[i // 2]]
        got = _tile_rgb(w, h, raw, 16, i)
        assert _near(got, want), f"tile {i} (t={i * 0.5}s) is {got}, expected {ORDER[i // 2]}"


def test_sprite_pages_continue_the_grid_and_pad_past_the_end(client, tmp_path: Path):
    sid = client.post("/api/sessions").json()["id"]
    src = tmp_path / sid / "uploads" / "blocks.mp4"
    _blocks_video(src, ORDER)       # 8 s
    # Page 1 of 4-tile pages at 1 s: seconds 4..7.
    r = _get(client, sid, src, step=1, page=1, n=4, h=54)
    assert r.status_code == 200, r.text
    w, h, raw = _decode(r.content, tmp_path)
    for i in range(4):
        got = _tile_rgb(w, h, raw, 4, i)
        assert _near(got, COLOURS[ORDER[4 + i]]), f"page 1 tile {i}: {got}"
    # 16 slots at 1 s over an 8 s file: tiles 8..15 are black padding and the
    # sprite is still exactly 16 tiles wide.
    r = _get(client, sid, src, step=1, page=0, n=16, h=54)
    assert r.status_code == 200, r.text
    w, h, raw = _decode(r.content, tmp_path)
    assert w == 16 * 96, w
    assert _near(_tile_rgb(w, h, raw, 16, 7), COLOURS["gray"])
    for i in range(8, 16):
        assert _near(_tile_rgb(w, h, raw, 16, i), (0, 0, 0), tol=20), f"slot {i} not padded"


def test_sprite_tile_at_the_tail_still_decodes(client, tmp_path: Path):
    """A grid point after the last frame but before the end of the file (a
    13-frame 25 fps clip: last frame 0.48 s, end 0.52 s, tile at 0.5 s) — a
    seek there decodes nothing. The tile reads a frame just before the end."""
    sid = client.post("/api/sessions").json()["id"]
    src = tmp_path / sid / "uploads" / "short25.mp4"
    src.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "color=c=blue:s=320x180:r=25", "-frames:v", "13",
                    "-pix_fmt", "yuv420p", str(src)], check=True, capture_output=True)
    r = _get(client, sid, src, step=0.5, page=0, n=2, h=54)
    assert r.status_code == 200, r.text
    w, h, raw = _decode(r.content, tmp_path)
    assert _near(_tile_rgb(w, h, raw, 2, 1), COLOURS["blue"])


def test_sprite_pads_where_only_the_audio_continues(client, tmp_path: Path):
    """The container runs 1 s past the last frame (longer audio): those
    slots are padding, not a failed seek into a picture-less overhang."""
    sid = client.post("/api/sessions").json()["id"]
    pic = tmp_path / "pic.mp4"
    _blocks_video(pic, ["red", "blue"])
    src = tmp_path / sid / "uploads" / "overhang.mp4"
    src.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(pic), "-f", "lavfi",
                    "-i", "sine=f=440:d=3", "-c:v", "copy", "-c:a", "aac", str(src)],
                   check=True, capture_output=True)
    r = _get(client, sid, src, step=0.5, page=0, n=8, h=54)
    assert r.status_code == 200, r.text
    w, h, raw = _decode(r.content, tmp_path)
    assert _near(_tile_rgb(w, h, raw, 8, 3), COLOURS["blue"])
    for i in range(4, 8):
        assert _near(_tile_rgb(w, h, raw, 8, i), (0, 0, 0), tol=20), f"slot {i} not padded"


def test_sprite_is_cached_on_file_identity(client, tmp_path: Path):
    sid = client.post("/api/sessions").json()["id"]
    src = tmp_path / sid / "uploads" / "blocks.mp4"
    _blocks_video(src, ["red", "lime"])
    cache = tmp_path / sid / "cache" / "thumbs"
    r1 = _get(client, sid, src, step=1, page=0, n=2, h=54)
    assert r1.status_code == 200
    files = list(cache.glob("sp_*.jpg"))
    assert len(files) == 1
    before = files[0].stat().st_mtime_ns
    r2 = _get(client, sid, src, step=1, page=0, n=2, h=54)
    assert r2.content == r1.content
    assert files[0].stat().st_mtime_ns == before, "a cache hit must not rebuild"
    # The same path re-written (a re-normalised file) is a different identity.
    _blocks_video(src, ["blue", "yellow"])
    r3 = _get(client, sid, src, step=1, page=0, n=2, h=54)
    w, h, raw = _decode(r3.content, tmp_path)
    assert _near(_tile_rgb(w, h, raw, 2, 0), COLOURS["blue"]), "served a stale sprite"
    assert len(list(cache.glob("sp_*.jpg"))) == 2


@pytest.mark.parametrize("params,code", [
    ({"step": 0.3, "page": 0}, 422),      # off the client's grid
    ({"step": 1, "page": 99}, 422),       # starts past the end
    ({"step": 1, "page": -1}, 422),
    ({"step": 1, "page": 0, "n": 64}, 422),
])
def test_sprite_rejects_bad_parameters(client, tmp_path: Path, params, code):
    sid = client.post("/api/sessions").json()["id"]
    src = tmp_path / sid / "uploads" / "blocks.mp4"
    _blocks_video(src, ["red", "lime"])
    assert _get(client, sid, src, **params).status_code == code


def test_sprite_keeps_the_thumb_trust_boundary(client, tmp_path: Path):
    sid = client.post("/api/sessions").json()["id"]
    sibling = tmp_path / f"{sid}x" / "uploads" / "clip.mp4"
    _blocks_video(sibling, ["red"])
    assert _get(client, sid, sibling, step=1).status_code == 403
    assert client.get(f"/api/sessions/{sid}/thumbstrip",
                      params={"src": "uploads/clip.mp4", "step": 1}).status_code == 403
    assert _get(client, sid, tmp_path / sid / "uploads" / "nope.mp4", step=1).status_code == 404
