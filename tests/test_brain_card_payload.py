"""Editor Brain surfaces (EB1-F): reasons on the change card and the honest reply.

A brain run's card stays DIFF-derived (`changes.summarize` never omits a
change); the decisions only add a WHY per line, keyed by the resolver's
footprint (`<scratch>/brain/footprint.json`) and the frozen EDP
(`<session>/brain/decisions/<did>.json`), and cuts collapse into one grouped
line with the reason tally ("Removed 3 stretches: 1 silence, 1 filler, 1
false start"). E8's rule: a decision never claims more than the diff shows —
a footprint naming an entity no diff key mentions is `applied: false` and
decorates no line. With `brain.enabled` off the card is byte-for-byte the
0.8.0 card.

The scenario is a dry run built by hand on `preview.scratch_store` (three
`cut_range`s, captions, a bed) with the hand-written EDP and footprint from
tests/brain_card_fixtures.py — the surfaces read those files; lanes C/E
write them.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import brain_card_fixtures as BF  # noqa: E402
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import no_downloads  # noqa: E402,F401

from video_ai_editor import config, storage  # noqa: E402
from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.agent.prompt import changes as C  # noqa: E402
from video_ai_editor.agent.prompt import pending, preview as PV  # noqa: E402
from video_ai_editor.agent.prompt.schema import Plan, Step  # noqa: E402

RUN_ID = "r_brain_card"


@pytest.fixture
def scene(tmp_path: Path, monkeypatch):
    """A live session, a scratch copy after a hand-run 'brain' plan, and the
    EDP + footprint the surfaces read."""
    before_restrict = config._FORCED_RESTRICT
    config.enable_path_restriction(False)
    monkeypatch.setattr(storage, "WORKDIR", tmp_path)
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    live = F.make_store(tmp_path, name="s_braincard")
    dispatch(live, "set_canvas", {"w": 1080, "h": 1920})
    bed = F.music_bed(tmp_path, dur=12.0)
    src = str(live.edl.get_track("v1").clips[0].src)
    orig_id = live.edl.get_track("v1").clips[0].id
    before = live.edl.model_copy(deep=True)
    scratch = PV.scratch_store(live, RUN_ID)
    for a, b in ((9.0, 10.0), (5.0, 5.4), (3.0, 3.8)):        # last first, as the tool does
        dispatch(scratch, "cut_range", {"track": "v1", "start": a, "end": b})
    dispatch(scratch, "add_caption_track", {"style": "ig_chunky", "position": "bottom"})
    dispatch(scratch, "add_music", {"src": str(bed), "start": 0.0, "volume_db": -20.0, "duck": True})
    after = scratch.edl.model_copy(deep=True)
    BF.write_edp(Path(live.dir), BF.edp(src))
    BF.write_footprint(Path(scratch.dir), BF.footprint(BF.DID, [orig_id], ghost="c_ghost0000"))
    plan = Plan.new(intent="edit", brain="recipes", content_brain="apple_intelligence",
                    title="Talking head → 45 s Reel", steps=[
                        Step(tool="cut_source_ranges", args={"track": "v1", "ranges": "$brain:cuts",
                                                             "plan_ref": BF.DID}, why="3 cuts", stage=2),
                        Step(tool="add_caption_track", args={"style": "ig_chunky", "position": "bottom"},
                             why="captions", stage=7),
                        Step(tool="add_music", args={"src": str(bed), "volume_db": -20.0, "duck": True},
                             why="bed", stage=9)], postconditions=[])
    yield SimpleNamespace(live=live, scratch=scratch, before=before, after=after, plan=plan, src=src,
                          orig_id=orig_id, root=tmp_path)
    PV.discard_scratch(scratch)
    config.enable_path_restriction(before_restrict)


class _Log:
    def __init__(self) -> None:
        self.record = SimpleNamespace(run_id=RUN_ID)
        self.events: list[dict] = []
        self.reply = None
        self.status = None

    def emit(self, evt: dict) -> None:
        self.events.append(evt)

    def set_reply(self, text: str) -> None:
        self.reply = text

    def set_status(self, status: str, **_: object) -> None:
        self.status = status


def _pause(scene) -> tuple[dict, _Log]:
    log = _Log()
    facts = F.facts_for(scene.live)
    PV.pause_for_confirm(scene.live, plan=scene.plan, prompt="make a 45-second reel", facts=facts,
                         before=scene.before, after=scene.after, results=[], base_hash=scene.before.hash(),
                         fingerprint=C.canonical(scene.after, scene.before), log=log, ui_state=None,
                         contract_hint={}, consented=frozenset())
    record = pending.load_pending(Path(scene.live.dir))
    assert record is not None
    return record, log


def test_every_diff_line_of_a_brain_run_has_a_why(scene):
    from video_ai_editor.agent.prompt import brain_card
    changes = C.summarize(scene.before, scene.after, session_dir=Path(scene.live.dir))
    display, payload = brain_card.card_payload(scene.plan, scene.live, Path(scene.scratch.dir),
                                               scene.before, scene.after, changes)
    assert payload is not None and payload["decisions_id"] == BF.DID
    # the card never omits a change: the display lines still claim every diff key
    assert C.covered(display) >= C.diff_keys(scene.before, scene.after)
    assert len(display) >= 1
    for ch in display:
        assert ch.why, f"line without a why: {ch.text!r}"
    assert payload["unexplained"] == []
    # the why column is aligned with the lines
    assert len(payload["whys"]) == len(display) and all(payload["whys"])
    assert payload["whys"] == [ch.why for ch in display]
    # the cuts collapsed into one grouped line with the reason tally
    grouped = [ch for ch in display if ch.text.startswith("Removed 3 stretches")]
    assert len(grouped) == 1, [ch.text for ch in display]
    assert grouped[0].text == "Removed 3 stretches: 1 silence, 1 filler, 1 false start"
    assert not any(ch.text.startswith("Deleted") for ch in display)
    # the kept pause is on the Plan tab, never a line (it is the absence of a cut)
    kinds = {d["id"]: d for d in payload["decisions"]}
    assert kinds["k_0004"]["applied"] is None and kinds["k_0004"]["code"] == "pause_kept:emotion"
    assert payload["tab_default"] == "plan"
    assert payload["summary"]["hook"]["quote"].startswith("Ninety percent")
    assert payload["summary"]["hook"]["timeline_t"] == pytest.approx(2.0)
    assert payload["summary"]["hook"]["src"] == "talk.mp4"          # the media bin's name, never the .normalized copy
    assert payload["deferred"][0]["asked"] == "per-word caption highlight"
    assert scene.root.as_posix() not in json.dumps(payload)


def test_decisions_never_claim_more_than_the_diff(scene):
    """E8: k_0007 (a punch-in) claims a clip no diff key mentions."""
    from video_ai_editor.agent.prompt import brain_card
    changes = C.summarize(scene.before, scene.after, session_dir=Path(scene.live.dir))
    display, payload = brain_card.card_payload(scene.plan, scene.live, Path(scene.scratch.dir),
                                               scene.before, scene.after, changes)
    by_id = {d["id"]: d for d in payload["decisions"]}
    assert by_id["k_0007"]["applied"] is False
    assert payload["overclaims"] == ["k_0007"]
    assert not any("punch in" in (ch.why or "") for ch in display)
    diff_clips = brain_card.clip_ids_in(C.diff_keys(scene.before, scene.after))
    fp = brain_card.load_footprint([Path(scene.scratch.dir)])
    for d in payload["decisions"]:
        if d["applied"] is not True:
            continue
        claimed = set(fp.get(d["id"], {}).get("clip_ids") or [])
        assert not claimed or claimed & diff_clips, (d["id"], claimed)
    # and the applied cuts are exactly the ones the footprint ties to the diff
    assert [d["id"] for d in payload["decisions"] if d["applied"] is True and d["kind"] == "cut_range"] \
        == ["k_0001", "k_0002", "k_0003"]


def test_card_record_carries_the_payload_and_keeps_apply_lines_raw(scene):
    record, log = _pause(scene)
    info = record["preview"]
    raw = PV.lines_for(scene.before, scene.after, [], Path(scene.live.dir))
    assert info["all_lines"] == raw                       # what Apply is checked against
    assert info["brain"]["decisions_id"] == BF.DID
    assert info["lines"][0].startswith("Removed 3 stretches")
    assert info["total"] == len(info["lines"]) + len(info["hidden"]) == len(info["brain"]["whys"])
    clarify = next(e for e in log.events if e["type"] == "clarify")
    assert clarify["preview"]["brain"]["tab_default"] == "plan"
    assert clarify["preview"]["lines"] == info["lines"]
    assert PV.public_view(record)["brain"]["decisions_id"] == BF.DID
    # the card as text (the chat pane, the phone) leads with the plan summary line
    assert "Removed 3 stretches" in log.reply
    # the card says how long the result is (UX-12); only a brain card does
    assert info["summary"].endswith(f" · {scene.after.duration:.1f} s when applied"), info["summary"]
    assert PV.apply_check(info)(scene.scratch, SimpleNamespace(edl_before=scene.before, steps=[])) is None


def test_flag_off_card_is_byte_for_byte_the_old_card(scene, monkeypatch):
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "0")
    record, log = _pause(scene)
    info = record["preview"]
    raw = PV.lines_for(scene.before, scene.after, [], Path(scene.live.dir))
    cl = C.change_list([C.Change(group="", text=t) for t in raw])
    assert "brain" not in info and "brain" not in PV.public_view(record)
    assert info["lines"] == cl.lines and info["hidden"] == cl.all_lines[len(cl.lines):]
    assert info["all_lines"] == raw and info["total"] == cl.total
    clarify = next(e for e in log.events if e["type"] == "clarify")
    assert set(clarify["preview"]) == {"summary", "lines", "more", "total", "hidden", "note", "nothing_changed"}
    assert log.reply == PV.reply_text("Recipes", PV.summary_line(scene.plan, cl.total), cl.all_lines, 0, None)


def test_an_ordinary_plan_gets_no_payload_even_with_the_flag_on(scene):
    from video_ai_editor.agent.prompt import brain_card
    plain = Plan.new(intent="mute", brain="recipes", steps=[Step(tool="set_clip_muted", args={"clip_id": "$v1_first",
                                                                                              "muted": True},
                                                                 why="mute")], postconditions=[])
    changes = C.summarize(scene.before, scene.after, session_dir=Path(scene.live.dir))
    display, payload = brain_card.card_payload(plain, scene.live, Path(scene.scratch.dir),
                                               scene.before, scene.after, changes)
    assert payload is None and display == changes


def _step(tool: str) -> SimpleNamespace:
    return SimpleNamespace(index=0, tool=tool, args=[{}], status="ok", results=[{}], error=None, effect=None,
                           notices=[])


def test_reply_names_rungs_and_deferred(scene):
    from video_ai_editor.agent.prompt import brain_card, summary
    brain_card.register_plan(scene.plan, Path(scene.live.dir))
    result = SimpleNamespace(error=None, steps=[_step("cut_source_ranges"), _step("add_caption_track")],
                             committed=True, new_sessions=[], child_runs=[])
    verify = {"passed": 3, "total": 3, "checks": []}
    text = summary.compose_reply(scene.plan, result, verify)
    assert text.startswith("via Recipes · moments by Apple Intelligence — "), text
    assert "text by" not in text
    assert "Not done this time: per-word caption highlight (next wave), music intro and outro (next wave)." in text
    assert "Undo with ⌘Z" in text
    # a recorded version is named
    brain_card.remember_version(BF.DID, "V1 Reel")
    assert "V1 Reel" in summary.compose_reply(scene.plan, result, verify)
    # an ordinary plan's reply is untouched: "text by" for a content brain
    plain = Plan.new(intent="hook", brain="recipes", content_brain="apple_intelligence",
                     steps=[Step(tool="apply_hook_stack", args={"text": "x"}, why="hook")], postconditions=[])
    plain_text = summary.compose_reply(plain, SimpleNamespace(error=None, steps=[_step("apply_hook_stack")],
                                                                committed=True, new_sessions=[], child_runs=[]),
                                       verify)
    assert plain_text.startswith("via Recipes · text by Apple Intelligence — ") and "Not done" not in plain_text


def test_version_is_recorded_after_an_applied_edit_plan(scene, monkeypatch):
    """service records V1 <target> through the versions seam once the run
    thread committed a brain plan; never for an ordinary plan."""
    from video_ai_editor.agent.prompt import brain_card, brain_seams, service
    calls: list[dict] = []

    class _Versions:
        def record(self, store, *, label, decisions_id, kind):
            calls.append({"dir": Path(store.dir), "label": label, "decisions_id": decisions_id, "kind": kind})
            return {"id": "v_1", "label": label}

    monkeypatch.setattr(brain_seams, "versions", lambda: _Versions())
    handle = SimpleNamespace(result=SimpleNamespace(committed=True, op={"tool": "prompt"}), mode="apply")
    service.record_brain_version(scene.live, scene.plan, handle)
    assert calls == [{"dir": Path(scene.live.dir), "label": "V1 Reel", "decisions_id": BF.DID, "kind": "brain"}]
    assert brain_card.version_label(BF.DID) == "V1 Reel"
    plain = Plan.new(intent="mute", brain="recipes", steps=[Step(tool="set_clip_muted", args={}, why="m")],
                     postconditions=[])
    service.record_brain_version(scene.live, plain, handle)
    service.record_brain_version(scene.live, scene.plan, SimpleNamespace(result=SimpleNamespace(committed=False,
                                                                                                op=None)))
    assert len(calls) == 1


def test_a_version_is_named_by_what_the_edit_made():
    """EB1 integration: the EDP's `target` is `reel | episode` (the planner's
    own vocabulary); the Versions strip names a reel "Reel" and an episode
    by its style — "V1 Premium Podcast" is the slice's label."""
    from video_ai_editor.agent.prompt import brain_card
    assert brain_card.version_noun({"style": "viral_reel", "summary": {"target": "reel"}}) == "Reel"
    assert brain_card.version_noun({"style": "premium_podcast", "summary": {"target": "reel"}}) == "Reel"
    assert brain_card.version_noun({"style": "premium_podcast", "summary": {"target": "episode"}}) == "Premium Podcast"
    assert brain_card.version_noun({"style": "clean_professional", "summary": {"target": "episode"}}) == "Clean Edit"
    assert brain_card.version_noun({"style": "viral_reel", "summary": {"target": "episode"}}) == "Episode"
    assert brain_card.version_noun({"summary": {"target": "Reel"}}) == "Reel"          # a hand-written EDP's own noun
    assert brain_card.version_noun({}) == "Edit"


