"""Integration test: WebCodecs frame-accurate scrub end-to-end.

This stands up a chromium page that mirrors what `FrameScrubber.tsx` does:
mp4box demux → VideoDecoder → render-to-canvas. Then for several target
times, we check that the centre pixel of the decoded frame matches the
expected colour from a known synthetic source (each second is a different
solid colour).

Skips cleanly if Playwright's chromium isn't installed (CI sandboxes often
lack it).
"""
from __future__ import annotations
import asyncio
import http.server
import socket
import socketserver
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest


COLOR_SECONDS = ["red", "green", "blue", "yellow"]
EXPECTED_RGB = {
    "red": (254, 0, 0),
    "green": (0, 128, 0),  # ffmpeg's "green" is dark green
    "blue": (0, 0, 254),
    "yellow": (254, 254, 0),
}


def _build_test_mp4(dst: Path) -> None:
    """4-second mp4, one solid colour per second, 30fps, keyframe interval 5."""
    inputs = []
    parts = []
    for i, c in enumerate(COLOR_SECONDS):
        inputs += ["-f", "lavfi", "-i", f"color=c={c}:s=320x180:d=1:r=30"]
        parts.append(f"[{i}:v]")
    fc = "".join(parts) + f"concat=n={len(COLOR_SECONDS)}:v=1:a=0[v]"
    subprocess.run(
        ["ffmpeg", "-y", *inputs,
         "-filter_complex", fc, "-map", "[v]",
         "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", "-g", "5",
         str(dst)],
        check=True, capture_output=True,
    )


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]; s.close(); return port


def _serve(dir_: Path, port: int) -> threading.Thread:
    handler = http.server.SimpleHTTPRequestHandler
    handler.extensions_map[".mp4"] = "video/mp4"
    handler.extensions_map[".js"] = "application/javascript"
    server = socketserver.TCPServer(("127.0.0.1", port),
                                    lambda *a: handler(*a, directory=str(dir_)))
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    setattr(th, "_server", server)
    return th


PAGE_HTML = """\
<!doctype html><html><body>
<canvas id="c" width="320" height="180"></canvas>
<script type="module">
import { createFile, DataStream } from '/mp4box.all.js';

window._frame_at = async (url, targetSec) => {
  const file = createFile();
  let resolveDone, doneP = new Promise(r => resolveDone = r);
  let decoder, samples = [], keyIdx = [], timescale = 1;
  let lastDecoded = null;

  file.onError = e => console.warn('mp4box error', e);
  file.onReady = info => {
    const vt = info.videoTracks[0];
    timescale = vt.timescale;
    const entry = file.getTrackById(vt.id).mdia.minf.stbl.stsd.entries[0];
    const box = entry.avcC || entry.hvcC || entry.av1C;
    const ds = new DataStream(undefined, 0, DataStream.BIG_ENDIAN);
    box.write(ds);
    const desc = new Uint8Array(ds.buffer, 8);
    decoder = new VideoDecoder({
      output: f => { lastDecoded = { ts: f.timestamp, w: f.codedWidth, h: f.codedHeight, frame: f }; },
      error: e => console.warn('decoder error', e),
    });
    decoder.configure({
      codec: vt.codec, codedWidth: vt.track_width,
      codedHeight: vt.track_height, description: desc,
    });
    file.setExtractionOptions(vt.id, null, { nbSamples: vt.nb_samples });
    file.start();
  };
  file.onSamples = (id, _u, ss) => {
    const off = samples.length;
    for (let i=0;i<ss.length;i++) {
      const s = ss[i];
      const cts = s.cts * 1e6 / timescale;
      const dts = s.dts * 1e6 / timescale;
      const dur = s.duration * 1e6 / timescale;
      samples.push({...s, cts, dts, duration: dur});
      if (s.is_sync) keyIdx.push(off+i);
    }
    if (samples.length >= 30) resolveDone();
  };

  const resp = await fetch(url);
  const reader = resp.body.getReader();
  let pos = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    const buf = value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength);
    buf.fileStart = pos;
    file.appendBuffer(buf);
    pos += value.byteLength;
  }
  file.flush();
  await doneP;

  const target = targetSec * 1e6;
  // bisect keyIdx
  let lo=0, hi=keyIdx.length-1, best=0;
  while (lo<=hi) { const m=(lo+hi)>>1; if (samples[keyIdx[m]].cts<=target) {best=m; lo=m+1;} else hi=m-1; }
  const startIdx = keyIdx[best];
  for (let i=startIdx; i<samples.length; i++) {
    const s = samples[i];
    const chunk = new EncodedVideoChunk({
      type: i===startIdx || s.is_sync ? 'key' : 'delta',
      timestamp: s.cts, duration: s.duration, data: s.data,
    });
    decoder.decode(chunk);
    if (s.cts > target) break;
  }
  await decoder.flush();
  if (!lastDecoded) return null;
  const c = document.getElementById('c');
  c.width = lastDecoded.w; c.height = lastDecoded.h;
  c.getContext('2d').drawImage(lastDecoded.frame, 0, 0);
  lastDecoded.frame.close();
  const px = c.getContext('2d').getImageData(160, 90, 1, 1).data;
  return [px[0], px[1], px[2], lastDecoded.ts];
};
</script></body></html>
"""


@pytest.mark.asyncio
async def test_webcodecs_scrub_lands_on_correct_frame(tmp_path: Path):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright python not installed")

    # Build the test mp4
    mp4 = tmp_path / "src.mp4"
    _build_test_mp4(mp4)

    # Stage mp4box.js next to the mp4 so the page can `import` it
    mp4box_src = (Path(__file__).resolve().parents[1] /
                  "frontend" / "node_modules" / "mp4box" / "dist" / "mp4box.all.js")
    if not mp4box_src.exists():
        pytest.skip("mp4box.js not installed under frontend/node_modules")
    (tmp_path / "mp4box.all.js").write_bytes(mp4box_src.read_bytes())
    (tmp_path / "index.html").write_text(PAGE_HTML)

    port = _free_port()
    server_th = _serve(tmp_path, port)
    try:
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=True)
            except Exception as e:
                pytest.skip(f"chromium not available: {e}")
            ctx = await browser.new_context()
            page = await ctx.new_page()
            page.on("console", lambda m: print("[browser]", m.text))
            await page.goto(f"http://127.0.0.1:{port}/index.html")

            # Each second of source is a different colour. Scrub to the middle
            # of each one and check the centre pixel.
            results = {}
            for i, name in enumerate(COLOR_SECONDS):
                t = i + 0.5  # mid-second
                rgba_ts = await page.evaluate(
                    "([url, t]) => window._frame_at(url, t)",
                    [f"http://127.0.0.1:{port}/src.mp4", t],
                )
                assert rgba_ts is not None, f"scrubber returned null at t={t}"
                r, g, b, ts = rgba_ts
                results[name] = (r, g, b, ts)
                exp = EXPECTED_RGB[name]
                # Allow ±20 per channel (codec rounding + colour space).
                for ch, ex in zip((r, g, b), exp):
                    assert abs(ch - ex) < 25, (
                        f"at t={t} ({name}): expected ~{exp}, got ({r},{g},{b}); ts_us={ts}")
            await browser.close()
        for name, v in results.items():
            print(f"  {name}@mid: rgb={v[:3]} ts_us={v[3]}")
    finally:
        getattr(server_th, "_server").shutdown()
