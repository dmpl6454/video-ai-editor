"""The chunked render hands ffmpeg an over-long filter graph in a FILE.

Windows' CreateProcess takes 32,767 characters of command line; `subprocess`
raises WinError 206 before ffmpeg starts (CI run 36584432248, the single-pass
export of a heavily cut timeline). The compositor learned to write the graph
to a file (`compositor._graph_in_file_if_long`); the per-chunk render in
render/chunks.py, the DEFAULT export and preview path, still put its graph on
the command line. These tests pin that it goes through the same helper, that
the frames are the inline render's frames, that nothing is left behind, and
that no other graph site under render/ or ingest/ appears unrouted.
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from video_ai_editor import platformutil as _pu
from video_ai_editor.edl.schema import Clip
from video_ai_editor.render import cancel as _cancel
from video_ai_editor.render import chunks, compositor
from ffmpeg_caps import binary_reads

W, H, FPS = 320, 180, 30
#: What CreateProcess accepts (characters of command line, with the NUL).
WINDOWS_CMDLINE_MAX = 32_767
GRAPH_OPTS = ("-/filter_complex", "-filter_complex_script")


@pytest.fixture(scope="module")
def src(tmp_path_factory) -> Path:
    """2 s of moving bars with a tone: every frame differs from its neighbour."""
    p = tmp_path_factory.mktemp("chunk-graph-src") / "bars.mp4"
    subprocess.run(
        [_pu.FFMPEG, "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:r={FPS}:d=2",
         "-f", "lavfi", "-i", "sine=f=440:r=48000:d=2",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(p)],
        check=True, capture_output=True)
    return p


@pytest.fixture(scope="module")
def enc() -> list[str]:
    """A software encoder, so two renders of the same frames are the same
    bytes (a hardware encoder is not bit-exact run to run)."""
    if not compositor._usable_encoder("libx264"):
        pytest.skip("this ffmpeg has no usable libx264 (compositor._usable_encoder)")
    return ["-c:v", "libx264", "-preset", "ultrafast", "-threads", "1",
            "-pix_fmt", "yuv420p"]


def _clip(src: Path) -> Clip:
    return Clip(src=str(src), in_=0.5, out=1.5, start=0, id="c1")


def _chunk(clip: Clip, dst: Path, enc: list[str], *, video_chain=None, streams="av") -> None:
    chunks.render_clip_to_chunk(
        clip, dst=dst, canvas_w=W, canvas_h=H, fps=FPS, encoder_args=enc,
        build_video_chain=video_chain or compositor._build_clip_video_chain,
        build_audio_chain=compositor._build_clip_audio_chain,
        cache_dir=dst.parent, streams=streams)


def _md5s(p: Path) -> tuple[list[str], list[str]]:
    """(one hash per decoded video frame, one per decoded audio packet)."""
    out = []
    for sel in ("0:v:0", "0:a:0"):
        r = subprocess.run([_pu.FFMPEG, "-v", "error", "-i", str(p), "-map", sel,
                            "-f", "framemd5", "-"], check=True, capture_output=True, text=True)
        out.append([ln for ln in r.stdout.splitlines() if ln and not ln.startswith("#")])
    return out[0], out[1]


def _spy(monkeypatch, run=None) -> list[dict]:
    """Record every argv the chunk render hands to `cancel.run`, and what the
    graph file held at that moment."""
    seen: list[dict] = []
    real = _cancel.run

    def spy(args, **kw):
        rec = {"argv": list(args), "graph_file": None, "graph_text": None}
        for opt in GRAPH_OPTS:
            if opt in args:
                rec["graph_file"] = Path(args[args.index(opt) + 1])
                rec["graph_text"] = rec["graph_file"].read_text(encoding="utf-8")
        seen.append(rec)
        return (run or real)(args, **kw)

    monkeypatch.setattr(_cancel, "run", spy)
    return seen


def _leftovers(d: Path) -> list[str]:
    return sorted(p.name for p in d.rglob("*")
                  if p.name.endswith(".filtergraph") or ".part" in p.name)


def _padded_video_chain(pad_chars: int):
    """The compositor's chain behind enough idle side chains (a 16x16 source
    into a sink, one frame each; the clip's frames never pass through them) to
    reach the length a long curve or effect stack reaches. Side chains rather
    than one deep chain: ffmpeg 8.1.1 dies (SIGBUS, rc -10) configuring 8,000
    filters in a row, which no edit produces."""
    side = "nullsrc=s=16x16:d=0.04:r=30,nullsink;"

    def build(c, *, input_label, label_out, canvas_w, canvas_h, fps=None):
        real = compositor._build_clip_video_chain(
            c, input_label=input_label, label_out=label_out,
            canvas_w=canvas_w, canvas_h=canvas_h, fps=fps)
        return side * (pad_chars // len(side) + 1) + real
    return build


# ---- the file path renders the inline render's frames -----------------------

def test_a_chunk_graph_over_the_limit_is_read_from_a_file_and_renders_the_same_frames(
        src, enc, tmp_path, monkeypatch):
    clip = _clip(src)
    seen = _spy(monkeypatch)
    _chunk(clip, tmp_path / "inline" / "chunk.mp4", enc)
    (inline,) = seen
    assert inline["graph_file"] is None                  # under the real limit: unchanged
    fc = inline["argv"][inline["argv"].index("-filter_complex") + 1]
    want_v, want_a = _md5s(tmp_path / "inline" / "chunk.mp4")
    assert len(want_v) == compositor.clip_frames(clip, FPS) == 30 and want_a

    monkeypatch.setattr(compositor, "_ARGV_GRAPH_LIMIT", 40)
    assert len(fc) > 40
    rendered = 0
    for major in (8, 6):                                 # `-/filter_complex` and the pre-7 option
        monkeypatch.setattr(compositor, "_ffmpeg_major", lambda m=major: m)
        opt = "-/filter_complex" if major >= 7 else "-filter_complex_script"
        assert compositor._graph_file_option() == opt
        if not binary_reads(opt):
            # This binary does not have the option a version-`major` ffmpeg is
            # given (ffmpeg_caps says which): the argv and the graph file are
            # checked, nothing is rendered.
            anchor = tmp_path / f"argv{major}" / "chunk.part"
            anchor.parent.mkdir(parents=True)
            with compositor._graph_in_file_if_long(list(inline["argv"]), anchor) as argv:
                assert opt in argv and "-filter_complex" not in argv and fc not in argv
                assert Path(argv[argv.index(opt) + 1]).read_text(encoding="utf-8") == fc
            assert _leftovers(anchor.parent) == []
            continue
        rendered += 1
        seen.clear()
        dst = tmp_path / f"file{major}" / "chunk.mp4"
        _chunk(clip, dst, enc)
        (rec,) = seen
        argv = rec["argv"]
        assert opt in argv and "-filter_complex" not in argv
        assert fc not in argv
        assert rec["graph_text"] == fc                   # the graph, character for character
        # everything else in argv is the inline argv's (own staged output aside)
        i = argv.index(opt)
        j = inline["argv"].index("-filter_complex")
        assert argv[:i] == inline["argv"][:j]
        assert argv[i + 2:-1] == inline["argv"][j + 2:-1]
        assert rec["graph_file"].parent == Path(argv[-1]).parent == dst.parent
        assert not rec["graph_file"].exists(), "the graph file is removed once ffmpeg is done"
        assert _leftovers(dst.parent) == []
        got_v, got_a = _md5s(dst)
        assert got_v == want_v, f"{sum(a != b for a, b in zip(got_v, want_v))} frames differ"
        assert got_a == want_a
        assert dst.read_bytes() == (tmp_path / "inline" / "chunk.mp4").read_bytes()
    assert rendered >= 1, "every ffmpeg reads at least one of the two options"


@pytest.mark.parametrize("streams", ["v", "a"])
def test_the_single_stream_chunks_of_a_segmented_render_take_the_file_path_too(
        src, enc, tmp_path, monkeypatch, streams):
    clip = _clip(src)
    _chunk(clip, tmp_path / "inline" / "c.mp4", enc, streams=streams)
    monkeypatch.setattr(compositor, "_ARGV_GRAPH_LIMIT", 40)
    seen = _spy(monkeypatch)
    _chunk(clip, tmp_path / "file" / "c.mp4", enc, streams=streams)
    (rec,) = seen
    assert rec["graph_file"] is not None and "-filter_complex" not in rec["argv"]
    assert (tmp_path / "file" / "c.mp4").read_bytes() == (tmp_path / "inline" / "c.mp4").read_bytes()
    assert _leftovers(tmp_path / "file") == []


def test_the_chunk_key_and_path_do_not_depend_on_how_the_graph_was_passed(
        src, enc, tmp_path, monkeypatch):
    clip = _clip(src)
    kw = dict(cache_dir=tmp_path / "chunks", canvas_w=W, canvas_h=H, fps=FPS, encoder_args=enc,
              build_video_chain=compositor._build_clip_video_chain,
              build_audio_chain=compositor._build_clip_audio_chain)
    key = chunks.fingerprint_clip(clip, canvas_w=W, canvas_h=H, fps=FPS, encoder_args=enc)
    monkeypatch.setattr(compositor, "_ARGV_GRAPH_LIMIT", 40)
    seen = _spy(monkeypatch)
    (built,) = chunks.get_or_build_chunks([clip], **kw)
    assert len(seen) == 1 and seen[0]["graph_file"] is not None
    assert built.name == f"chunk_{key}.mp4"
    assert key == chunks.fingerprint_clip(clip, canvas_w=W, canvas_h=H, fps=FPS, encoder_args=enc)
    monkeypatch.setattr(compositor, "_ARGV_GRAPH_LIMIT", 100_000)
    seen.clear()
    assert chunks.get_or_build_chunks([clip], **kw) == [built]    # a hit: the same chunk serves both
    assert seen == []
    assert _leftovers(tmp_path / "chunks") == []


# ---- nothing is left behind -------------------------------------------------

def test_the_graph_file_and_the_staged_chunk_are_removed_when_ffmpeg_fails(
        src, enc, tmp_path, monkeypatch):
    monkeypatch.setattr(compositor, "_ARGV_GRAPH_LIMIT", 40)
    real = _cancel.run

    def broken(args, **kw):                              # a bad codec, after argv is built
        return real([*args[:-1], "-c:v", "no_such_encoder", args[-1]], **kw)

    seen = _spy(monkeypatch, run=broken)
    with pytest.raises(RuntimeError, match="chunk render failed"):
        _chunk(_clip(src), tmp_path / "c" / "chunk.mp4", enc)
    assert len(seen) == 1 and seen[0]["graph_file"] is not None   # ffmpeg did read a graph file
    assert not seen[0]["graph_file"].exists()
    assert _leftovers(tmp_path) == [] and not (tmp_path / "c" / "chunk.mp4").exists()


def test_the_graph_file_is_removed_when_the_chunk_holds_the_wrong_frame_count(
        src, enc, tmp_path, monkeypatch):
    """The failure AFTER a successful ffmpeg run (chunks.py's frame check)."""
    monkeypatch.setattr(compositor, "_ARGV_GRAPH_LIMIT", 40)
    monkeypatch.setattr(chunks, "_video_packets", lambda p: 29)
    seen = _spy(monkeypatch)
    with pytest.raises(RuntimeError, match="29 video frames, the clip plans 30"):
        _chunk(_clip(src), tmp_path / "c" / "chunk.mp4", enc)
    assert seen[0]["graph_file"] is not None
    assert _leftovers(tmp_path) == [] and not (tmp_path / "c" / "chunk.mp4").exists()


@pytest.mark.parametrize("exc", [KeyboardInterrupt, _cancel.RenderCancelled, OSError])
def test_the_graph_file_is_removed_when_the_run_raises(src, enc, tmp_path, monkeypatch, exc):
    """Cancel (a superseded preview), Ctrl-C, and a spawn that fails."""
    monkeypatch.setattr(compositor, "_ARGV_GRAPH_LIMIT", 40)

    def boom(args, **kw):
        raise exc("stopped")

    seen = _spy(monkeypatch, run=boom)
    with pytest.raises(exc):
        _chunk(_clip(src), tmp_path / "c" / "chunk.mp4", enc)
    assert len(seen) == 1 and seen[0]["graph_text"]      # the file existed while ffmpeg would run
    assert not seen[0]["graph_file"].exists()
    assert _leftovers(tmp_path) == []


def test_parallel_chunks_each_use_their_own_graph_file(src, enc, tmp_path, monkeypatch):
    """The file is named after the staged chunk (pid + thread + key), so the
    pool's renders never read each other's graph."""
    monkeypatch.setattr(compositor, "_ARGV_GRAPH_LIMIT", 40)
    monkeypatch.setenv("VAI_CHUNK_WORKERS", "4")
    clips = [Clip(src=str(src), in_=0.1 * i, out=0.1 * i + 0.5, start=0.5 * i, id=f"c{i}")
             for i in range(4)]
    seen = _spy(monkeypatch)
    paths = chunks.get_or_build_chunks(
        clips, cache_dir=tmp_path / "chunks", canvas_w=W, canvas_h=H, fps=FPS, encoder_args=enc,
        build_video_chain=compositor._build_clip_video_chain,
        build_audio_chain=compositor._build_clip_audio_chain)
    assert len({r["graph_file"] for r in seen}) == 4 and None not in {r["graph_file"] for r in seen}
    for c, p in zip(clips, paths):
        assert len(_md5s(p)[0]) == compositor.clip_frames(c, FPS) == 15
    assert _leftovers(tmp_path / "chunks") == []


# ---- Windows, rehearsed -----------------------------------------------------

def test_on_windows_the_chunk_command_line_stays_under_what_createprocess_takes(
        src, enc, tmp_path, monkeypatch):
    """A 40,000-character chunk graph at the SHIPPED Windows limit: the command
    line as CreateProcess would receive it (`subprocess.list2cmdline`, what
    `subprocess` itself builds on Windows) is under 32,767. No process is
    started under the Windows flag: the spy answers for ffmpeg."""
    monkeypatch.setattr(compositor._pu, "IS_WINDOWS", True)
    monkeypatch.setattr(compositor, "_ARGV_GRAPH_LIMIT", 30_000)  # the value compositor ships on Windows
    monkeypatch.setattr(compositor, "_ffmpeg_major", lambda: 9)

    def no_ffmpeg(args, **kw):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="rehearsal: not run")

    seen = _spy(monkeypatch, run=no_ffmpeg)
    with pytest.raises(RuntimeError, match="chunk render failed"):
        _chunk(_clip(src), tmp_path / "c" / "chunk.mp4", enc,
               video_chain=_padded_video_chain(40_000))
    (rec,) = seen
    assert len(rec["graph_text"]) > 40_000 > WINDOWS_CMDLINE_MAX
    assert "-/filter_complex" in rec["argv"] and "-filter_complex" not in rec["argv"]
    assert len(subprocess.list2cmdline(rec["argv"])) < 1_000 < WINDOWS_CMDLINE_MAX
    assert _leftovers(tmp_path) == []


