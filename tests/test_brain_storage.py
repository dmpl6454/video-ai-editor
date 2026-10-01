"""`brain/store.py` — the analysis store under `WORKDIR/analysis/<src_key>/`
and the per-session `<session>/brain/` files (EB1 lane C).

Pins: identity key (realpath + size + mtime_ns) vs content key (size + head
+ tail bytes) — a copy at a new path shares the content key and not the
identity key, and a re-touched file changes the identity key only; writes
are atomic (temp beside the target, `os.replace`, no temp left behind) and
refuse payloads over the 32 MB layer / 64 MB graph caps without touching an
existing file; `refs.json`; the immutable EDP; the current-graph pointer.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import brain_contract_fixtures as BF  # noqa: E402

from video_ai_editor.brain import schema as S  # noqa: E402
from video_ai_editor.brain import store as BS  # noqa: E402


@pytest.fixture
def media(tmp_path: Path) -> Path:
    p = tmp_path / "clip.bin"
    p.write_bytes(os.urandom(3 * 1024 * 1024))
    return p


# ---------------------------------------------------------------- keys

def test_identity_and_content_keys(tmp_path: Path, media: Path):
    k1, c1 = BS.src_key(media), BS.content_key(media)
    assert len(k1) == 24 and all(ch in "0123456789abcdef" for ch in k1)
    assert len(c1) == 64
    copy = tmp_path / "elsewhere" / "clip_copy.bin"
    copy.parent.mkdir()
    copy.write_bytes(media.read_bytes())
    assert BS.content_key(copy) == c1                # same bytes → same content key
    assert BS.src_key(copy) != k1                    # another realpath → another identity
    st = media.stat()
    os.utime(media, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert BS.src_key(media) != k1 and BS.content_key(media) == c1   # a touch changes the identity, not the content
    # the content key reads only the head and tail: a middle-byte change is invisible to it, by design
    with open(copy, "r+b") as fh:
        fh.seek(1_500_000)
        fh.write(b"\x00" * 16)
    assert BS.content_key(copy) == c1
    with pytest.raises(OSError):
        BS.src_key(tmp_path / "missing.bin")
    assert BS.content_key(tmp_path / "missing.bin") == "missing"


def test_source_json_and_find_by_content(tmp_path: Path, media: Path):
    workdir = tmp_path / "work"
    key, ck = BS.src_key(media), BS.content_key(media)
    BS.write_source(key, {"src_key": key, "content_key": ck, "leaf": media.name, "duration": 1.0, "fps": None,
                          "has_audio": True, "has_video": False, "proxy_key": None}, workdir=workdir)
    assert BS.analysis_dir(key, workdir=workdir) == workdir / "analysis" / key
    assert BS.find_by_content(ck, workdir=workdir) == key
    assert BS.find_by_content("f" * 64, workdir=workdir) is None
    BS.add_ref(key, "s_one", workdir=workdir)
    BS.add_ref(key, "s_one", workdir=workdir)
    BS.add_ref(key, "s_two", workdir=workdir)
    assert BS.read_refs(key, workdir=workdir) == ["s_one", "s_two"]


# ---------------------------------------------------------------- atomic writes + caps

def test_atomic_write_and_size_caps(tmp_path: Path, monkeypatch):
    workdir = tmp_path / "work"
    key = "b" * 24
    p = BS.write_layer(key, "audio", "hand-v1", BF.audio_layer(), workdir=workdir)
    assert p == workdir / "analysis" / key / "audio" / "hand-v1.json" and p.exists()
    assert not [x for x in p.parent.iterdir() if x.name != p.name]        # no temp file left beside it
    before = p.read_bytes()
    # a write that fails mid-way leaves the old file byte-identical and no temp
    real_replace = os.replace

    def boom(src, dst):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(BS.os, "replace", boom)
    with pytest.raises(OSError):
        BS.write_layer(key, "audio", "hand-v1", {**BF.audio_layer(), "loudness_i": -12.0}, workdir=workdir)
    monkeypatch.setattr(BS.os, "replace", real_replace)
    assert p.read_bytes() == before
    assert [x.name for x in p.parent.iterdir()] == [p.name]
    # the caps: a layer over 32 MB and a graph over 64 MB are refused, file untouched
    big = {"hz": 2, "faces": [[0.1, 0.2, 0.3, 0.4]] * 3_000_000}          # ≈ 54 MB of a schema-less layer
    with pytest.raises(BS.TooLarge, match="32 MB"):
        BS.write_layer(key, "visual", "hand-v1", big, workdir=workdir)
    assert p.read_bytes() == before and not (workdir / "analysis" / key / "visual").exists()
    monkeypatch.setattr(S, "MAX_GRAPH_BYTES", 500)
    monkeypatch.setattr(BS, "MAX_GRAPH_BYTES", 500)
    session = tmp_path / "s_x"
    with pytest.raises(BS.TooLarge, match="graph"):
        BS.write_graph(session, S.Graph.model_validate(BF.graph_header("/tmp/x.mp4")))
    assert not (session / "brain" / "graph").exists() or not list((session / "brain" / "graph").glob("*.json"))
    # reading an oversized file on disk is refused too (never partially trusted)
    p.write_bytes(b"[" + b"0," * 1_100 + b"0]")
    monkeypatch.setattr(BS, "MAX_LAYER_BYTES", 1_000)
    with pytest.raises(BS.TooLarge):
        BS.read_layer(key, "audio", "hand-v1", workdir=workdir)


def test_layer_status_and_invalid_layer_is_missing(tmp_path: Path):
    workdir = tmp_path / "work"
    key = "c" * 24
    assert BS.layer_status(key, "speech", "hand-v1", workdir=workdir) == "missing"
    BS.write_layer(key, "speech", "hand-v1", BF.speech_layer(), workdir=workdir)
    assert BS.layer_status(key, "speech", "hand-v1", workdir=workdir) == "ok"
    p = BS.layer_path(key, "speech", "hand-v1", workdir=workdir)
    p.write_text(json.dumps({**BF.speech_layer(), "bogus": 1}), encoding="utf-8")
    assert BS.layer_status(key, "speech", "hand-v1", workdir=workdir).startswith("failed:")
    assert BS.load_layer(key, "speech", "hand-v1", workdir=workdir) is None


# ---------------------------------------------------------------- the session's brain/

def test_edp_is_immutable_and_graph_pointer(tmp_path: Path):
    session = tmp_path / "s_sess"
    edp = S.EDP.model_validate(BF.edp("/tmp/x.mp4"))
    p = BS.write_edp(session, edp)
    assert p == session / "brain" / "decisions" / f"{BF.DID}.json"
    assert BS.read_edp(session, BF.DID).id == BF.DID
    assert BS.write_edp(session, edp) == p                                  # the same bytes again: fine
    with pytest.raises(FileExistsError):
        BS.write_edp(session, S.EDP.model_validate({**BF.edp("/tmp/x.mp4"), "seed": 3}))
    assert BS.read_edp(session, "d_00000000") is None
    with pytest.raises(ValueError):
        BS.read_edp(session, "../etc/passwd")
    assert BS.current_graph_id(session) is None
    BS.set_current_graph(session, BF.GID)
    assert BS.current_graph_id(session) == BF.GID
    g = BS.write_graph(session, S.Graph.model_validate(BF.graph_header("/tmp/x.mp4")))
    assert g == session / "brain" / "graph" / f"{BF.GID}.json"
    assert BS.read_graph(session, BF.GID).id == BF.GID
    assert BS.list_edps(session) == [BF.DID]


# ---------------------------------------------------------------- FX-A: temp names (SC-07)

import threading  # noqa: E402


def test_every_write_uses_its_own_temp_name(tmp_path, monkeypatch):
    seen: list[str] = []
    real = os.replace

    def spy(src, dst):
        seen.append(Path(src).name)
        return real(src, dst)
    monkeypatch.setattr(BS.os, "replace", spy)
    target = tmp_path / "x" / "layer.json"
    BS.write_json(target, {"a": 1})
    BS.write_json(target, {"a": 2})
    ts = [threading.Thread(target=BS.write_json, args=(target, {"a": i})) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(seen) == 8 and len(set(seen)) == 8, seen
    assert not [p for p in target.parent.iterdir() if p.name.endswith(".tmp")]


def test_concurrent_writers_of_one_file_never_fail_or_publish_a_torn_file(tmp_path):
    target = tmp_path / "layer.json"
    errors: list[BaseException] = []
    payloads = [{"n": i, "pad": "x" * 200_000} for i in range(4)]

    def writer(k: int) -> None:
        try:
            for i in range(15):
                BS.write_json(target, payloads[(k + i) % 4])
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=writer, args=(k,)) for k in range(6)]
    [t.start() for t in ts]
    while any(t.is_alive() for t in ts):
        got = BS.read_json(target)              # a reader mid-storm: whole file or none
        assert got is None or got["pad"] == "x" * 200_000
    assert not errors, errors
    assert BS.read_json(target)["pad"] == "x" * 200_000