# ---------------------------------------------------------------------------
# UX-06 (E8 marking, the why attribution) over the two REAL demo runs: the
# talking-head reel and the two-camera premium podcast, as the re-testers'
# Mac applied them (tests/goldens/brain/card/<case>/{before,after,edp,footprint}.json:
# the EDL before and after the prompt op, the frozen EDP, the resolver's footprint).
# ---------------------------------------------------------------------------

GOLD = Path(__file__).parent / "goldens" / "brain" / "card"


def _run_of(case: str):
    from video_ai_editor.edl.schema import EDL
    d = GOLD / case
    before = EDL.model_validate_json((d / "before.json").read_text(encoding="utf-8"))
    after = EDL.model_validate_json((d / "after.json").read_text(encoding="utf-8"))
    edp = json.loads((d / "edp.json").read_text(encoding="utf-8"))
    raw = json.loads((d / "footprint.json").read_text(encoding="utf-8"))
    return before, after, edp, raw, d


def _card_of(case: str, tmp_path: Path):
    from video_ai_editor.agent.prompt import brain_card
    before, after, edp, raw, d = _run_of(case)
    (tmp_path / "brain").mkdir(exist_ok=True)
    (tmp_path / "brain" / "footprint.json").write_text(json.dumps(raw), encoding="utf-8")
    fp = brain_card.load_footprint([tmp_path])
    changes = C.summarize(before, after)
    plan = Plan.new(intent="edit", brain="recipes", title="x", steps=[], postconditions=[])
    display, payload = brain_card.build(plan, edp, fp, before, after, changes, None)
    return SimpleNamespace(before=before, after=after, edp=edp, raw=raw, changes=changes, display=display,
                           payload=payload)


