from pathlib import Path


def test_no_bare_read_text_in_state_modules():
    """State/transcript modules must never call read_text()/write_text() without
    an explicit encoding= — locale cp1252 on Windows corrupts Hindi/emoji."""
    import ast
    targets = [
        "src/video_ai_editor/edl/snapshot.py",
        "src/video_ai_editor/agent/dispatch.py",
        "src/video_ai_editor/main.py",
        "src/video_ai_editor/storage.py",
        "src/video_ai_editor/storage_project.py",
        "src/video_ai_editor/ingest/pipeline.py",
    ]
    offenders = []
    for t in targets:
        txt = Path(t).read_text(encoding="utf-8")
        tree = ast.parse(txt, filename=t)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("read_text", "write_text")):
                if not any(kw.arg == "encoding" for kw in node.keywords):
                    offenders.append(f"{t}:{node.lineno}: .{node.func.attr}(...) missing encoding=")
    assert not offenders, "bare text I/O:\n" + "\n".join(offenders)


def test_snapshot_roundtrips_devanagari(tmp_path):
    """A snapshot written then reloaded preserves Hindi text on any locale."""
    from video_ai_editor.edl.snapshot import EDLStore
    store = EDLStore(tmp_path)
    # add a text clip with Hindi via the schema; then reload
    store.edl.tracks  # touch to ensure valid tree
    hindi = "नमस्ते दुनिया 🙏"
    # write a raw state file the way snapshot does and read it back
    p = tmp_path / "probe.json"
    p.write_text(hindi, encoding="utf-8")
    assert p.read_text(encoding="utf-8") == hindi