def test_a_forty_thousand_character_chunk_graph_renders_the_clips_frames(
        src, enc, tmp_path, monkeypatch):
    """The same long graph, run for real through the file (limit as on
    Windows, measured the POSIX way here: the longest argument)."""
    clip = _clip(src)
    _chunk(clip, tmp_path / "short" / "chunk.mp4", enc)
    monkeypatch.setattr(compositor, "_ARGV_GRAPH_LIMIT", 30_000)
    seen = _spy(monkeypatch)
    _chunk(clip, tmp_path / "long" / "chunk.mp4", enc, video_chain=_padded_video_chain(40_000))
    (rec,) = seen
    assert len(rec["graph_text"]) > 40_000 and max(len(a) for a in rec["argv"]) < 1_000
    assert _md5s(tmp_path / "long" / "chunk.mp4") == _md5s(tmp_path / "short" / "chunk.mp4")
    assert _leftovers(tmp_path) == []


# ---- the guard --------------------------------------------------------------

#: Graph sites under render/ and ingest/ that do NOT go through the helper,
#: with the most their command can hold. Both are bounded by construction, not
#: by the timeline. A new entry here needs the same argument; a graph that
#: grows with the edit goes through `_graph_in_file_if_long`.
KNOWN_BOUNDED_INLINE = {
    # one input, two fixed chains (`[v]` and `[a]`): under 700 characters
    "render/reverse.py": 1,
    # one `-ss -i <src>` and a ~75-character chain per tile, SPRITE_MAX = 32
    # tiles: 32 x (260-character path + ~110) is under 12,000 characters
    "render/thumbs.py": 1,
}