@pytest.fixture(params=["th", "p2"])
def real(request, tmp_path):
    return _card_of(request.param, tmp_path), request.param


def _footprint_clips(raw: dict, did: str) -> set[str]:
    for entries in raw["steps"].values():
        for e in entries:
            if e["decision"] == did:
                return set(e.get("clip_ids") or [])
    return set()


def test_no_applied_decision_is_struck_through(real):
    """UX-06: the reel really opens on the hook; the card struck 'Opening 1 ·
    1 not applied' through because it looked for a Video line with
    'order/moved/first' in it. A decision is not applied only when its
    footprint is absent from the diff — proved here from the trees."""
    from video_ai_editor.agent.prompt import brain_card
    from video_ai_editor.agent.timemap import media_clips
    card, case = real
    rows = {d["id"]: d for d in card.payload["decisions"]}
    before_ids = {c.id for t in card.before.tracks for c in t.clips}
    after_ids = {c.id for t in card.after.tracks for c in t.clips}
    diff_clips = brain_card.clip_ids_in(C.diff_keys(card.before, card.after)) | (after_ids ^ before_ids)
    for did, row in rows.items():
        if row["kind"] == "keep_pause":
            assert row["applied"] is None
            continue
        clips = _footprint_clips(card.raw, did)
        if row["applied"] is False:
            assert not (clips & diff_clips), (did, "struck although its footprint is in the diff")
        elif clips:
            assert clips & diff_clips, (did, "claimed without evidence")
    assert card.payload["overclaims"] == []
    assert [d for d, r in rows.items() if r["applied"] is False] == []
    if case == "th":
        first = media_clips(card.after, "v1")[0]
        hook = card.edp["summary"]["hook"]
        assert (first.in_, first.out) == pytest.approx((hook["t0"], hook["t1"]), abs=0.05)   # the reel opens on it
        assert rows["k_0012"]["kind"] == "open_on" and rows["k_0012"]["applied"] is True


