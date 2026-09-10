"""Every filesystem-typed tool argument goes through the allowlist.

WHY THIS TEST IS TABLE-DRIVEN AND NOT A CASE STUDY
--------------------------------------------------
A single-tool version of this test — "call `add_clip` with /etc/passwd, assert
it raises" — would have passed for the entire life of 0.5.0 while
`import_srt.path`, `multicam.srcs`, `find_broll.bin` and the three
`export_*.path` args were completely unguarded. `add_clip` was the one tool
anybody had thought about.

So the table below is the specification, and the test derives the SAME list
independently from `/api/tools` and asserts the two agree. Adding a tool with a
path-shaped argument and no entry here fails the run; adding an entry for a
tool whose handler does not actually call a guard fails too. There is no way to
add the seventh hole quietly.

Three assertions, in order of how much they catch:
  1. Coverage — the derived set and the table are identical.
  2. Source — every guarded entry's handler really calls `_safe_src`/`_safe_dst`.
  3. Behaviour — with restriction armed, an out-of-allowlist value is refused.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from lan_fixtures import lan_home  # noqa: F401

DISPATCH_PY = (Path(__file__).resolve().parents[1]
               / "src/video_ai_editor/agent/dispatch.py")

# The table itself moved to `agent/path_args.py` (spec §1.3 rule 6) because
# the Prompt Editor's plan validator needs the SAME rows at runtime. This test
# keeps deriving the candidate set independently from /api/tools and asserting
# the two agree, and keeps the count pin, so the move changes nothing about
# what a new path-shaped argument has to pass.
from video_ai_editor.agent.path_args import PATH_ARGS as EXPECTED_GUARDS

#: An argument NAME that looks like a path. Kept separate from the description
#: heuristic so a tool with an empty description still gets caught.
#
# `name` is in the list even though most `name` args are labels, because the
# one that was NOT — `save_show_template.name` — was an arbitrary file write
# that this derivation could not see. Six tools have to carry an `exempt` row
# as a result. That is the correct trade: the cost of a false positive is one
# line with a reason written next to it.
_NAME_HINT = re.compile(
    r"(?:^|_)(src|srcs|path|paths|file|files|dir|bin|folder|reference|"
    r"end_card|dst|dest|output|out_path|font|name)$")
_DESC_HINT = re.compile(r"\b(path|file|folder|directory)\b", re.I)


def _advertised_path_args() -> set[tuple[str, str]]:
    """Derive the candidate set from the live tool catalogue, not from memory.

    Deliberately over-inclusive: a false positive costs one `exempt` row with a
    written reason, and a false negative is a security hole.
    """
    from video_ai_editor.agent.dispatch import list_tools
    found: set[tuple[str, str]] = set()
    for tool in list_tools():
        props = (tool.get("input_schema") or {}).get("properties") or {}
        for arg, spec in props.items():
            desc = spec.get("description") or ""
            if _NAME_HINT.search(arg) or _DESC_HINT.search(desc):
                found.add((tool["name"], arg))
    return found


def _handler_source(tool: str) -> str:
    """Source text of the handler DISPATCH maps `tool` to."""
    from video_ai_editor.agent.dispatch import DISPATCH
    fn = DISPATCH[tool]
    tree = ast.parse(DISPATCH_PY.read_text(encoding="utf-8"))
    lines = DISPATCH_PY.read_text(encoding="utf-8").splitlines()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == fn.__name__:
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"no source found for {tool} -> {fn.__name__}")


# --- 1. coverage -------------------------------------------------------------

def test_every_advertised_path_arg_has_a_table_entry():
    advertised = _advertised_path_args()
    missing = advertised - set(EXPECTED_GUARDS)
    extra = set(EXPECTED_GUARDS) - advertised
    assert not missing, (
        "these tool args look like filesystem paths and have no guard "
        f"decision recorded: {sorted(missing)}. Add a 'read'/'write' entry and "
        "route the handler through _safe_src/_safe_dst, or an 'exempt' entry "
        "with the reason it is not a path.")
    assert not extra, (
        f"EXPECTED_GUARDS names args /api/tools no longer advertises: "
        f"{sorted(extra)}")


def test_the_guard_count_is_pinned():
    """A bare count, so a rename cannot quietly shrink the table."""
    assert len(EXPECTED_GUARDS) == 22  # +1 on 2026-09-08: apply_lut.lut_path (alias of src) is now advertised
    assert sum(1 for v in EXPECTED_GUARDS.values() if v == "read") == 10
    assert sum(1 for v in EXPECTED_GUARDS.values() if v == "write") == 3


# --- 2. source ---------------------------------------------------------------

@pytest.mark.parametrize("tool,arg,guard",
                         [(t, a, g) for (t, a), g in sorted(EXPECTED_GUARDS.items())
                          if g != "exempt"])
def test_handler_calls_the_right_guard(tool, arg, guard):
    src = _handler_source(tool)
    wanted = "_safe_src(" if guard == "read" else "_safe_dst("
    assert wanted in src, (
        f"{tool}.{arg} is a {guard} path and its handler never calls {wanted}")


def test_export_handlers_guard_before_they_mkdir():
    """`mkdir(parents=True)` on a rejected destination is still a filesystem
    side effect an unauthenticated caller should not be able to cause — so the
    guard has to come first, not merely exist."""
    for tool in ("export_srt", "export_vtt", "export_ass"):
        src = _handler_source(tool)
        assert src.index("_safe_dst(") < src.index("mkdir("), tool


def test_no_unguarded_path_arg_survives_in_the_source():
    """The grep that would have caught the original six.

    Named args reaching `Path(...)` or a bare read with no guard on the same
    line. Written as a source scan rather than a behaviour test because a tool
    whose ffmpeg dependency is missing never gets far enough to be guarded, and
    would pass a behaviour-only check by failing early.
    """
    text = DISPATCH_PY.read_text(encoding="utf-8")
    offenders = []
    patterns = [r'args\["path"\]', r'args\["src"\]', r'args\.get\("srcs"\)',
                r'args\.get\("bin"\)', r'args\["reference"\]',
                r'args\.get\("end_card"\)', r'args\.get\("path"\)']
    for i, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        for pat in patterns:
            if not re.search(pat, line):
                continue
            if "_safe_src(" in line or "_safe_dst(" in line:
                continue
            # An arg that is genuinely not a filesystem path carries an inline
            # `# path-guard: exempt (reason)` marker. Requiring the marker means
            # the exemption is a decision somebody wrote down, not an omission.
            if "path-guard: exempt" in line:
                continue
            # `raw_srcs` and `end_card` are read into a local first and guarded
            # a few lines later after a validity check; allow that shape.
            window = "\n".join(text.splitlines()[i - 1:i + 8])
            if "_safe_src(" in window or "_safe_dst(" in window:
                continue
            offenders.append(f"{i}: {line.strip()}")
    assert not offenders, (
        "unguarded filesystem args in dispatch.py:\n" + "\n".join(offenders))


# --- 3. behaviour ------------------------------------------------------------

def _armed(monkeypatch, tmp_path):
    """Arm restriction the way LAN mode does, with WORKDIR under tmp_path."""
    from video_ai_editor import config
    from video_ai_editor.api import pairing
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    pairing.set_lan_enabled(True)
    assert config.restrict_paths_active() is True


def test_show_template_names_cannot_escape_presets(lan_home, tmp_path, monkeypatch):
    """The behaviour half of the two `exempt` rows for the show templates.

    Armed exactly as LAN mode arms it, because the bug this pins was invisible
    with restriction ON: the write never consulted the allowlist at all, so
    arming it changed nothing. The assertion is therefore on `_show_path`'s own
    refusal, not on `assert_write_path_allowed`.
    """
    from video_ai_editor.show import templates
    _armed(monkeypatch, tmp_path)
    # `shows_dir()` resolves `PRESETS_DIR` out of this module's globals on every
    # call, so redirecting the name here sends the whole test at tmp_path. It
    # must be redirected: the happy-path assertion below writes a real file, and
    # against the shipped constant that file lands in the REPO's `presets/shows`
    # — which is exactly how `presets/shows/my-show_01.json` came to be sitting
    # untracked in a clean tree. A test that leaves an artefact behind also
    # makes itself order-dependent, since `load_show` on the second run reads
    # what the first run wrote rather than what this run saved.
    monkeypatch.setattr(templates, "PRESETS_DIR", tmp_path / "presets")
    escapes = [
        "../../../../../../../../tmp/ESCAPED",
        "../settings",
        "/tmp/absolute",
        "nested/name",
        "..",
    ]
    for bad in escapes:
        with pytest.raises(ValueError):
            templates.save_show(bad, __import__(
                "video_ai_editor.edl", fromlist=["EDL"]).EDL())
        with pytest.raises(ValueError):
            templates.load_show(bad)
    # And the ordinary case still works, so the whitelist is not just "no".
    from video_ai_editor.edl import EDL
    written = templates.save_show("my-show_01", EDL())
    assert written.parent == templates.shows_dir()
    assert templates.load_show("my-show_01")


def test_reads_outside_the_allowlist_are_refused(lan_home, tmp_path, monkeypatch):
    from video_ai_editor import config
    _armed(monkeypatch, tmp_path)
    for probe in ("/etc/passwd", "~/.ssh/id_ed25519", "/var/db/anything"):
        with pytest.raises(ValueError, match="outside the allowed roots"):
            config.assert_path_allowed(probe)


def test_writes_outside_the_allowlist_are_refused(lan_home, tmp_path, monkeypatch):
    """The half that mattered most: `export_srt(path="~/.zshrc")` was code
    execution on the Mac at the user's next login."""
    from video_ai_editor import config
    _armed(monkeypatch, tmp_path)
    for probe in ("~/.zshrc", "~/Library/LaunchAgents/evil.plist", "/tmp/x.srt"):
        with pytest.raises(ValueError, match="outside the allowed roots"):
            config.assert_write_path_allowed(probe)


