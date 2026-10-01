"""The rules that turn an EDL diff into editor lines (changes.py's engine).

Every rule CLAIMS the flat-state keys it describes; `generic` writes a line
for every key nobody claimed, which is what makes "the card never omits a
change" hold by construction (see changes.py)."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from ...edl.schema import EDL, Clip, Sticker, TextClip
from . import change_rules_brain as _brain
from .change_words import (MISSING, Names, _CAPTION_WORDS, _deg, _effects_words, _fmt_value, _num, _pct,
                           _ratio, _short, _trans_name, _v1, colour, fmt_num, secs, speed_word, split_parent)
from .changes import EPS, GROUP_ORDER, Change, diff_keys, flat_state

#: What `dispatch._ripple_overlays` leaves of a lone title / sticker whose
#: footage was deleted (a 0.1 s stub the person can still find and move).
_STUB_S = 0.1


class Summary:
    def __init__(self, before: EDL, after: EDL, session_dir: Path | None) -> None:
        self.before, self.after = before, after
        self.fb, self.fa = flat_state(before), flat_state(after)
        self.pending = diff_keys(self.fb, self.fa)
        self.names = Names(before, after, session_dir)
        # clip id -> its pending keys, built once: keys_of used to scan every
        # pending key per call, and a remove_silences with hundreds of cuts
        # called it once per piece per merged span (cubic; 24 s for one line).
        self._by_clip: dict[str, set[str]] = {}
        for k in self.pending:
            if k.startswith("clip:"):
                self._by_clip.setdefault(k[5:].split(".", 1)[0], set()).add(k)
        self.changes: list[Change] = []
        self.v1_moved = False            # a main-lane change that re-times what follows
        self._pieces: dict[str, list[Clip]] = {}
        self._wins: dict[str, dict[str, tuple[float, float]]] = {}

    # -- bookkeeping ------------------------------------------------------
    def add(self, group: str, text: str, keys: Iterable[str]) -> None:
        ks = frozenset(k for k in keys if k in self.pending)
        self.pending -= ks
        self.changes.append(Change(group=group, text=text, keys=ks))

    def claim_silently(self, into: int, keys: Iterable[str]) -> None:
        """Fold `keys` into an existing line (the line already says it)."""
        ks = frozenset(k for k in keys if k in self.pending)
        if not ks:
            return
        c = self.changes[into]
        self.changes[into] = Change(group=c.group, text=c.text, keys=c.keys | ks)
        self.pending -= ks

    def keys_of(self, cid: str) -> set[str]:
        return self._by_clip.get(cid, set()) & self.pending

    def val(self, side: str, key: str) -> Any:
        return (self.fb if side == "b" else self.fa).get(key, MISSING)

    # -- main lane ----------------------------------------------------------
    def main_lane(self) -> None:
        before, after = _v1(self.before), _v1(self.after)
        bid = {c.id: c for c in before}
        pieces: dict[str, list[Clip]] = {c.id: [] for c in before}
        self._pieces = pieces
        new: list[Clip] = []
        for c in after:
            if c.id in bid:
                pieces[c.id].append(c)
                continue
            parent = split_parent(c.id, bid)
            (pieces[parent].append(c) if parent else new.append(c))
        deleted: list[tuple[float, float, str]] = []      # before-timeline spans, owner id
        # Parent-source window per piece: an angle piece (apply_camera_plan)
        # plays another file, so its own in/out are not the parent's clock.
        self._wins = {b.id: _brain.angle_windows(b, pieces[b.id]) for b in before}
        for b in before:
            ps = sorted(pieces[b.id], key=lambda p: p.in_)
            if b.freeze is not None:
                if not ps:
                    deleted.append((b.start, b.start + b.effective_duration, b.id))
                continue
            lost = _subtract((b.in_, b.out), [self._wins[b.id][p.id] for p in ps])
            for s0, s1 in lost:
                t0 = b.start + b.timeline_offset_at(s0 - b.in_)
                t1 = b.start + b.timeline_offset_at(s1 - b.in_)
                if t1 - t0 > EPS:
                    deleted.append((t0, t1, b.id))
        self._deletions(deleted, bid, pieces)
        self._splits(before, pieces)
        self._trims(before, pieces)
        self._new_on_v1(new, bid)
        self._order(before, after, pieces)
        _brain.camera_lines(self, before, pieces)

    def _deletions(self, deleted: list[tuple[float, float, str]], bid: dict[str, Clip],
                   pieces: dict[str, list[Clip]]) -> None:
        if not deleted:
            return
        self.v1_moved = True
        deleted.sort()
        merged: list[list[Any]] = []
        tol = 1.5 / float(self.names.fps or 30)
        for t0, t1, owner in deleted:
            if merged and t0 - merged[-1][1] <= tol:
                merged[-1][1] = max(merged[-1][1], t1)
                merged[-1][2].add(owner)
            else:
                merged.append([t0, t1, {owner}])
        keys: set[str] = set()
        for o in set().union(*(m[2] for m in merged)):     # each owner ONCE, not once per span
            if not pieces[o]:
                keys |= self.keys_of(o)
            for p in pieces[o]:
                keys |= self._piece_keys(o, p)
        if len(merged) > 4:
            total = sum(m[1] - m[0] for m in merged)
            self.add("Video", f"Deleted {len(merged)} parts of the video ({secs(total)} in total), "
                              f"from {self.names.tc(merged[0][0])} to {self.names.tc(merged[-1][1])}", keys)
            return
        for i, (t0, t1, owners) in enumerate(merged):
            whole = [o for o in owners if not pieces[o]]
            if len(owners) == 1 and whole:
                o = whole[0]
                text = f"Deleted {self.names.clip(o)} ({self.names.span(t0, t1)})"
            elif len(owners) == 1:
                o = next(iter(owners))
                text = f"Deleted {self.names.span(t0, t1)} of the video (part of {self.names.clip(o)})"
            else:
                text = f"Deleted {self.names.span(t0, t1)} of the video ({len(owners)} clips)"
            self.add("Video", text, keys if i == 0 else ())

    def _piece_keys(self, parent: str, p: Clip) -> set[str]:
        """The keys of piece `p` that a cut or split EXPLAINS — and only those:
        its in/out (and a new piece's place); for a new piece what it
        inherited unchanged from its parent; and an edge-only field (a fade,
        an animation, a curve) only when the split demonstrably MOVED it to
        another piece or re-sliced it. A fade added in the same plan is a
        change of its own and stays for the attribute rule to say — claiming
        every edge field here is how "split and fade in" once came out as one
        line, the fade missing from the card."""
        out: set[str] = {f"clip:{p.id}.in", f"clip:{p.id}.out"}
        pre = f"clip:{p.id}."
        new = p.id != parent
        if new:
            out |= {f"clip:{p.id}", f"clip:{p.id}.start"}
        for k in self.keys_of(p.id):
            if not k.startswith(pre):
                continue
            field_ = k[len(pre):]
            if field_ in _PIECE_EDGE:
                if self._edge_moved(parent, field_, p) or self._resliced(parent, field_, p):
                    out.add(k)
                elif new and self.fa.get(k, MISSING) == self.fb.get(f"clip:{parent}.{field_}", MISSING):
                    out.add(k)
            elif new and self.fa.get(k, MISSING) == self.fb.get(f"clip:{parent}.{field_}", MISSING):
                out.add(k)
        return out

    def _edge_moved(self, parent: str, field_: str, p: Clip) -> bool:
        """`p` no longer holds its parent's `field_` and another piece of the
        parent now does (a fade-out follows the tail of a split)."""
        bv = self.fb.get(f"clip:{parent}.{field_}", MISSING)
        if bv is MISSING or bv in (0, 0.0, False):
            return False
        if self.fa.get(f"clip:{p.id}.{field_}", MISSING) not in (MISSING, 0, 0.0, False):
            return False
        return any(self.fa.get(f"clip:{q.id}.{field_}", MISSING) == bv
                   for q in self._pieces.get(parent, []) if q.id != p.id)

    def _resliced(self, parent: str, field_: str, p: Clip) -> bool:
        """A speed curve / volume envelope cut with its clip: every piece
        holds a slice of the parent's shape."""
        bv = self.fb.get(f"clip:{parent}.{field_}", MISSING)
        av = self.fa.get(f"clip:{p.id}.{field_}", MISSING)
        return isinstance(bv, dict) and isinstance(av, dict) and len(self._pieces.get(parent, [])) > 1

    def _splits(self, before: list[Clip], pieces: dict[str, list[Clip]]) -> None:
        for b in before:
            win = self._wins[b.id]
            ps = sorted(pieces[b.id], key=lambda p: win[p.id][0])
            if len(ps) < 2:
                continue
            points = [b.start + b.timeline_offset_at(win[p.id][0] - b.in_)
                      for prev, p in zip(ps, ps[1:]) if abs(win[p.id][0] - win[prev.id][1]) <= EPS]
            if not points:
                continue           # pieces with a hole between: the deletion line said it
            self.v1_moved = True
            keys: set[str] = set()
            for p in ps:
                keys |= self._piece_keys(b.id, p)
            at = " and ".join(self.names.tc(t) for t in points[:3]) + (
                f" (+{len(points) - 3} more)" if len(points) > 3 else "")
            self.add("Video", f"Split {self.names.clip(b.id)} at {at}", keys)

    def _trims(self, before: list[Clip], pieces: dict[str, list[Clip]]) -> None:
        """A kept clip whose source window GREW (nothing was lost, so no
        deletion line claimed it): say its new length."""
        for b in before:
            if len(pieces[b.id]) != 1 or pieces[b.id][0].id != b.id:
                continue
            p = pieces[b.id][0]
            if _brain.is_angle_piece(b, pieces[b.id], p):
                continue           # a whole clip swapped to another angle: the camera line says it
            ks = {f"clip:{b.id}.in", f"clip:{b.id}.out"} & self.pending
            if not ks:
                continue
            self.v1_moved = True
            self.add("Video", f"{self.names.clip(b.id)}: length {secs(b.effective_duration)} -> "
                              f"{secs(p.effective_duration)} (source {secs(b.in_)}-{secs(b.out)} -> "
                              f"{secs(p.in_)}-{secs(p.out)})", ks)

    def _new_on_v1(self, new: list[Clip], bid: dict[str, Clip]) -> None:
        for c in new:
            self.v1_moved = True
            keys = self.keys_of(c.id)
            span = self.names.span(c.start, c.start + c.effective_duration)
            if c.freeze is not None:
                src = _holder(c, bid.values())
                what = self.names.clip(src.id) if src else f"'{self.names.media(c.src)}'"
                self.add("Video", f"Added a {secs(c.freeze)} freeze frame of {what} at {span}", keys)
            elif any(b.src == c.src for b in bid.values()):
                # The clip it copies: same file AND same source window when
                # there is one (three pieces of one upload share the file).
                same = [b for b in bid.values() if b.src == c.src]
                src = next((b for b in same if abs(b.in_ - c.in_) <= EPS and abs(b.out - c.out) <= EPS),
                           None)
                if src is not None:
                    self.add("Video", f"Duplicated {self.names.clip(src.id)} (the copy plays {span})", keys)
                else:
                    self.add("Video", f"Added clip '{self.names.media(c.src)}' to the video at {span} "
                                      f"(source {secs(c.in_)}-{secs(c.out)})", keys)
            else:
                self.add("Video", f"Added clip '{self.names.media(c.src)}' to the video at {span}", keys)

    def _order(self, before: list[Clip], after: list[Clip], pieces: dict[str, list[Clip]]) -> None:
        owner = {p.id: b for b, ps in pieces.items() for p in ps}
        seq: list[str] = []
        for c in after:
            o = owner.get(c.id)
            if o is not None and (not seq or seq[-1] != o):
                seq.append(o)
        kept = [b.id for b in before if b.id in seq]
        first_seen = list(dict.fromkeys(seq))
        if first_seen == kept:
            return
        self.v1_moved = True
        order = ", ".join(str(self.names.v1_number(i)) for i in first_seen)
        keys = {f"clip:{c.id}.start" for c in after}
        self.add("Video", f"Reordered the video: clips now play in the order {order}", keys)

    # -- attributes of a clip that exists on both sides ------------------------
    def attributes(self) -> None:
        by_text: dict[tuple[str, str], list[tuple[str, set[str]]]] = {}
        ids = [c.id for t in self.after.tracks for c in t.clips]
        before_ids = {c.id for t in self.before.tracks for c in t.clips}
        for cid in ids:
            parent = cid if cid in before_ids else split_parent(cid, before_ids)
            if parent is None:
                continue
            for group, text, keys in self._clip_attrs(cid, parent):
                by_text.setdefault((group, text), []).append((cid, keys))
        v1_all = {c.id for c in _v1(self.after)}
        for (group, text), rows in by_text.items():
            subjects = [cid for cid, _ in rows]
            keys = set().union(*(k for _, k in rows))
            if len(rows) == 1:
                self.add(group, f"{self.names.clip(subjects[0])}: {text}", keys)
            elif set(subjects) == v1_all and len(v1_all) > 1:
                self.add(group, f"All {len(subjects)} clips of the video: {text}", keys)
            else:
                nums = [self.names.v1_number(s) for s in subjects]
                if all(n is not None for n in nums):
                    listed = ", ".join(str(n) for n in sorted(set(nums)))
                    self.add(group, f"Clips {listed}: {text}", keys)
                else:
                    self.add(group, f"{self._many(subjects)}: {text}", keys)

    def _many(self, subjects: list[str]) -> str:
        """"3 caption lines", "2 titles", "4 captions and titles", "3 clips"."""
        kinds = {self.names.clip(s).split(" ", 1)[0] for s in subjects}
        n = len(subjects)
        if kinds == {"Caption"}:
            return f"{n} caption lines"
        if kinds == {"Title"}:
            return f"{n} titles"
        if kinds <= {"Caption", "Title"}:
            return f"{n} captions and titles"
        return f"{n} clips ({self.names.clip(subjects[0])} and {n - 1} more)"

    def _clip_attrs(self, cid: str, parent: str) -> list[tuple[str, str, set[str]]]:
        """(group, words, keys) for each attribute of `cid` that moved — for a
        split piece, compared with the clip it came from."""
        pre = f"clip:{cid}."
        out: list[tuple[str, str, set[str]]] = []
        mine = sorted(k for k in self.keys_of(cid) if k.startswith(pre))
        if not mine:
            return out

        def b(field_: str) -> Any:
            return self.fb.get(f"clip:{parent}.{field_}", MISSING)

        def a(field_: str) -> Any:
            return self.fa.get(f"{pre}{field_}", MISSING)

        fields = [k[len(pre):] for k in mine]
        done: set[str] = set()
        clip_b = self._clip_obj(self.before, parent)
        clip_a = self._clip_obj(self.after, cid)

        def take(group: str, text: str, *fs: str) -> None:
            out.append((group, text, {f"{pre}{f}" for f in fs}))
            done.update(fs)

        if "speed" in fields and isinstance(clip_a, Clip) and isinstance(clip_b, Clip):
            track = self.names._track_of.get(cid)
            if track is not None and track.id == "v1":
                self.v1_moved = True
            take("Clips", f"speed {speed_word(b('speed'))} -> {speed_word(a('speed'))} "
                          f"({secs(clip_b.effective_duration)} -> {secs(clip_a.effective_duration)})", "speed")
        if "freeze" in fields:
            take("Clips", f"freeze {fmt_num(b('freeze'), 's')} -> {fmt_num(a('freeze'), 's')}", "freeze")
        if "reverse" in fields:
            take("Clips", "plays backwards" if a("reverse") is True else "plays forwards again", "reverse")
        if "audio.mute" in fields:
            take("Audio", "muted" if a("audio.mute") is True else "unmuted", "audio.mute")
        if "audio.gain_db" in fields:
            take("Audio", f"volume {fmt_num(b('audio.gain_db'), 'dB')} -> {fmt_num(a('audio.gain_db'), 'dB')}",
                 "audio.gain_db")
        for f, word in (("audio.fade_in", "sound fade in"), ("audio.fade_out", "sound fade out"),
                        ("video_fade_in", "fade in"), ("video_fade_out", "fade out")):
            if f in fields:
                take("Look" if f.startswith("video") else "Audio",
                     f"{word} {fmt_num(b(f) if b(f) is not MISSING else 0.0, 's')} -> "
                     f"{fmt_num(a(f) if a(f) is not MISSING else 0.0, 's')}", f)
        if "effects" in fields:
            take("Look", _effects_words(b("effects"), a("effects")), "effects")
        track = self.names._track_of.get(cid)
        if ({"in", "out"} & set(fields)) and isinstance(clip_a, Clip) and isinstance(clip_b, Clip) \
                and (track is None or track.id != "v1"):
            # A sound or overlay trimmed (music cut to the video's new end):
            # its length and where it now ends, not "out 12 -> 4".
            end = clip_a.start + clip_a.effective_duration
            take("Audio" if track is not None and track.type in ("music", "vo", "audio") else "Clips",
                 f"length {secs(clip_b.effective_duration)} -> {secs(clip_a.effective_duration)} "
                 f"(ends at {self.names.tc(end)})", *({"in", "out"} & set(fields)))
        tr = [f for f in fields if f.startswith("transform.")]
        for f in tr:
            prop = f.split(".", 1)[1]
            bv, av = b(f), a(f)
            if prop == "scale":
                text = f"zoom {_pct(bv)} -> {_pct(av)}"
            elif prop == "rotation":
                text = f"rotation {_deg(bv)} -> {_deg(av)}"
            elif prop == "opacity":
                text = f"opacity {_pct(bv)} -> {_pct(av)}"
            elif prop in ("flip_h", "flip_v"):
                way = "horizontally" if prop == "flip_h" else "vertically"
                text = f"flipped {way}" if av is True else f"no longer flipped {way}"
            elif prop in ("x", "y"):
                text = f"position {prop} {_fmt_value(bv if bv is not MISSING else 0.0)} -> {_fmt_value(av)}"
            else:
                text = f"{prop} {_fmt_value(bv)} -> {_fmt_value(av)}"
            take("Look", text, f)
        simple = {"fit": ("Look", "fit"), "blend": ("Look", "blend mode"), "anim_in": ("Look", "intro animation"),
                  "anim_out": ("Look", "outro animation"), "anim_combo": ("Look", "combo animation"),
                  "anim_dur": ("Look", "animation length"), "anim_out_dur": ("Look", "outro animation length"),
                  "audio.voice_effect": ("Audio", "voice effect"), "audio.voice_intensity": ("Audio", "voice effect strength"),
                  "audio.channels": ("Audio", "channels"), "audio.keep_pitch": ("Audio", "keep pitch"),
                  "canvas_bg": ("Look", "background"), "framing": ("Look", "framing"), "mask": ("Look", "mask"),
                  "chromakey": ("Look", "green screen"), "src": ("Clips", "media"),
                  "audio.gain_env": ("Audio", "volume keyframes")}
        for f, (group, word) in simple.items():
            if f in fields and f not in done:
                bv, av = b(f), a(f)
                if f == "fit":
                    fit = {"contain": "fit to frame", "cover": "fill frame", MISSING: "fit to frame"}
                    take(group, f"{fit.get(bv, _fmt_value(bv))} -> {fit.get(av, _fmt_value(av))}", f)
                elif f == "src":
                    take(group, f"media {self.names.media(str(bv))} -> {self.names.media(str(av))}", f)
                else:
                    take(group, f"{word} {_fmt_value(bv)} -> {_fmt_value(av)}", f)
        return out

    @staticmethod
    def _clip_obj(edl: EDL, cid: str) -> Any:
        for t in edl.tracks:
            for c in t.clips:
                if c.id == cid:
                    return c
        return None

    # -- text, captions, stickers, audio clips that come or go -----------------
    def overlays_and_audio(self) -> None:
        b_ids = {c.id: (t, c) for t in self.before.tracks for c in t.clips}
        a_ids = {c.id: (t, c) for t in self.after.tracks for c in t.clips}
        self._caption_lines(b_ids, a_ids)
        for cid, (t, c) in a_ids.items():
            if cid in b_ids or t.id == "v1" or t.type == "captions":
                continue
            if split_parent(cid, b_ids):
                continue
            keys = self.keys_of(cid)
            if not keys and self._by_clip.get(cid):
                continue                    # an earlier line already says it (the dialogue lane's pieces)
            if isinstance(c, TextClip):
                shown = c.text.upper() if getattr(c.style, "upper", None) else c.text   # as it renders
                look = title_look(c, self.after.canvas.w, self.after.canvas.h)
                self.add("Text", f"Added title '{_short(shown)}' ({self.names.span(c.start, c.end)})"
                                 + (f": {look}" if look else ""), keys)
            elif isinstance(c, Sticker):
                self.add("Stickers", f"Added sticker '{c.label or Path(c.src).stem}' "
                                     f"({self.names.span(c.start, c.end)})", keys)
            else:
                vol = f", volume {fmt_num(c.audio.gain_db, 'dB')}" if c.audio.gain_db else ""
                self.add("Audio" if t.type in ("music", "vo", "audio") else "Video",
                         f"Added {self.names.clip(cid).split(' ', 1)[0].lower()} '{self.names.media(c.src)}' "
                         f"at {self.names.tc(c.start)} ({secs(c.effective_duration)}{vol})", keys)
        for cid, (t, c) in b_ids.items():
            if cid in a_ids or t.id == "v1" or t.type == "captions":
                continue
            keys = self.keys_of(cid)
            if not keys and self._by_clip.get(cid):
                continue                    # … or that it went (the recorder's copy on the Music lane)
            self.add("Text" if isinstance(c, TextClip) else "Stickers" if isinstance(c, Sticker) else "Audio",
                     f"Removed {self.names.clip(cid)}", keys)
        self._text_edits(b_ids, a_ids)

    def _caption_lines(self, b_ids: dict, a_ids: dict) -> None:
        captions_added = [c for cid, (t, c) in a_ids.items() if cid not in b_ids and t.type == "captions"]
        captions_gone = [c for cid, (t, c) in b_ids.items() if cid not in a_ids and t.type == "captions"]
        if captions_added:
            lo = min(c.start for c in captions_added)
            hi = max(c.end for c in captions_added)
            keys = set().union(*(self.keys_of(c.id) for c in captions_added))
            n = len(captions_added)
            if captions_gone:
                keys |= set().union(*(self.keys_of(c.id) for c in captions_gone))
                self.add("Captions", f"Rebuilt the captions: {len(captions_gone)} -> {n} line{'s' if n != 1 else ''} "
                                     f"({self.names.span(lo, hi)})", keys)
                captions_gone = []
            else:
                self.add("Captions", f"Added captions: {n} line{'s' if n != 1 else ''} ({self.names.span(lo, hi)})",
                         keys)
        if captions_gone:
            keys = set().union(*(self.keys_of(c.id) for c in captions_gone))
            self.add("Captions", f"Removed {len(captions_gone)} caption line{'s' if len(captions_gone) != 1 else ''}",
                     keys)

    def _text_edits(self, b_ids: dict, a_ids: dict) -> None:
        caption_words = [cid for cid, (t, c) in a_ids.items()
                         if cid in b_ids and t.type == "captions" and f"clip:{cid}.text" in self.pending]
        if caption_words:
            self.add("Captions", f"Changed the words of {len(caption_words)} caption line"
                                 f"{'s' if len(caption_words) != 1 else ''}",
                     {f"clip:{cid}.text" for cid in caption_words})
        style: dict[str, list[str]] = {}
        for cid, (t, c) in a_ids.items():
            if cid not in b_ids or not isinstance(c, TextClip):
                continue
            pre = f"clip:{cid}."
            if f"{pre}text" in self.pending:
                self.add("Text", f"{self.names.clip(cid)}: words '{_short(b_ids[cid][1].text)}' -> "
                                 f"'{_short(c.text)}'", {f"{pre}text"})
            for k in sorted(k for k in self.keys_of(cid) if k.startswith(f"{pre}style.")):
                f = k[len(f"{pre}style."):]
                bv, av = self.fb.get(k, MISSING), self.fa.get(k, MISSING)
                word = {"color": "colour", "stroke": "outline colour", "stroke_w": "outline width",
                        "background": "box", "upper": "all capitals", "size": "size", "font": "font",
                        "align": "alignment", "shadow_on": "shadow", "letter_spacing": "letter spacing",
                        "line_spacing": "line spacing", "shadow": "shadow"}.get(f, f)
                if f in ("color", "stroke", "background"):
                    text = f"{word} {colour(bv)} -> {colour(av)}"
                else:
                    text = f"{word} {_fmt_value(bv)} -> {_fmt_value(av)}"
                if t.type == "captions":
                    style.setdefault(text, []).append(k)
                else:
                    self.add("Text", f"{self.names.clip(cid)}: {text}", {k})
        for text, keys in style.items():
            self.add("Captions", f"{len(keys)} caption line{'s' if len(keys) != 1 else ''}: {text}", keys)

    # -- timing of things that follow the main lane ----------------------------
    def retiming(self) -> None:
        v1_ids = {c.id for c in _v1(self.after)}
        shifts: list[str] = []
        others: list[tuple[str, set[str]]] = []
        for t in self.after.tracks:
            for c in t.clips:
                keys = {k for k in (f"clip:{c.id}.start", f"clip:{c.id}.end") if k in self.pending}
                if not keys:
                    continue
                if c.id in v1_ids:
                    shifts.extend(keys)
                    continue
                others.append((c.id, keys))
        if shifts:
            if self.v1_moved:
                n = len({k.split(".")[0] for k in shifts})
                self.add("Follow", f"{n} later clip{'s move' if n != 1 else ' moves'} to keep the video "
                                   "continuous", shifts)
            else:
                for k in sorted(shifts):
                    cid = k.split(":", 1)[1].split(".")[0]
                    self.add("Video", f"{self.names.clip(cid)}: moved {self.names.tc(self.fb.get(k, 0.0))} -> "
                                      f"{self.names.tc(self.fa.get(k, 0.0))}", {k})
        follow: list[set[str]] = []
        follow_ids: list[str] = []
        resized: dict[str, list[tuple[str, set[str]]]] = {}
        for cid, keys in others:
            bs, be = self.fb.get(f"clip:{cid}.start", MISSING), self.fb.get(f"clip:{cid}.end", MISSING)
            as_, ae = self.fa.get(f"clip:{cid}.start", MISSING), self.fa.get(f"clip:{cid}.end", MISSING)
            if self.v1_moved and not self._length_changed(bs, be, as_, ae):
                follow.append(keys)
                follow_ids.append(cid)
                continue
            if self.v1_moved:
                # final sweep 3 r2 (CRITICAL): a 3 s title that Apply cuts to a
                # 0.1 s stub (its footage deleted), or halves / doubles with a
                # speed change, was folded into "1 title moves with the video"
                kind = self.names.clip(cid).split(" ", 1)[0]
                if kind == "Caption":
                    resized.setdefault(kind, []).append((cid, keys))
                    continue
                self.add("Text", self._resized_words(cid, float(bs), float(be), float(as_), float(ae)), keys)
                continue
            if be is MISSING or ae is MISSING:
                text = f"moved {self.names.tc(_num(bs) or 0.0)} -> {self.names.tc(_num(as_) or 0.0)}"
            else:
                text = f"{self.names.span(float(bs), float(be))} -> {self.names.span(float(as_), float(ae))}"
            self.add("Text", f"{self.names.clip(cid)}: {text}", keys)
        if follow:
            self.add("Follow", f"{self._follow_words(follow_ids)} with the video", set().union(*follow))
        for items in resized.values():
            short = sum(1 for cid, _ in items
                        if self._len("fa", cid) < self._len("fb", cid))
            n = len(items)
            bits = []
            if short:
                bits.append(f"{short} shortened")
            if n - short:
                bits.append(f"{n - short} lengthened")
            stubs = sum(1 for cid, _ in items if self._len("fa", cid) <= _STUB_S + 1e-6)
            tail = f"; {stubs} now show{'s' if stubs == 1 else ''} for only a moment" if stubs else ""
            self.add("Follow", f"{n} caption{'s' if n != 1 else ''} change length with the video "
                               f"({', '.join(bits)}{tail})", set().union(*(k for _, k in items)))

    #: a frame's worth of slack when telling a move from a length change
    def _length_changed(self, bs: Any, be: Any, as_: Any, ae: Any) -> bool:
        if any(_num(v) is None for v in (bs, be, as_, ae)):
            return False
        frame = 1.0 / float(self.names.fps or 30.0)
        return abs((float(ae) - float(as_)) - (float(be) - float(bs))) >= frame - 1e-6

    def _len(self, side: str, cid: str) -> float:
        vals = getattr(self, side)
        a, b = _num(vals.get(f"clip:{cid}.start", MISSING)), _num(vals.get(f"clip:{cid}.end", MISSING))
        return (b - a) if a is not None and b is not None else 0.0

    def _resized_words(self, cid: str, bs: float, be: float, as_: float, ae: float) -> str:
        was, now = be - bs, ae - as_
        head = f"{self.names.clip(cid)}: {self.names.span(bs, be)} -> {self.names.span(as_, ae)}"
        if now <= _STUB_S + 1e-6:
            return (f"{head} (the video under it is removed; it now shows for only {secs(now)}, "
                    f"was {secs(was)})")
        verb = "shortened" if now < was else "lengthened"
        return f"{head} ({verb} with the video: {secs(was)} -> {secs(now)})"

    def _follow_words(self, ids: list[str]) -> str:
        """"1 sticker moves", "3 captions and 1 title move" — named from what
        actually moved (a lone sticker was called a "caption, title or sound")."""
        kinds: dict[str, int] = {}
        for cid in ids:
            word = self.names.clip(cid).split(" ", 1)[0].lower()
            word = {"clip": "clip", "audio": "sound"}.get(word, word)
            kinds[word] = kinds.get(word, 0) + 1
        plural = {"music": "music clips", "voice-over": "voice-overs"}
        parts = [f"{n} {w if n == 1 else plural.get(w, w + 's')}" for w, n in kinds.items()]
        listed = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
        return f"{listed} {'moves' if len(ids) == 1 else 'move'}"

    # -- transitions, canvas, tracks, markers -------------------------------------
    def transitions(self) -> None:
        added: dict[tuple[str, float], list[tuple[float, str]]] = {}
        removed: dict[tuple[str, str, float], list[tuple[float, str]]] = {}
        slots: dict[str, tuple[Any, Any]] = {}      # a time whose transition changed type
        lone: list[tuple[float, str]] = []
        for k in sorted(k for k in self.pending if k.startswith("trans:")):
            tid, at_s = k[len("trans:"):].rsplit("@", 1)
            at = float(at_s)
            bv, av = self.fb.get(k, MISSING), self.fa.get(k, MISSING)
            where = self.names.tc(at)
            if bv is MISSING:
                name = (_trans_name(av.get("type")), float(av.get("duration") or 0.0))
                added.setdefault(name, []).append((at, k))
            elif av is MISSING:
                removed.setdefault((tid, _trans_name(bv.get("type")), float(bv.get("duration") or 0.0)),
                                   []).append((at, k))
            elif self.v1_moved and (_trans_name(bv.get("type")), float(bv.get("duration") or 0.0)) != \
                    (_trans_name(av.get("type")), float(av.get("duration") or 0.0)):
                # The main lane moved: a transition that followed its cut may
                # have landed on ANOTHER one's old time. Take the slot apart
                # (one gone, one arrived) so the move matcher below can pair
                # each with its own seam; what stays unpaired at this time is
                # said as a retype after all.
                removed.setdefault((tid, _trans_name(bv.get("type")), float(bv.get("duration") or 0.0)),
                                   []).append((at, k))
                added.setdefault((_trans_name(av.get("type")), float(av.get("duration") or 0.0)),
                                 []).append((at, k))
                slots[k] = (bv, av)
            else:
                self.add("Transitions", f"Transition at {where}: {_trans_name(bv.get('type'))} "
                                        f"{secs(float(bv.get('duration') or 0))} -> {_trans_name(av.get('type'))} "
                                        f"{secs(float(av.get('duration') or 0))}", {k})
        # A transition whose seam moved (a cut earlier in the video) is ONE
        # change: the same look, gone from one time and back at another.
        moved: list[tuple[str, float, float, set[str]]] = []
        for (tid, name, dur), gone in removed.items():
            arrived = added.get((name, dur), [])
            here = [(a, k) for a, k in arrived if k.startswith(f"trans:{tid}@")]
            gone.sort()
            here.sort()
            for (a0, k0), (a1, k1) in zip(gone, here):
                moved.append((name, a0, a1, {k0, k1}))
                arrived.remove((a1, k1))
            for a0, k0 in gone[len(here):]:
                if k0 in slots:
                    lone.append((a0, k0))     # said below: a retype, or a removal
                    continue
                self.add("Transitions", f"Removed transition {name} at {self.names.tc(a0)}", {k0})
        for a0, k0 in lone:
            bv, av = slots[k0]
            arrived = added.get((_trans_name(av.get("type")), float(av.get("duration") or 0.0)), [])
            if (a0, k0) in arrived:          # nothing moved in or out: a plain retype
                arrived.remove((a0, k0))
                self.add("Transitions", f"Transition at {self.names.tc(a0)}: {_trans_name(bv.get('type'))} "
                                        f"{secs(float(bv.get('duration') or 0))} -> {_trans_name(av.get('type'))} "
                                        f"{secs(float(av.get('duration') or 0))}", {k0})
            else:
                self.add("Transitions", f"Removed transition {_trans_name(bv.get('type'))} at {self.names.tc(a0)}",
                         {k0})
        if moved and self.v1_moved:
            n = len(moved)
            what = "transitions move with their cuts" if n != 1 else "transition moves with its cut"
            self.add("Follow", f"{n} {what}", set().union(*(m[3] for m in moved)))
        else:
            for name, a0, a1, keys in moved:
                self.add("Transitions", f"Transition {name} moved {self.names.tc(a0)} -> {self.names.tc(a1)}", keys)
        for (name, dur), rows in added.items():
            if not rows:
                continue
            rows.sort()
            ats = [self.names.tc(a) for a, _ in rows]
            if len(rows) == 1:
                text = f"Added transition {name} {fmt_num(dur, 's')} at {ats[0]}"
            else:
                listed = ", ".join(ats[:4]) + (f" and {len(ats) - 4} more" if len(ats) > 4 else "")
                text = f"Added transition {name} {fmt_num(dur, 's')} at {len(rows)} cuts: {listed}"
            self.add("Transitions", text, {k for _, k in rows})

    def canvas(self) -> None:
        w0, h0 = self.fb.get("canvas.w"), self.fb.get("canvas.h")
        w1, h1 = self.fa.get("canvas.w"), self.fa.get("canvas.h")
        if {"canvas.w", "canvas.h"} & self.pending:
            self.add("Canvas", f"Canvas {w0}x{h0} ({_ratio(w0, h0)}) -> {w1}x{h1} ({_ratio(w1, h1)})",
                     {"canvas.w", "canvas.h"})
        words = {"fps": ("frame rate", "fps"), "loudness_lufs": ("loudness target", "LUFS"),
                 "bitrate_kbps": ("export bitrate", "kbps"), "bg": ("background colour", "")}
        for k in sorted(k for k in self.pending if k.startswith("canvas.")):
            f = k.split(".", 1)[1]
            word, unit = words.get(f, (f, ""))
            bv, av = self.fb.get(k, MISSING), self.fa.get(k, MISSING)
            if f == "bg":
                self.add("Canvas", f"Canvas {word} {colour(bv)} -> {colour(av)}", {k})
            elif f == "loudness_lufs":
                # the UI's name for it (final sweep 3: "Canvas loudness target")
                self.add("Canvas", f"Export loudness target {fmt_num(bv, unit)} -> {fmt_num(av, unit)}", {k})
            else:
                self.add("Canvas", f"Canvas {word} {fmt_num(bv, unit)} -> {fmt_num(av, unit)}", {k})

    def tracks(self) -> None:
        for k in sorted(k for k in self.pending if k.startswith("track:")):
            if k not in self.pending:
                continue
            tid, _, f = k[len("track:"):].partition(".")
            name = self.names.track(tid)
            bv, av = self.fb.get(k, MISSING), self.fa.get(k, MISSING)
            if not f:
                keys = {x for x in self.pending if x == k or x.startswith(f"{k}.")}
                host = self._line_for_track(tid) if bv is MISSING else None
                if host is not None:
                    # A lane made to hold the new title / sticker / sound is
                    # plumbing, not an edit of its own: fold it into that line.
                    self.claim_silently(host, keys)
                    continue
                self.add("Tracks", f"Added the {name}" if bv is MISSING else f"Removed the {name}", keys)
                continue
            if f == "muted":
                self.add("Tracks", f"{name}: {'muted' if av is True else 'unmuted'}", {k})
            elif f == "solo":
                self.add("Tracks", f"{name}: {'solo on' if av is True else 'solo off'}", {k})
            elif f == "locked":
                self.add("Tracks", f"{name}: {'locked' if av is True else 'unlocked'}", {k})
            elif f.startswith("duck"):
                keys = {x for x in self.pending if x.startswith(f"track:{tid}.duck")}
                to = self.fa.get(f"track:{tid}.duck.to_db", MISSING)
                was = self.fb.get(f"track:{tid}.duck.to_db", MISSING)
                if to is MISSING:
                    self.add("Audio", f"{name}: no longer ducks under the voice", keys)
                elif was is MISSING:
                    self.add("Audio", f"{name}: ducks to {fmt_num(to, 'dB')} under the voice", keys)
                else:
                    self.add("Audio", f"{name}: ducking {fmt_num(self.fb.get(f'track:{tid}.duck.to_db'), 'dB')} -> "
                                      f"{fmt_num(to, 'dB')}", keys)
            elif f.startswith("config"):
                sub = f.split(".", 1)[1] if "." in f else f
                word = _CAPTION_WORDS.get(sub, sub.replace("look.", "").replace("_", " "))
                show = colour if sub.endswith(("color", "stroke", "background")) else _fmt_value
                text = (f"{word} {'default' if bv is MISSING else show(bv)} -> "
                        f"{'default' if av is MISSING else show(av)}")
                self.add("Captions", f"Captions {text}", {k})
            else:
                self.add("Tracks", f"{name}: {f} {_fmt_value(bv)} -> {_fmt_value(av)}", {k})

    def _line_for_track(self, tid: str) -> int | None:
        """The index of the line that added a clip on the new track `tid`."""
        t = self.after.get_track(tid)
        ids = {f"clip:{c.id}" for c in (t.clips if t else [])}
        if not ids:
            return None
        return next((i for i, c in enumerate(self.changes) if c.keys & ids), None)

    def markers(self) -> None:
        for k in sorted(k for k in self.pending if k.startswith("marker:") and "." not in k):
            keys = {x for x in self.pending if x == k or x.startswith(f"{k}.")}
            side = self.fa if self.fa.get(k, MISSING) is not MISSING else self.fb
            label = side.get(f"{k}.label") or ""
            at = self.names.tc(float(side.get(f"{k}.time") or 0.0))
            verb = "Added" if self.fb.get(k, MISSING) is MISSING else "Removed"
            named = f"'{_short(str(label))}' " if label else ""
            self.add("Markers", f"{verb} marker {named}at {at}", keys)

    # -- the net under all of it ------------------------------------------------
    def generic(self) -> None:
        """Every key no rule claimed gets its own line — the guarantee."""
        for k in sorted(self.pending):
            bv, av = self.fb.get(k, MISSING), self.fa.get(k, MISSING)
            if k.startswith("clip:"):
                cid, _, f = k[len("clip:"):].partition(".")
                subject = self.names.clip(cid)
                word = (f or "track").replace("_", " ").replace(".", " ")
                if not f:
                    text = f"{subject}: {'added' if bv is MISSING else 'removed' if av is MISSING else 'moved to another track'}"
                else:
                    text = f"{subject}: {word} {_fmt_value(bv)} -> {_fmt_value(av)}"
                self.add("Clips", text, {k})
            elif k.startswith("marker:"):
                mid, _, f = k[len("marker:"):].partition(".")
                t = self.fa.get(f"marker:{mid}.time", self.fb.get(f"marker:{mid}.time", 0.0))
                self.add("Markers", f"Marker at {self.names.tc(float(_num(t) or 0.0))}: "
                                    f"{f or 'marker'} {_fmt_value(bv)} -> {_fmt_value(av)}", {k})
            elif k.startswith("edl."):
                f = k[4:].replace("_", " ")
                self.add("Project", f"Project {f} {_fmt_value(bv)} -> {_fmt_value(av)}", {k})
            else:
                self.add("Other", f"{k} {_fmt_value(bv)} -> {_fmt_value(av)}", {k})

    def run(self) -> list[Change]:
        self.canvas()
        self.main_lane()
        _brain.dialogue_lane_lines(self)
        self.overlays_and_audio()
        self.attributes()
        self.transitions()
        self.retiming()
        self.tracks()
        self.markers()
        self.generic()
        return sorted(self.changes, key=lambda c: GROUP_ORDER.index(c.group) if c.group in GROUP_ORDER else 99)


#: Fields a cut or a split legitimately moves between the pieces of a clip.
_PIECE_EDGE = ("video_fade_in", "video_fade_out", "audio.fade_in", "audio.fade_out", "speed", "audio.gain_env",
               "anim_in", "anim_out", "anim_combo", "anim_dur", "anim_out_dur")


_ANIM_WORDS = {"pop": "pops", "fade": "fades", "slide_up": "slides up", "slide_down": "slides down"}


def title_look(c: TextClip, w: int, h: int) -> str:
    """How a NEW title will look, in words: place, size, colours, box, font
    and animation (final sweep 3 r2, HIGH — the line said only the words and
    the span, so "a small title at the bottom" planned big at the top, a
    "black box" as black text, and nothing on the card showed it)."""
    tx, st = c.transform, c.style
    bits: list[str] = []
    if w and h and tx is not None:
        fy, fx = float(tx.y) / float(h), float(tx.x) / float(w)
        row = ("top" if fy < 0.2 else "upper third" if fy < 0.4 else "middle" if fy <= 0.6
               else "lower third" if fy < 0.7 else "bottom")
        col = "left" if fx < 0.36 else "right" if fx > 0.64 else ""
        bits.append(f"{row} {col}".strip() if col else (f"{row} centre" if row != "middle" else "middle of the frame"))
    if st is not None:
        bits.append(f"size {fmt_num(st.size)}")
        text = f"{colour(st.color)} text"
        if float(st.stroke_w or 0) > 0:
            text += f", {colour(st.stroke)} outline"
        else:
            text += ", no outline"
        if st.background:
            text += f", on a {colour(st.background)} box"
        bits.append(text)
        if st.font:
            bits.append(f"font {Path(str(st.font)).stem.split('-')[0]}")
    anim = [f"{_ANIM_WORDS.get(str(c.anim_in), str(c.anim_in))} in"] if c.anim_in else []
    if c.anim_out:
        anim.append(f"{_ANIM_WORDS.get(str(c.anim_out), str(c.anim_out))} out")
    if anim:
        bits.append(" and ".join(anim))
    return "; ".join(bits)


def _holder(c: Clip, clips: Iterable[Clip]) -> Clip | None:
    """The before-clip whose SOURCE window holds `c`'s frame: the pieces of a
    split upload share one file, so the file alone named Clip 1 for a freeze
    of Clip 3. Half-open first (a frame on a seam belongs to the clip that
    starts there), then closed (the last frame), then the file alone."""
    same = [b for b in clips if b.src == c.src]
    for inside in (lambda b: b.in_ - EPS <= c.in_ < b.out - EPS,
                   lambda b: b.in_ - EPS <= c.in_ <= b.out + EPS):
        hit = [b for b in same if inside(b)]
        if hit:
            return min(hit, key=lambda b: abs(b.in_ - c.in_))
    return same[0] if same else None


def _subtract(span: tuple[float, float], parts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    lo, hi = span
    out: list[tuple[float, float]] = []
    cur = lo
    for a, b in sorted(parts):
        if b <= cur + EPS:
            continue
        if a > cur + EPS:
            out.append((cur, min(a, hi)))
        cur = max(cur, b)
        if cur >= hi - EPS:
            break
    if cur < hi - EPS:
        out.append((cur, hi))
    return [(a, b) for a, b in out if b - a > EPS]


