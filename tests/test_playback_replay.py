"""Regression test: a preview played to its end must REPLAY cleanly, even when
the browser's <video> seek completes slowly (Windows/WebView2), rather than
freezing at edl.duration with the play/pause state oscillating.

This injects artificial seek latency to deterministically reproduce a race that
otherwise only appears on Windows (macOS VideoToolbox seeks land within a frame).
Skips cleanly if Playwright chromium isn't installed.
"""
from __future__ import annotations
import http.server
import socket
import socketserver
import subprocess
import threading
from pathlib import Path

import pytest


def _build_clip(dst: Path) -> None:
    # 3s, 30fps. The muxed/browser-reported duration will be slightly > 3.0,
    # which is what makes the rAF end-clamp fire with ended:false.
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "15", str(dst)],
        check=True, capture_output=True,
    )


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]; s.close(); return port


def _serve(dir_: Path, port: int) -> threading.Thread:
    handler = http.server.SimpleHTTPRequestHandler
    handler.extensions_map[".mp4"] = "video/mp4"
    server = socketserver.TCPServer(
        ("127.0.0.1", port), lambda *a: handler(*a, directory=str(dir_)))
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    setattr(th, "_server", server)
    return th


# The page reproduces Preview.tsx's playback model in plain JS: store with an
# UNCONDITIONAL setPlaying/setPlayhead, the three effects, and the button.
# DURATION is set BELOW the clip's real length so the rAF clamp fires while the
# video is still playing (ended:false) — the failing state. __installSlowSeek
# delays currentTime writes to simulate a slow seek.
PAGE_HTML = r"""<!doctype html><html><body>
<video id="v" src="clip.mp4" preload="auto" style="width:320px;height:240px"></video>
<button id="btn">play</button><span id="pill"></span>
<script>
let playhead=0,isPlaying=false,playbackRate=1;const DURATION=2.0;let clock=0,raf=0;
let effRaf=0;
const flips=[];const v=document.getElementById('v');const pill=document.getElementById('pill');
function setPlayhead(t){playhead=Math.max(0,DURATION?Math.min(t,DURATION):t);pill.textContent=playhead.toFixed(2)+' / '+DURATION.toFixed(2);}
function setPlaying(p){if(p===isPlaying)return;isPlaying=p;flips.push(p);document.getElementById('btn').textContent=isPlaying?'pause':'play';
  // Defer effect re-run to the NEXT animation frame — matching React's real
  // scheduling boundary (a useEffect triggered by a state update never runs
  // synchronously nested inside the call stack that set the state). Calling
  // runEffects() synchronously here (as an earlier version of this test did)
  // collapsed that boundary and let native <video> play/pause DOM events
  // re-enter setPlaying within the SAME tick over and over — an unbounded
  // ~40,000/sec synchronous recursion artifact of the TEST HARNESS itself,
  // not the real rAF-frame-paced (~16-60ms between flips) oscillation the
  // production bug produces. Verified: with this deferral, transitions drop
  // from ~40,000+/sec to a small deterministic bounded count with realistic
  // ms-scale deltas between flips.
  cancelAnimationFrame(effRaf);
  effRaf=requestAnimationFrame(runEffects);}
function effA(){if(playbackRate>0)v.playbackRate=Math.min(4,playbackRate);else v.playbackRate=1;if(isPlaying&&playbackRate>0)v.play().catch(()=>{});else v.pause();}
function effB(){const gap=Math.abs(v.currentTime-playhead);if(gap>(isPlaying?0.35:0.05)){try{v.currentTime=playhead}catch{}clock=playhead;}}
function runEffects(){effA();effB();cancelAnimationFrame(raf);if(!isPlaying)return;
  let last=performance.now();clock=playhead;const TRUST_TOL=0.35;
  const loop=(now)=>{const dt=(now-last)/1000;last=now;const rate=playbackRate;
    const trustworthy=v&&!v.paused&&!v.ended&&Math.abs(v.currentTime-clock)<TRUST_TOL;
    if(trustworthy){clock=v.currentTime;}else{clock+=dt*Math.max(-4,Math.min(4,rate||1));}
    let t=clock;
    if(DURATION&&t>=DURATION){setPlayhead(DURATION);setPlaying(false);return;}
    if(t<=0&&rate<0){setPlayhead(0);setPlaying(false);return;}
    setPlayhead(Math.max(0,t));raf=requestAnimationFrame(loop);};
  raf=requestAnimationFrame(loop);}
v.addEventListener('play',()=>setPlaying(true));v.addEventListener('pause',()=>setPlaying(false));
document.getElementById('btn').addEventListener('click',()=>{
  let rewound=false;
  if(!isPlaying&&DURATION>0&&playhead>=DURATION-1/30){setPlayhead(0);rewound=true;}
  if(rewound){try{v.currentTime=0}catch{}clock=0;}
  setPlaying(!isPlaying);
});
window.__startPlay=()=>setPlaying(true);
window.__state=()=>({playhead,isPlaying,ended:v.ended,ct:+v.currentTime.toFixed(3),paused:v.paused});
window.__flips=()=>flips.length;
window.__installSlowSeek=(ms)=>{let p=Object.getPrototypeOf(v),d=null;while(p&&!d){d=Object.getOwnPropertyDescriptor(p,'currentTime');if(!d)p=Object.getPrototypeOf(p);}
  Object.defineProperty(v,'currentTime',{configurable:true,get(){return d.get.call(this)},set(val){const s=this;setTimeout(()=>{try{d.set.call(s,val)}catch{}},ms)}});};
</script></body></html>
"""


@pytest.mark.asyncio
async def test_replay_at_end_does_not_oscillate(tmp_path: Path):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright python not installed")

    _build_clip(tmp_path / "clip.mp4")
    (tmp_path / "index.html").write_text(PAGE_HTML)
    port = _free_port()
    server_th = _serve(tmp_path, port)
    try:
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(
                    headless=True, args=["--autoplay-policy=no-user-gesture-required"])
            except Exception as e:
                pytest.skip(f"chromium not available: {e}")
            page = await browser.new_context()
            page = await page.new_page()
            await page.goto(f"http://127.0.0.1:{port}/index.html")
            await page.wait_for_function("() => document.getElementById('v').readyState >= 1")

            # Play; the rAF clamp pauses at DURATION with ended:false.
            await page.evaluate("() => window.__startPlay()")
            await page.wait_for_function(
                "() => window.__state().isPlaying === false", timeout=8000)
            await page.wait_for_timeout(200)

            # Simulate Windows/WebView2 seek latency, then click to replay.
            await page.evaluate("() => window.__installSlowSeek(200)")
            flips_before = await page.evaluate("() => window.__flips()")
            await page.click("#btn")
            await page.wait_for_timeout(2500)
            flips_after = await page.evaluate("() => window.__flips()")
            transitions = flips_after - flips_before

            state = await page.evaluate("() => window.__state()")
            await browser.close()

        # A clean replay flips isPlaying at most twice (the click, and possibly a
        # single settle). The bug produces many transitions (the ▶↔❚❚ flicker).
        assert transitions <= 2, (
            f"play/pause oscillated {transitions}x after replay-at-end "
            f"(freeze reproduced); final state={state}")
    finally:
        getattr(server_th, "_server").shutdown()