def test_export_srt_refuses_a_hostile_destination_without_creating_it(
        lan_home, tmp_path, monkeypatch):
    """End to end through the real handler, including the mkdir ordering."""
    from video_ai_editor.agent.dispatch import export_srt_tool
    from video_ai_editor.edl import EDLStore
    from video_ai_editor.ingest.transcribe import Transcript

    session = tmp_path / "s_abc1234567"
    session.mkdir(parents=True)
    _armed(monkeypatch, tmp_path)
    store = EDLStore(session)
    (session / "transcript.json").write_text(
        Transcript(segments=[], language="en", duration=0.0).model_dump_json(),
        encoding="utf-8")

    hostile = tmp_path.parent / "outside" / "nested" / "evil.srt"
    with pytest.raises(ValueError, match="outside the allowed roots"):
        export_srt_tool(store, {"path": str(hostile)})
    assert not hostile.parent.exists(), "the guard ran after mkdir"


def test_import_srt_refuses_a_file_outside_the_allowlist(
        lan_home, tmp_path, monkeypatch):
    """This one is an exfiltration path, not just a read: whatever it parses
    becomes the project transcript, which GET /transcript hands straight back."""
    from video_ai_editor.agent.dispatch import import_srt_tool
    from video_ai_editor.edl import EDLStore

    session = tmp_path / "s_abc1234567"
    session.mkdir(parents=True)
    _armed(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="outside the allowed roots"):
        import_srt_tool(EDLStore(session), {"path": "/etc/hosts"})