def _graph_sites(path: Path) -> tuple[int, int]:
    """(inline `-filter_complex` literals outside the helper itself, `with
    _graph_in_file_if_long(...)` blocks) in one source file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    helper = {id(n) for f in ast.walk(tree)
              if isinstance(f, ast.FunctionDef) and f.name == "_graph_in_file_if_long"
              for n in ast.walk(f)}
    inline = sum(1 for n in ast.walk(tree)
                 if isinstance(n, ast.Constant) and n.value == "-filter_complex"
                 and id(n) not in helper)
    routed = sum(1 for n in ast.walk(tree) if isinstance(n, ast.With)
                 for it in n.items
                 if isinstance(it.context_expr, ast.Call)
                 and getattr(it.context_expr.func, "id", "") == "_graph_in_file_if_long")
    return inline, routed


def test_every_graph_under_render_and_ingest_goes_through_the_file_fallback():
    root = Path(chunks.__file__).resolve().parents[1]
    found = {}
    for sub in ("render", "ingest"):
        for p in sorted((root / sub).rglob("*.py")):
            inline, routed = _graph_sites(p)
            if inline or routed:
                found[f"{sub}/{p.relative_to(root / sub).as_posix()}"] = (inline, routed)
    assert found["render/chunks.py"] == (1, 1)
    assert found["render/compositor.py"][0] >= 5
    unrouted = {k: i - r for k, (i, r) in found.items() if i != r}
    assert unrouted == KNOWN_BOUNDED_INLINE, (
        "a `-filter_complex <graph>` on the command line is WinError 206 waiting for a "
        "long timeline: run it inside `with _graph_in_file_if_long(args, tmp) as run_args`")


def test_the_bounds_the_guard_relies_on_still_hold():
    from video_ai_editor.render import thumbs
    assert thumbs.SPRITE_MAX <= 32