def test_an_opening_that_did_not_happen_is_still_struck(tmp_path):
    """E8 stays honest: move the opening clip away from the front and the
    same decision is `not applied`, with a note that says so."""
    from video_ai_editor.agent.prompt import brain_card
    from video_ai_editor.agent.timemap import media_clips
    before, after, edp, raw, _ = _run_of("th")
    v1 = after.get_track("v1")
    first = media_clips(after, "v1")[0]
    other = media_clips(after, "v1")[1]
    first.start, other.start = other.start, first.start           # a reel that does not open on the hook
    v1.clips.sort(key=lambda c: c.start)
    (tmp_path / "brain").mkdir()
    (tmp_path / "brain" / "footprint.json").write_text(json.dumps(raw), encoding="utf-8")
    fp = brain_card.load_footprint([tmp_path])
    plan = Plan.new(intent="edit", brain="recipes", title="x", steps=[], postconditions=[])
    _, payload = brain_card.build(plan, edp, fp, before, after, C.summarize(before, after), None)
    row = next(d for d in payload["decisions"] if d["id"] == "k_0012")
    assert row["applied"] is False and "open" in (row["note"] or "")


def test_every_why_names_a_decision_with_a_matching_footprint(real):
    """UX-06: 'fit to frame' carried the cuts' reasons, 'muted' carried the
    trim's, the bitrate line said 'Platform: reels; Platform: reels'. A why
    is written from decisions whose footprint the LINE came from."""
    from video_ai_editor.agent.prompt import brain_card
    card, _case = real
    p = card.payload
    rows = {d["id"]: d for d in p["decisions"]}
    assert len(p["why_ids"]) == len(card.display) == len(p["whys"])
    for i, ch in enumerate(card.display):
        ids = p["why_ids"][i]
        if i in p["unexplained"]:
            assert ids == [] and ch.why == brain_card.UNEXPLAINED
            continue
        assert ids, f"a why without a decision: {ch.text!r}"
        line_clips = brain_card.clip_ids_in(ch.keys)
        for did in ids:
            clips = _footprint_clips(card.raw, did)
            assert rows[did]["applied"] is True
            assert not clips or clips & line_clips, (ch.text, did, "the footprint is elsewhere")
        assert "; ;" not in ch.why and not ch.why.startswith(";")