def test_set_property_src_value_is_guarded(lan_home, tmp_path, monkeypatch):
    """`set_property(clip, "src", "/etc/passwd")` is the same primitive as
    `add_clip.src`; guarding one and not the other just moves the hole."""
    from video_ai_editor.agent.dispatch import set_property
    from video_ai_editor.edl import EDLStore
    from video_ai_editor.edl.schema import Clip

    session = tmp_path / "s_abc1234567"
    session.mkdir(parents=True)
    _armed(monkeypatch, tmp_path)
    store = EDLStore(session)
    clip = Clip(src=str(tmp_path / "a.mp4"), in_=0.0, out=1.0, start=0.0)
    store.edl.get_track("v1").clips.append(clip)
    with pytest.raises(ValueError, match="outside the allowed roots"):
        set_property(store, {"clip_id": clip.id, "path": "src",
                             "value": "/etc/passwd"})


def test_guards_are_inert_while_restriction_is_off(lan_home, tmp_path):
    """The `git diff` promise: on a default desktop the six new guards resolve
    a path and change nothing else."""
    from video_ai_editor import config
    from video_ai_editor.agent.dispatch import _safe_dst, _safe_src
    assert config.restrict_paths_active() is False
    assert _safe_src("/etc/hosts") == str(Path("/etc/hosts").resolve())
    assert _safe_dst("/tmp/anything.srt") == Path("/tmp/anything.srt").resolve()