def test_th_lines_carry_the_reason_of_what_did_them(tmp_path):
    card = _card_of("th", tmp_path)
    p, by_text = card.payload, {ch.text: (ch, p_ids) for ch, p_ids in zip(card.display, card.payload["why_ids"])}
    kinds = {d["id"]: d["kind"] for d in p["decisions"]}

    def ids_of(prefix: str) -> list[str]:
        return next(ids for text, (_c, ids) in by_text.items() if text.startswith(prefix))

    assert {kinds[i] for i in ids_of("All 10 clips of the video: fit to frame")} == {"reframe"}
    assert {kinds[i] for i in ids_of("All 10 clips of the video: muted")} == {"dialogue"}
    assert {kinds[i] for i in ids_of("Canvas export bitrate")} == {"export_preset"}
    assert {kinds[i] for i in ids_of("Canvas 1920x1080")} == {"reframe"}
    assert {kinds[i] for i in ids_of("Added music")} == {"music"}
    bitrate = next(ch for ch in card.display if ch.text.startswith("Canvas export bitrate"))
    assert bitrate.why.count("Platform") <= 1


def test_p2_camera_line_is_the_switches_and_the_mutes_are_the_dialogues(tmp_path):
    card = _card_of("p2", tmp_path)
    kinds = {d["id"]: d["kind"] for d in card.payload["decisions"]}
    by = {ch.text.split(":")[0].split(" from")[0] if ch.text.startswith(("Camera", "Dialogue")) else ch.text[:20]:
          card.payload["why_ids"][i] for i, ch in enumerate(card.display)}
    assert {kinds[i] for i in by["Camera"]} == {"switch_angle"}
    assert {kinds[i] for i in by["Camera sound muted on 13 video clips"]} == {"dialogue"}
    assert {kinds[i] for i in by["Dialogue"]} == {"dialogue"}


def test_the_card_never_shows_a_graph_key_or_a_normalized_name(real):
    import re as _re
    card, _case = real
    blob = json.dumps({"summary": card.payload["summary"], "rungs": card.payload["rungs"],
                       "decisions": [{k: v for k, v in d.items() if k != "text"} for d in card.payload["decisions"]]})
    assert not _re.search(r"src_[0-9a-f]{8,}", blob), _re.findall(r"src_[0-9a-f]{8,}", blob)
    assert ".normalized." not in blob


def test_every_decision_with_a_source_range_can_seek(real):
    """UX-05: a seek button per decision that has a source range (not only
    the hook): the payload carries where it plays in the CURRENT timeline."""
    card, case = real
    seekable = [d for d in card.payload["decisions"] if d["ref"] and d["kind"] in ("cut_range", "keep_pause")]
    assert seekable
    on_line = [d for d in seekable if d.get("timeline_t") is not None]
    assert len(on_line) >= len(seekable) - 1, [(d["id"], d.get("timeline_t")) for d in seekable]
    for d in on_line:
        assert 0.0 <= d["timeline_t"] <= card.before.duration + 1e-3
    if case == "th":
        cut = next(d for d in card.payload["decisions"] if d["id"] == "k_0003")
        assert cut["timeline_t"] == pytest.approx(14.3667, abs=0.01)     # the cut's own second, before the run moved it


def test_the_payload_says_how_long_the_result_is(real):
    card, case = real
    s = card.payload["summary"]
    assert s["result_s"] == pytest.approx(card.after.duration, abs=0.06)
    assert s["before_s"] == pytest.approx(card.before.duration, abs=0.06)
    if case == "th":
        assert 44.0 < s["result_s"] < 46.0


# ---------------------------------------------------------------------------
# UX-13: the reply of a brain run reads as one clean piece of prose
# ---------------------------------------------------------------------------

LONG_QUOTE = ("I now finish forty percent more of what I plan, which is the best stretch of my working life "
              "and the reason I stopped answering email before noon.")
PLANNER_REPLY = ("opens on “I now finish forty percent more of what I plan, which is the best stretch of my ”; "
                 "talking head → 45 s reel: 3 cuts, captions; dialogue lane skipped: the source has no file path; "
                 "not done this time — per-word caption highlight: next wave; "
                 "not done this time — music intro and outro: next wave")


def _reply_scene(scene, quote: str = LONG_QUOTE, reply: str = PLANNER_REPLY):
    from video_ai_editor.agent.prompt import brain_card
    edp = BF.edp(scene.src)
    edp["summary"]["hook"]["quote"] = quote
    BF.write_edp(Path(scene.live.dir), edp)
    plan = scene.plan.model_copy(update={"reply": reply, "title": "Edit"})
    brain_card.register_plan(plan, Path(scene.live.dir))
    return plan


def _ran(*tools: str) -> SimpleNamespace:
    return SimpleNamespace(error=None, steps=[_step(t) for t in tools], committed=True, new_sessions=[],
                           child_runs=[])


def _checks(*rows) -> dict:
    checks = [{"check": c, "human": h, "pass": ok, "measured": m, "expected": e, "headline": True,
               "blocking": blocking, **({"detail": d} if d else {})} for c, h, ok, m, e, blocking, d in rows]
    return {"passed": sum(1 for c in checks if c["pass"]), "total": len(checks), "checks": checks}


def test_the_reply_says_each_thing_once_and_quotes_the_hook_whole(scene):
    from video_ai_editor.agent.prompt import summary
    plan = _reply_scene(scene)
    text = summary.compose_reply(plan, _ran("cut_source_ranges", "add_caption_track"),
                                 _checks(("captions_nonempty", "captions were laid", True, 27, "≥ 1", False, None)))
    assert text.count("Not done this time") == 1 and "not done this time" not in text, text
    assert text.count("opens on") + text.count("Opens on") == 1, text
    # the hook is quoted WHOLE or cut at a word with an ellipsis — never mid-word, never run into the next sentence
    quote = text.split("Opens on “", 1)[1].split("”", 1)[0]
    assert quote.endswith("…") and LONG_QUOTE.startswith(quote[:-1].rstrip()), quote
    assert LONG_QUOTE[len(quote[:-1].rstrip())] == " ", "cut in the middle of a word"
    assert "…”. " in text or "…”." in text
    # the planner's own notes survive, once, as sentences
    assert "Dialogue lane skipped: the source has no file path." in text, text
    assert "talking head → 45 s reel: 3 cuts, captions" in text
    assert "; not done" not in text and "”; " not in text
    # a short quote is quoted whole
    short = summary.compose_reply(_reply_scene(scene, "Ninety percent of first cuts are thrown away."),
                                  _ran("cut_source_ranges"), None)
    assert "Opens on “Ninety percent of first cuts are thrown away.”" in short, short


def test_the_reply_states_the_resulting_length(scene):
    from video_ai_editor.agent.prompt import summary
    plan = _reply_scene(scene)
    live = scene.live
    text = summary.compose_reply(plan, _ran("cut_source_ranges"), None)
    assert f"{live.edl.duration:.1f} s" in text, (text, live.edl.duration)
    assert "now" in text.lower().split(f"{live.edl.duration:.1f} s")[0][-30:], text


def test_a_failed_advisory_check_is_a_note_not_a_failed_step(scene):
    """UX-13: '13 of 14 checks passed · 1 failed' forever, from the aesthetic
    audit's hook score (advisory: 'score is reported, not gated')."""
    from video_ai_editor.agent.prompt import summary
    plan = _reply_scene(scene)
    audit = ("audit_ok", "the aesthetic audit passes", False, {"score": 85, "errors": [], "hook_score": 2},
             {"errors": 0, "hook_score": 3}, False, "score is reported, not gated")
    ok = ("captions_nonempty", "captions were laid", True, 27, "≥ 1", False, None)
    text = summary.compose_reply(plan, _ran("cut_source_ranges", "audit_aesthetic"), _checks(ok, audit))
    assert "done with issues" not in text.lower() and "✗" not in text, text
    assert "1 of 1 check held" in text, text
    assert "Note: the aesthetic audit scored 85 out of 100 and rated the opening 2 of 3" in text, text
    # a REAL failure still leads the reply and the verdict
    real_fail = ("duration_shrank", "the video got shorter", False, 0.0, "≥ 0.10", False, None)
    bad = summary.compose_reply(plan, _ran("cut_source_ranges", "audit_aesthetic"), _checks(ok, real_fail, audit))
    assert "done with issues" in bad.lower() and "✗ the video got shorter" in bad, bad
    # the legacy audit run (not a brain plan) keeps its 0.8.0 wording
    plain = Plan.new(intent="audit", brain="recipes", steps=[Step(tool="audit_aesthetic", args={}, why="a")],
                     postconditions=[])
    legacy = summary.compose_reply(plain, _ran("audit_aesthetic"), _checks(audit))
    assert "done with issues" in legacy and "✗ the aesthetic audit passes" in legacy


def test_no_id_reaches_the_reply(scene):
    from video_ai_editor.agent.prompt import summary
    plan = _reply_scene(scene)
    res = _ran("cut_source_ranges")
    res.steps.append(SimpleNamespace(index=1, tool="cut_source_ranges", args=[{}], status="ok", effect="none",
                                     results=[{"summary": "8 cuts dropped (k_0003, k_0004, k_0005): already removed "
                                                          "earlier in this plan"}], error=None, notices=[]))
    text = summary.compose_reply(plan, res, None)
    assert "k_00" not in text and "d_0a1b" not in text, text
    assert "8 cuts dropped" in text and "already removed earlier in this plan" in text, text


# ------------------------------------------------ finalize: the card's EDP read is capped, its registry pruned

def test_the_registry_is_bounded_and_keeps_the_newest(tmp_path):
    """FX-A / FX-E request: `_REGISTRY` was never pruned, one entry per plan a session ever ran."""
    from video_ai_editor.agent.prompt import brain_card
    brain_card._REGISTRY.clear()
    for i in range(brain_card.REGISTRY_MAX * 3):
        brain_card.remember_version(f"d_{i:08x}", f"V{i}")
    assert len(brain_card._REGISTRY) == brain_card.REGISTRY_MAX
    newest = f"d_{brain_card.REGISTRY_MAX * 3 - 1:08x}"
    assert brain_card.version_label(newest) == f"V{brain_card.REGISTRY_MAX * 3 - 1}"
    assert brain_card.version_label("d_00000000") is None            # the oldest went
    brain_card.remember_version("d_00000000", "again")               # touching one keeps it young
    for i in range(brain_card.REGISTRY_MAX - 1):
        brain_card.remember_version(f"d_{0x9000 + i:08x}", "x")
    assert brain_card.version_label("d_00000000") == "again"
    brain_card._REGISTRY.clear()


def test_load_edp_refuses_a_file_over_the_cap(tmp_path, monkeypatch):
    from video_ai_editor.agent.prompt import brain_card
    did = "d_0badf00d"
    p = brain_card.edp_path(tmp_path, did)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"id": did, "decisions": []}))
    assert brain_card.load_edp(tmp_path, did) is not None
    monkeypatch.setattr(brain_card, "MAX_GRAPH_BYTES", 8)              # the same limit the routes keep, made tiny
    assert brain_card.load_edp(tmp_path, did) is None


def _idle(tool: str, summary: str) -> SimpleNamespace:
    return SimpleNamespace(index=9, tool=tool, args=[{}], status="ok", effect="none", results=[{"summary": summary}],
                           error=None, notices=[])


def test_the_length_is_said_once_and_idle_bookkeeping_steps_are_not_news(scene):
    """Closer review (UX-13): 'The result runs 0:44 … The video is now 44.0 s', 'reframed 0 clips', 'nothing to split'."""
    from video_ai_editor.agent.prompt import summary
    plan = _reply_scene(scene, reply=PLANNER_REPLY + "; the result runs 0:44")
    res = _ran("cut_source_ranges")
    res.steps += [_idle("auto_reframe", "Auto-reframe → 9:16 (no re-encode); reframed 0 clips"),
                  _idle("split_at", "the opening moment is already isolated — nothing to split"),
                  _idle("cut_source_ranges", "8 cuts dropped: already removed in an earlier edit")]
    text = summary.compose_reply(plan, res, None)
    assert "the result runs" not in text.lower() and text.count(" s.") >= 1, text
    assert "The video is now" in text and text.count("The video is now") == 1
    assert "reframed 0 clips" not in text and "nothing to split" not in text, text
    assert "8 cuts dropped" in text, text                     # a step that did nothing about CUTS is still worth saying
