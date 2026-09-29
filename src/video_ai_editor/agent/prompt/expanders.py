"""Per-recipe expanders — the recipe table's right-hand column (spec §2.4).

Split out of `recipes.py` (which keeps the frozen contract: cards, slot
normalisation and the expansion types) so each file stays readable; every
name here is still reachable as `recipes.<name>` through a lazy module
`__getattr__`, because B's brains, X's executor and the tests import the
recipe surface from one place.

An expander is a pure function `(Intent, TimelineFacts, Context) -> Expansion`.
It never sees the store, never touches disk, and says in `notes` whenever it
deviates from the literal request (skipped music because music exists, small
model because large-v3 is not on disk, …) — the reply is built from those
notes so the run log is honest before anything executes.

Layout:
  1. per-recipe expanders (stage order)    3. `expand_auto_edit` / `audit_expansion`
  2. `EXPANDERS` table
`costs.py` (step estimates) and `heuristics.py` (beat splits, hook text) are
re-exported here so `recipes.__getattr__` finds every name in one place.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from . import grammar as G
from . import slots as S
from .facts import TimelineFacts, VOICE_IDS
from .langs import base_lang, is_latin_hindi, needs_translation
from .presets import bed_for_mood, edit_templates, transition_entry, transition_looks
from .recipes import (_PLATFORMS, _RATIOS, FILLERS_STRICT, RECIPE_SLOTS, Context, Expansion, Intent, ask,
                      download, normalize_slots, pc, placeholder, step)
from .live import MIN_TRANSITION_NEIGHBOUR_S, seams_from_boundaries, smpte
from . import clip_expanders as CX
from . import name_expanders as NX
from . import canvas_expanders as KX
from . import voice_expanders as VX
from . import anim_expanders as AX
from ...edl.speed_presets import PRESET_BY_ID as _SPEED_PRESET_BY_ID, PRESETS as _SPEED_PRESETS
from .costs import DEFAULT_STEP_COST, RECIPE_COST, estimate_seconds, step_cost   # noqa: F401 — re-exported
from .heuristics import (_CANNED_HOOK, MAX_BEAT_SPLITS, MIN_SHOT_S, PULSE_RISE_S, PULSE_SCALE,  # noqa: F401
                         WORD_EDGE_TOLERANCE_S, beat_split_times, heuristic_hook)
from .schema import (ARG_REF, FIT_BEST_PREFIX, HOOK_SENTINEL, SEAM_SENTINEL, STAGE_AUDIO, STAGE_AUDIT, STAGE_CAPTIONS, STAGE_CUTS, STAGE_EXPORT,
                     STAGE_LOOK, STAGE_MUSIC, STAGE_PREREQ, STAGE_REFRAME, STAGE_STRUCTURE, STAGE_TEXT,
                     STAGE_TRANSITIONS, DownloadNeeded, NeedsInput, Step)


def _platform_ratio(platform: str | None) -> str | None:
    return S.PLATFORM_RATIO.get(platform or "")


def _x_transcribe(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    model = it.get("model") or "small"
    downloads: tuple[DownloadNeeded, ...] = ()
    notes: tuple[str, ...] = ()
    if not f.is_cached(f"whisper:{model}"):
        if ctx.allow_downloads:
            downloads = (download(f"whisper:{model}", "transcribe"),)
        else:
            cached = f.whisper_cached_best()
            notes = (f"transcribing with the {cached} model — {model} is not downloaded",)
            model = cached
    return Expansion(
        steps=(step("transcribe", STAGE_PREREQ, "the edit reads the transcript", model=model),),
        postconditions=(pc("transcript_present", "a transcript exists"),),
        downloads=downloads, notes=notes)


def _caption_model(f: TimelineFacts, it: Intent, ctx: Context) -> tuple[str, tuple[DownloadNeeded, ...], tuple[str, ...]]:
    """§2.4: large-v3 if cached, else turbo if cached, else small + note;
    `model_upgrade` asks for large-v3 (a download question when missing)."""
    if it.get("model_upgrade") and not f.is_cached("whisper:large-v3"):
        if ctx.allow_downloads:
            return "large-v3", (download("whisper:large-v3", "auto_caption"),), ()
        return f.whisper_cached_best(), (), ("accurate captions need large-v3 (3.1 GB); using the cached model",)
    best = f.whisper_cached_best()
    notes = () if best != "small" or f.is_cached("whisper:large-v3") else (
        "captions from the small model — say 'accurate captions' to download large-v3 (3.1 GB)",)
    downloads = () if f.is_cached(f"whisper:{best}") else (download(f"whisper:{best}", "auto_caption"),)
    return best, downloads, notes


def _hinglish_captions(it: Intent, f: TimelineFacts, ctx: Context, style: str, position: str,
                       pcs: list, notes: list[str]) -> Expansion | None:
    """Hinglish captions by TRANSLITERATION (QA-043): lay the captions from the
    persisted transcript, then `translate_captions(target_lang=hinglish)`,
    which romanises Hindi with the bundled `ai.romanize` — no model, no
    network, seconds instead of a large-v3 re-transcription. Only when the
    transcript is known NOT to be Hindi does it take MADLAD (asked for like any
    download); an unknown language (no transcript yet) is resolved by the
    executor from the live transcript, and the run-time guard refuses the step
    rather than fetch a model nobody agreed to. Never Devanagari: with no
    translation model and non-Hindi speech the captions stay in the (Latin)
    spoken language and the language check says so. None → not this path."""
    if it.get("model_upgrade"):
        return None
    source = f.language if f.has_transcript else None
    need = needs_translation("hinglish", source)
    downloads: tuple[DownloadNeeded, ...] = ()
    if need and not f.is_cached("madlad"):
        if not ctx.allow_downloads:
            notes.append(f"captions stay in {source} — Hinglish from {source} speech needs the MADLAD "
                         "translation model (3 GB), which is not downloaded")
            return Expansion(steps=(step("add_caption_track", STAGE_CAPTIONS, "captions from the persisted transcript",
                                         style=style, position=position),),
                             postconditions=tuple(pcs), notes=tuple(notes))
        downloads = (download("madlad", "translate_captions"),)
    if need is False:
        notes.append("Hinglish by transliteration of the Hindi transcript (no translation model)")
    tr_args: dict[str, Any] = {"target_lang": "hinglish"}
    if source:
        tr_args["source_lang"] = source
    return Expansion(
        steps=(step("add_caption_track", STAGE_CAPTIONS, "captions from the persisted transcript (no model)",
                    style=style, position=position),
               step("translate_captions", STAGE_CAPTIONS,
                    "Hindi → Latin script (romanise)" if need is False else "captions → Hinglish", **tr_args)),
        postconditions=(*pcs, pc("captions_language", "captions are in the requested language", target="hinglish")),
        downloads=downloads, notes=tuple(notes),
        prerequisites=(Intent("transcribe"),) if not f.has_transcript else ())


def _x_caption_look(look: dict[str, Any], f: TimelineFacts) -> Expansion:
    """A look change on the EXISTING captions (Final QA): one
    `set_caption_style` step, measured on the stored look and every cue —
    never a re-lay (that replaced every cue, kept them white, dropped any
    hand edits, and still reported "4/4 checks held")."""
    if not f.has_captions:
        return Expansion(notes=("There are no captions yet — say 'add captions' first, then change their look.",))
    args: dict[str, Any] = {k: v for k, v in look.items() if not k.startswith("_")}
    if "_grow" in look:
        now = f.caption_size or 96.0
        args["size"] = float(round(min(400.0, max(24.0, now * float(look["_grow"])))))
    if args.get("background") == "":
        args["background"] = None
    said = []
    if "color" in args:
        said.append(f"colour {args['color']}")
    if "size" in args:
        said.append(f"size {args['size']:g} px")
    if "upper" in args:
        said.append("ALL CAPS" if args["upper"] else "normal case")
    if "stroke_w" in args:
        said.append("no outline" if not args["stroke_w"] else "an outline")
    if "background" in args:
        said.append("a box behind them" if args["background"] else "no box")
    if "position" in args:
        said.append(f"at the {args['position']}")
    check = {k: v for k, v in args.items() if k in ("color", "size", "upper", "stroke_w", "position")}
    if "background" in args and args["background"]:
        check["background"] = args["background"]
    bold_note = ("the captions already use the boldest weight (Inter Black) — pick another font in the "
                 "Inspector (Captions)") if look.get("_bold") else None
    if not args:
        return Expansion(notes=(bold_note or CAPTIONS_KEPT_REPLY,))
    return Expansion(
        steps=(step("set_caption_style", STAGE_CAPTIONS, "restyle the captions: " + ", ".join(said), **args),),
        postconditions=(pc("caption_look", "the captions have the requested look", **check),),
        notes=tuple(["captions: " + ", ".join(said)] + ([bold_note] if bold_note else [])))


#: Captions exist and the clause names nothing the Prompt bar can change on
#: them (Final QA r2: "make the captions bold" re-laid every cue as ig_chunky).
CAPTIONS_KEPT_REPLY = ("The captions are already there. The Prompt bar can change their colour, size, case, "
                       "outline, box or position — for a bolder font use the Inspector (Captions). To lay them "
                       "again, say 'redo the captions'.")


def _x_captions(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    look = it.get("_look") or {}
    if look and not it.get("_new") and not it.get("target"):
        return _x_caption_look(look, f)
    x = _x_captions_add(it, f, ctx)
    if look and x.steps:
        # final sweep 4: "auto captions in yellow" laid plain white captions
        # — the look rides on the same plan as its own step
        style = _x_caption_look(look, f.with_(has_captions=True))
        from dataclasses import replace as _r
        x = _r(x, steps=x.steps + style.steps, postconditions=x.postconditions + style.postconditions,
               notes=x.notes + style.notes)
    return x


def _x_captions_add(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    if (f.has_captions and not it.get("_new") and not it.get("target") and not it.get("model_upgrade")
            and not it.get("style")):
        return Expansion(notes=(CAPTIONS_KEPT_REPLY,))
    # A re-lay keeps the captions' own style unless a style is named.
    style = it.get("style") or (f.caption_style if f.has_captions else None) or "ig_chunky"
    position = it.get("position") or "bottom"
    target = it.get("target")
    # Only an explicit request packs cues by characters; otherwise the style
    # decides (ig_chunky = short phrases, QA-071 — a fixed 42 here put up to
    # 84 characters on every TikTok caption).
    max_chars = it.get("max_chars")
    max_chars = None if max_chars is None else min(60, max(16, int(max_chars)))
    notes: list[str] = []
    downloads: list[DownloadNeeded] = []
    pcs = [pc("captions_cover", "captions cover the speech", min_ratio=0.9),
           pc("captions_nonempty", "captions were laid"),
           pc("captions_within_extent", "no caption runs past the video"),
           pc("captions_style", "captions use the requested style", style=style)]
    if f.has_captions:
        notes.append("replacing the existing captions")
    if target == "hinglish":
        x = _hinglish_captions(it, f, ctx, style, position, pcs, notes)
        if x is not None:
            return x
    if (target and not it.get("model_upgrade") and f.has_transcript
            and base_lang(f.language) == base_lang(target) and base_lang(target) is not None
            and target != "hinglish" and not is_latin_hindi(f.language)):
        # The transcript on disk is already in the requested language: the
        # captions come from it, and the language check still measures them.
        return Expansion(
            steps=(step("add_caption_track", STAGE_CAPTIONS, "captions from the persisted transcript (no model)",
                        style=style, position=position),),
            postconditions=(*pcs, pc("captions_language", "captions are in the requested language", target=target)),
            notes=tuple(notes))
    spoken = f.spoken_language or f.language
    translation_needed = needs_translation(target, spoken) is not False
    if translation_needed and not f.is_cached("madlad"):
        if ctx.allow_downloads:
            downloads.append(download("madlad", "auto_caption"))
        else:
            notes.append(f"captions stay in the spoken language — the {target} translation model "
                         "(3 GB) is not downloaded")
            target = None
    if target or it.get("model_upgrade"):
        model, dl, mnotes = _caption_model(f, it, ctx)
        downloads.extend(dl)
        notes.extend(mnotes)
        pcs.append(pc("captions_language", "captions are in the requested language", target=target)
                   if target else pc("transcript_present", "a transcript exists"))
        # The spoken language, when known, spares auto_caption its detection
        # pass and tells the executor's guard that hi → hinglish needs no model.
        spoken_base = base_lang(spoken)
        language = (spoken_base if target and spoken_base and len(spoken_base) <= 3
                    and needs_translation(target, spoken) is False else None)
        return Expansion(
            steps=(step("auto_caption", STAGE_PREREQ if not f.has_transcript else STAGE_CAPTIONS,
                        "language change or model upgrade needs a fresh transcription",
                        style=style, position=position, target=target, max_chars=max_chars, model=model,
                        language=language),),
            postconditions=tuple(pcs), downloads=tuple(downloads), notes=tuple(notes))
    if f.speech_sources > 1:
        # Final sweep 3: a tone-only product shot, then a talking clip — the
        # transcript path read the FIRST main-track clip alone and laid 0
        # captions. auto_caption hears every clip that speaks and the
        # voice-over (caption_sources), as the Captions panel does.
        model, dl, mnotes = _caption_model(f, it, ctx)
        return Expansion(
            steps=(step("auto_caption", STAGE_PREREQ if not f.has_transcript else STAGE_CAPTIONS,
                        "captions from every clip that speaks and the voice-over",
                        style=style, position=position, max_chars=max_chars, model=model),),
            postconditions=tuple(pcs), downloads=tuple(dl), notes=tuple(notes) + tuple(mnotes))
    return Expansion(
        steps=(step("add_caption_track", STAGE_CAPTIONS, "captions from the persisted transcript (no model)",
                    style=style, position=position),),
        postconditions=tuple(pcs), notes=tuple(notes),
        prerequisites=(Intent("transcribe"),) if not f.has_transcript else ())


def _x_translate(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    target = it.get("target_lang")
    questions: tuple[NeedsInput, ...] = ()
    downloads: list[DownloadNeeded] = []
    notes: list[str] = []
    if not target:
        questions = (ask("target_lang", "Which language for the captions? Reply hi, en, hinglish or es.",
                         options=[("hi", "Hindi"), ("en", "English"), ("hinglish", "Hinglish"), ("es", "Spanish")]),)
        target_arg: Any = placeholder("target_lang")
    else:
        target_arg = target
        # The captions on the timeline are in the transcript's language:
        # hi → hinglish is transliteration (bundled, no model — QA-043).
        source = f.language if f.has_transcript else None
        need = needs_translation(target, source)
        if need is False and target != "hinglish" and not is_latin_hindi(source):
            return Expansion(notes=(f"the captions are already in {target} — nothing to translate",))
        if need is not False and not (target == "hinglish" and need is None) and not f.is_cached("madlad"):
            if ctx.allow_downloads:
                downloads.append(download("madlad", "translate_captions"))
            else:
                notes.append("translation skipped — the MADLAD model (3 GB) is not downloaded")
                return Expansion(notes=tuple(notes))
    tr_args: dict[str, Any] = {"target_lang": target_arg}
    if target == "hinglish" and f.has_transcript and f.language:
        tr_args["source_lang"] = f.language
    prereq = () if f.has_captions or "captions" in ctx.recipes else (Intent("captions"),)
    return Expansion(
        steps=(step("translate_captions", STAGE_CAPTIONS, "translate the caption track", **tr_args),),
        postconditions=(pc("captions_language", "captions are in the requested language", target=target),),
        questions=questions, downloads=tuple(downloads), notes=tuple(notes), prerequisites=prereq)


def _x_remove_silences(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    pcs = [pc("speech_preserved", "no kept word was cut"),
           pc("silence_total_leq", "no long pauses remain", max_total_s=1.0)]
    if f.silence_seconds >= 1.0:
        pcs.insert(0, pc("duration_shrank", "the video got shorter", min_ratio=0.02))
    return Expansion(
        steps=(step("remove_silences", STAGE_CUTS, "cut the silent pauses on v1", track="v1",
                    threshold_db=float(it.get("threshold_db", -30)), min_dur=float(it.get("min_dur", 0.5)),
                    keep_pad=float(it.get("keep_pad", 0.1)),
                    # final sweep 3 r2: a B-roll / product shot with no sound
                    # of its own is not a pause — it was deleted whole
                    keep_silent_clips=True),),
        postconditions=tuple(pcs))


def _x_remove_fillers(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    extra = tuple(w for w in (it.get("words") or ()) if w)
    words = list(dict.fromkeys((*FILLERS_STRICT, *extra)))
    pcs = [pc("fillers_remaining_leq", "filler words are gone", words=words, max=0),
           pc("speech_preserved", "no kept word was cut")]
    if f.filler_count > 0:
        pcs.insert(1, pc("duration_shrank", "the video got shorter", min_seconds=0.1))
    return Expansion(
        steps=(step("remove_fillers", STAGE_CUTS, "cut the filler words out of v1",
                    words=words, pad=0.05, track="v1"),),
        postconditions=tuple(pcs), prerequisites=(Intent("transcribe"),) if not f.has_transcript else ())


def _x_tighten(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    a = _x_remove_silences(Intent("remove_silences"), f, ctx)
    b = _x_remove_fillers(Intent("remove_fillers", {"words": it.get("words") or ()}), f, ctx)
    return Expansion(steps=a.steps + b.steps, postconditions=a.postconditions + b.postconditions,
                     prerequisites=b.prerequisites)


def _x_shorts(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    platform = it.get("platform")
    count = int(it.get("count") or 3)
    count = min(10, max(1, count))
    max_dur = it.get("max_dur")
    if max_dur is None:
        max_dur = S.PLATFORM_SHORT_MAX_S.get(platform or "", 60.0)
    max_dur = float(min(180.0, max(5.0, max_dur)))
    min_dur = float(it.get("min_dur") or min(12.0, max_dur / 2))
    min_dur = min(min_dur, max_dur)
    finish = it.get("finish")
    if finish is None:
        finish = platform is not None
    pcs = [pc("shorts_created", "the shorts were created", count=count, max_dur=max_dur, min_dur=min_dur)]
    if finish:
        pcs.append(pc("shorts_finished", "each short has captions, a hook and a 9:16 canvas"))
    return Expansion(
        steps=(step("make_shorts", STAGE_STRUCTURE, "pick the highlights and save each as its own session",
                    target_count=count, max_dur=max_dur, min_dur=min_dur, save_as_sessions=True),),
        postconditions=tuple(pcs),
        notes=(f"{count} shorts ≤ {max_dur:g}s" + (", each finished vertical with captions and a hook" if finish else ""),))


def _x_reframe(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    ratio = it.get("ratio") or _platform_ratio(it.get("platform"))
    if ratio is None:
        if f.aspect == "9:16":
            # §2.7: source already vertical and no platform named → ask.
            q = ask("ratio", "Which aspect ratio? Reply 9:16, 16:9, 1:1 or 4:5.",
                    options=[(r, r) for r in _RATIOS])
            return Expansion(
                steps=(step("auto_reframe", STAGE_REFRAME, "reframe to the chosen aspect",
                            ratio=placeholder("ratio"), subject_track="motion_track" in f.tools_available),
                       step("set_clip_fit", STAGE_REFRAME, "fill the frame — no letterbox", clip_id="$v1_all", fit="cover")),
                postconditions=(pc("canvas_aspect", "the canvas has the requested aspect"),
                                pc("reframe_effective", "the reframe changed the picture"),
                                pc("no_letterbox", "no black bars")),
                questions=(q,))
        ratio = "9:16"
    if f.aspect == ratio:
        return Expansion(notes=(f"already {ratio} — no reframe needed",))
    subject = it.get("subject_track")
    if subject is None:
        # cv2 gates BOTH motion_track and the tracked reframe; a centre canvas
        # change + cover works without it, so the recipe never gates itself.
        subject = "motion_track" in f.tools_available
    return Expansion(
        steps=(step("auto_reframe", STAGE_REFRAME, f"reframe the canvas to {ratio}", ratio=ratio, subject_track=bool(subject)),
               step("set_clip_fit", STAGE_REFRAME, "fill the frame — auto_reframe may skip a clip and Clip.fit defaults to contain",
                    clip_id="$v1_all", fit="cover")),
        postconditions=(pc("canvas_aspect", "the canvas has the requested aspect", ratio=ratio),
                        pc("reframe_effective", "the reframe changed the picture"),
                        pc("no_letterbox", "no black bars"),
                        pc("overlays_inside_safe_zone", "text stays clear of the platform UI", ratio=ratio)))


def _x_music(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    if f.has_music and not it.get("_replace") and "remove_music" not in ctx.recipes:
        # QA-018: the everyday requests about a bed that is already there
        # are level / fade / mute / fit — offer those, not only a new track.
        return Expansion(notes=("music is already on the timeline — say 'turn the music down', "
                                "'fade the music out', 'mute the music', 'end the music with the video' "
                                "or 'another track' to replace it",))
    # "replace the music": the OLD bed goes first, else the new one is laid on
    # top of it and both play at once (two beds mixed at 0 s — measured on a
    # session that already carried upbeat_120bpm: `music_present clips: 2`).
    replacing = bool(f.has_music and it.get("_replace"))
    pre_steps: tuple[Step, ...] = ()
    if replacing:
        if not f.music_clip_ids:
            return Expansion(notes=("cannot replace the music — the current bed's clips are not known; "
                                    "remove it on the timeline first",))
        pre_steps = (step("bulk_delete", STAGE_MUSIC, "remove the current music bed before laying the new one",
                          clip_ids=list(f.music_clip_ids)),)
    volume_db = float(it.get("volume_db", -14.0))
    volume_db = min(0.0, max(-40.0, volume_db))
    duck = it.get("duck")
    # "add music without ducking" / "... but don't duck it" (QA-031).
    duck = (False if "duck" in ctx.exclusions else True) if duck is None else bool(duck)
    mood = it.get("mood")
    src_slot = it.get("src")
    questions: tuple[NeedsInput, ...] = ()
    notes: list[str] = []
    src: Any = None
    if src_slot and src_slot in f.allowed_paths:
        src = src_slot
    elif src_slot and any(p.endswith("/" + src_slot) or p.endswith("\\" + src_slot) for p in f.allowed_paths):
        src = next(p for p in f.allowed_paths if p.endswith("/" + src_slot) or p.endswith("\\" + src_slot))
    elif len(f.uploads_audio) == 1 and not mood:
        src = f.uploads_audio[0]
        notes.append(f"using your upload {src.replace(chr(92), '/').split('/')[-1]}")
    else:
        bed = bed_for_mood(mood)
        if bed is not None and str(bed.path) in f.allowed_paths:
            src = str(bed.path)
            notes.append(f"{bed.mood} bed at {bed.bpm:g} BPM, {volume_db:g} dB, ducked" if duck
                         else f"{bed.mood} bed at {bed.bpm:g} BPM")
        elif len(f.uploads_audio) == 1:
            src = f.uploads_audio[0]
        elif f.uploads_audio:
            questions = (ask("music_src", "Which track? Reply with the file name.", kind="choice",
                             options=[(p, p.replace(chr(92), "/").split("/")[-1]) for p in f.uploads_audio[:12]]),)
            src = placeholder("music_src")
        else:
            questions = (ask("music_src", "Which music? Upload an audio file first, then reply with its name.",
                             kind="path"),)
            src = placeholder("music_src")
            notes.append("no music beds are installed and nothing is uploaded")
    present_args: dict[str, Any] = {"ducked": duck}
    if replacing:
        present_args["count"] = 1            # exactly ONE bed after a replace
        notes.append("replacing the current music")
    return Expansion(
        steps=pre_steps + (step("add_music", STAGE_MUSIC, "lay the bed under the whole video, ducked under speech",
                                src=src, start=0.0, volume_db=volume_db, duck=duck, loop=True),),
        postconditions=(pc("music_present", "music is on the timeline", **present_args),
                        pc("music_covers", "music runs under the whole video", min_ratio=0.95),
                        pc("music_within_video_extent", "music does not outlast the video")),
        questions=questions, notes=tuple(notes))


def _x_duck(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    if it.get("_programme"):
        return Expansion(notes=("Ducking dips the music under speech — the clips' own sound cannot duck. To lower "
                                "it under the voiceover, say 'lower the clip audio by 6 dB'.",))
    if it.get("enabled") is False:
        # QA-031: "turn off ducking" turns it OFF, and the check measures OFF.
        if not f.has_music and "music" not in ctx.recipes:
            return Expansion(notes=("there is no music on the timeline — nothing to stop ducking",))
        if f.has_music and not f.music_ducked and "music" not in ctx.recipes:
            return Expansion(notes=("ducking is already off — the music plays at its own level",))
        return Expansion(
            steps=(step("set_duck", STAGE_MUSIC, "stop ducking the music under speech", track="music",
                        enabled=False),),
            postconditions=(pc("music_ducked", "ducking is off", enabled=False),),
            notes=("ducking off — the music keeps its level under speech",))
    current = f.music_duck_db if f.music_ducked else None
    if it.get("_deeper") is not None and it.get("to_db") is None and current is not None:
        to_db = float(current) + float(it.get("_deeper"))
    else:
        to_db = float(it.get("to_db", -18.0))
    to_db = min(0.0, max(-40.0, to_db))
    if current is not None and abs(current - to_db) < 0.05 and "music" not in ctx.recipes:
        # Final QA r3: the same depth again was "done — 1/1 checks held"
        return Expansion(notes=(f"The music already ducks to {to_db:g} dB under speech — nothing changed. "
                                f"Say 'duck the music more' to dip it further.",))
    prereq = () if f.has_music or "music" in ctx.recipes else (Intent("music", {"mood": it.get("_mood")}),)
    return Expansion(
        steps=(step("set_duck", STAGE_MUSIC, "duck the music under speech", track="music", enabled=True, to_db=to_db),),
        postconditions=(pc("music_ducked", "music ducks under speech", to_db=max(to_db, -12.0)),),
        prerequisites=prereq)


def _x_beat_sync(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    subdivision = int(it.get("subdivision") or 4)
    subdivision = min(16, max(1, subdivision))
    pulse = it.get("pulse")
    pulse = True if pulse is None else bool(pulse)
    prereq = () if f.has_music or "music" in ctx.recipes else (Intent("music", {"mood": it.get("_mood")}),)
    notes: list[str] = []
    steps: list[Step] = []
    # The exact grid is only known for a preset bed being ADDED in this plan
    # (its sidecar states it) — and only when nothing earlier in the plan
    # re-times the footage, because the word spans below are pre-cut.
    bed = bed_for_mood(it.get("_mood")) if ("music" in ctx.recipes and not f.has_music) else None
    can_precompute = bed is not None and f.has_transcript and not ctx.has_cut_steps
    splits: list[float] = []
    if can_precompute:
        splits = beat_split_times(bed.beats(f.duration), f.duration,
                                  [(w.start, w.end) for w in f.word_spans], subdivision=subdivision)
    if len(splits) >= 2:
        steps += [step("split_at", STAGE_MUSIC, f"split on beat at {t:.2f}s (clear of words)", track="v1", time=t)
                  for t in splits]
        notes.append(f"{len(splits)} beat-aligned splits at {bed.bpm:g} BPM, none inside a word")
    else:
        # `min_shot` here, not only in the precomputed path: the plan promises
        # `min_shot_geq(MIN_SHOT_S)` below, and when the bed already exists
        # this tool is the only thing placing the cuts — without it the
        # fragment before the first beat was whatever the footage gave
        # (benchmark case 10 measured 0.697 s) and the plan failed its own
        # postcondition.
        steps.append(step("auto_cut_to_beats", STAGE_MUSIC, "split v1 on the detected beats of the music",
                          subdivision=subdivision, min_shot=MIN_SHOT_S))
        notes.append("splits placed by beat detection at run time" + (
            " (the transcript is read after the cuts)" if ctx.has_cut_steps else ""))
    pcs = [pc("beat_splits_geq", "cuts were placed on beats", n=2),
           pc("min_shot_geq", "no shot is too short", seconds=MIN_SHOT_S)]
    if pulse:
        # Every fragment gets a 1.0 → 1.05 punch-in from its first frame — the
        # same keyframe shape apply_hook_stack uses. `$v1_all` fans out AFTER
        # the splits so the fragments (fresh ids) each carry the pulse.
        steps.append(step("add_keyframe", STAGE_MUSIC, "anchor scale 1.0 at each fragment start",
                          clip_id="$v1_all", prop="scale", time=0.0, value=1.0, interp="ease-out"))
        steps.append(step("add_keyframe", STAGE_MUSIC, f"punch in to {PULSE_SCALE} on the beat",
                          clip_id="$v1_all", prop="scale", time=PULSE_RISE_S, value=PULSE_SCALE, interp="ease-out"))
        pcs.append(pc("beat_pulse_present", "punch-ins land on beats", n=2))
    return Expansion(steps=tuple(steps), postconditions=tuple(pcs), notes=tuple(notes), prerequisites=prereq)


def _x_hook(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    duration = float(it.get("duration_s") or 3.0)
    duration = min(6.0, max(1.0, duration))
    content_brain: str | None = None
    notes: list[str] = []
    questions: tuple[NeedsInput, ...] = ()
    text = (it.get("text") or "").strip()
    if text:
        notes.append("hook text: yours")
    elif ctx.hook_text and ctx.hook_text[0].strip():
        text, content_brain = ctx.hook_text[0].strip(), ctx.hook_text[1]
        notes.append(f"hook text: written by the {content_brain} brain")
    else:
        guess = heuristic_hook(f.transcript_head)
        if guess:
            text = guess
            notes.append("hook text: heuristic from your first sentence (no local model available)")
        elif f.transcript_pending or "transcribe" in ctx.recipes:
            # QA-072: the transcript lands before this step runs (the executor
            # waits for the upload's, or the plan transcribes first), so the
            # line is written from it THEN — live.live_hook_text.
            text = HOOK_SENTINEL
            notes.append("hook text: from your first sentence, written once the transcript is ready")
        else:
            text = _CANNED_HOOK
            notes.append("hook text: generic (no transcript yet) — reply with your own line to change it")
            questions = (ask("hook_text", "What should the hook say?", kind="text", default=_CANNED_HOOK, required=False),)
    text = text if text == HOOK_SENTINEL else text[:60]
    return Expansion(
        steps=(step("apply_hook_stack", STAGE_TEXT, "text + punch-in + audio fade in the first seconds",
                    text=text, duration=duration, visual="punch_in", audio="fade_boost"),),
        postconditions=(pc("hook_text_starts_leq", "the hook starts immediately", t=0.5),
                        pc("hook_axes_geq", "the hook works on several axes", n=3)),
        questions=questions, notes=tuple(notes), content_brain=content_brain)


#: The bundled looks a "Which look?" question offers (Final QA r2).
_LOOK_OPTIONS: tuple[tuple[str, str], ...] = (
    ("teal_orange.cube", "Cinematic"), ("warm.cube", "Warm"), ("cool.cube", "Cool"),
    ("punch.cube", "Punchy"), ("faded.cube", "Faded"), ("mono.cube", "Black and white"))


#: run 4: one step of a look's strength ("stronger" / "weaker").
LOOK_STRENGTH_STEP = 0.2


def _x_look_strength(it: Intent, f: TimelineFacts, look: str, way: str) -> Expansion:
    """"make the warm look stronger" / "tone the filter down": the strength
    of the look the clips ALREADY carry moves by `LOOK_STRENGTH_STEP`, on
    those clips only (it re-applied warm at 80 % to every clip — and "weaker"
    turned it UP). A named clip narrows it; a look nowhere on the timeline
    is a question, never a fresh apply."""
    main = [c for c in f.clips if c.id in set(f.v1_clip_ids) and c.freeze is None]
    ref = it.get("clip_ref")
    if ref not in (None, "$v1_all"):
        cid, q = CX.bind_clip(ref, f)
        if q:
            return Expansion(notes=(q,))
        ids = list(f.v1_clip_ids)
        # final sweep 4: "$v1_first" / "$v1_last" are sentinels, not ids —
        # "the warm look on clip 1" filtered every clip out and said "no warm look"
        cid = {"$v1_first": ids[0] if ids else cid, "$v1_last": ids[-1] if ids else cid}.get(cid, cid)
        main = [c for c in main if c.id == cid]
    carrying = [c for c in main if look in c.look_intensity]
    name = look.replace(".cube", "").replace("_", " ")
    if not carrying:
        if ref not in (None, "$v1_all") and main:
            return Expansion(notes=(f"{CX._label(main[0].id, f).capitalize()} has no {name} look to make "
                                    f"{'stronger' if way == 'up' else 'weaker'} — say 'apply the {name} look to it' first.",))
        return Expansion(notes=(f"There is no {name} look on the timeline to make {'stronger' if way == 'up' else 'weaker'} "
                                f"— say 'apply the {name} look' first.",))
    steps, pcs, notes = [], [], []
    for c in carrying[:20]:
        cur = float(c.look_intensity[look])
        new = round(min(1.0, max(0.0, cur + (LOOK_STRENGTH_STEP if way == "up" else -LOOK_STRENGTH_STEP))), 2)
        if abs(new - cur) < 0.005:
            notes.append(f"{CX._label(c.id, f)}: the {name} look is already at {'full' if way == 'up' else 'zero'} strength")
            continue
        steps.append(step("apply_lut", STAGE_LOOK, f"{CX._label(c.id, f)}: {name} look {cur:.0%} → {new:.0%}",
                          clip_id=c.id, src=look, intensity=new))
        pcs.append(pc("effect_present", "the look is applied", type="lut", track="v1", all=False, clip_id=c.id))
        notes.append(f"{CX._label(c.id, f)}: {name} look {cur:.0%} → {new:.0%}")
    if not steps:
        return Expansion(notes=tuple(notes))
    return Expansion(steps=tuple(steps), postconditions=tuple(pcs), notes=tuple(notes))


def _x_color_look(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    look = it.get("look")
    intensity = float(it.get("intensity", 0.8))
    intensity = min(1.0, max(0.0, intensity))
    if it.get("_strength") in ("up", "down") and it.get("_range") is None and not it.get("_half"):
        if not look:
            carried = sorted({lk for c in f.clips if c.id in set(f.v1_clip_ids) for lk in c.looks})
            if len(carried) == 1:
                look = carried[0]                 # "make the look stronger": the one look there is
            elif carried:
                return Expansion(notes=("Which look? The clips carry " + ", ".join(x.replace(".cube", "") for x in carried)
                                        + " — say like 'make the warm look stronger'.",))
            else:
                return Expansion(notes=("There is no look on the timeline to change the strength of — say like "
                                        "'apply the warm look' first.",))
        return _x_look_strength(it, f, look, str(it.get("_strength")))
    if (it.get("_range") is not None or it.get("_half")) and it.get("clip_ref") is None and look:
        if it.get("_ui_anchor") is not None:
            # "from here to the end make it black and white": split at the
            # playhead, the look on the right half and every clip after it
            pre, ids, q = CX.range_targets(it.get("_range"), f, "change the look of")
            if q or not ids:
                return Expansion(notes=(q or "That range covers no clip — which clip should get the look?",))
            return Expansion(
                steps=tuple(pre) + tuple(step("apply_lut", STAGE_LOOK, f"apply the {look.replace('.cube', '')} look to "
                                              f"{CX._label(cid, f)}", clip_id=cid, src=look, intensity=intensity)
                                         for cid in ids),
                postconditions=tuple(pc("effect_present", "the look is applied", type="lut", track="v1", all=False,
                                        clip_id=cid) for cid in ids))
        ids, q = _range_clips_whole(it, f, "change the look of")
        if q or not ids:
            return Expansion(notes=(q or "That range covers no whole clip — which clip should get the look?",))
        return Expansion(
            steps=tuple(step("apply_lut", STAGE_LOOK, f"apply the {look.replace('.cube', '')} look to "
                             f"{CX._label(cid, f)}", clip_id=cid, src=look, intensity=intensity) for cid in ids),
            postconditions=tuple(pc("effect_present", "the look is applied", type="lut", track="v1", all=False,
                                    clip_id=cid) for cid in ids))
    if it.get("clip_ref") is None and (nq := CX.named_clip_question(it.clause or "", f)):
        # final sweep 4: "make the garage shot black and white" with no such
        # footage asked nothing and greyed every clip
        return Expansion(notes=(nq,))
    clip, q = CX.bind_clip(it.get("clip_ref") or "$v1_all", f)
    if q:
        return Expansion(notes=(q,))
    where = "every v1 clip" if clip == "$v1_all" else "the named clip"
    check = pc("effect_present", "the look is applied", type="lut", track="v1", all=clip == "$v1_all",
               **({} if clip == "$v1_all" else {"clip_id": clip}))
    if not look:
        # Final QA r2: no look named is a question, never the cinematic
        # default ("make this clip black and white" applied teal-orange).
        return Expansion(
            questions=(ask("look", "Which look?", options=list(_LOOK_OPTIONS)),),
            steps=(step("apply_lut", STAGE_LOOK, f"apply the chosen look to {where}",
                        clip_id=clip, src=placeholder("look"), intensity=intensity),),
            postconditions=(check,))
    return Expansion(
        steps=(step("apply_lut", STAGE_LOOK, f"apply the {look.replace('.cube', '')} look to {where}",
                    clip_id=clip, src=look, intensity=intensity),),
        postconditions=(check,))


def _x_clean_audio(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    strength = float(it.get("strength", 0.85))
    strength = min(1.0, max(0.0, strength))
    steps = [step("noise_reduce", STAGE_AUDIO, "spectral denoise of the v1 audio", optional=True,
                  clip_id="$v1_all", strength=strength)]
    pcs = [pc("clip_src_changed", "the audio was cleaned")]
    lufs = it.get("lufs")
    if lufs is not None or "export_preset" not in ctx.recipes:
        target = float(lufs) if lufs is not None else S.default_lufs(it.get("_platform"), f.loudness_lufs)
        target = min(-9.0, max(-24.0, target))
        steps.append(step("set_loudness_target", STAGE_EXPORT if (lufs is not None and "export_preset" in ctx.recipes) else STAGE_AUDIO,
                          f"normalise speech to {target:g} LUFS", lufs=target))
        pcs += [pc("loudness_target_set", "the loudness target is recorded", lufs=target),
                pc("loudness_within", "the render hits the loudness target", tol=1.0)]
    return Expansion(steps=tuple(steps), postconditions=tuple(pcs))


def _x_loudness(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    lufs = it.get("lufs")
    explicit = lufs is not None
    target = float(lufs) if explicit else S.default_lufs(it.get("_platform"), f.loudness_lufs)
    change = it.get("_change") if not explicit else None
    if change in ("up", "down"):
        target += LOUDNESS_STEP_LU if change == "up" else -LOUDNESS_STEP_LU
    target = min(-9.0, max(-24.0, target))
    stage = STAGE_EXPORT if (explicit and "export_preset" in ctx.recipes) else STAGE_AUDIO
    return Expansion(
        steps=(step("set_loudness_target", stage, f"set the export loudness target to {target:g} LUFS", lufs=target),),
        postconditions=(pc("loudness_target_set", "the loudness target is recorded", lufs=target),
                        pc("loudness_within", "the render hits the loudness target", tol=1.0)))


def _clip_targets(it: Intent, f: TimelineFacts, verb: str, default: str = "$v1_all"
                  ) -> tuple[list[Step], list[str], str | None]:
    """(split steps, clips, question) for an edit that names a clip, a range
    ("the last 3 seconds") or neither (`default`). Wave D3 (E3): "speed up
    the second clip" used to speed up EVERY clip (the ordinal was never read)."""
    rng = it.get("_range")
    if rng is None and it.get("_half") and it.get("clip_ref") is None:
        vend = float(f.video_end or f.duration)
        rng = S.TimeRange(kind=it.get("_half"), end=round(vend / 2, 3))
    if rng is not None and it.get("clip_ref") is None:
        return CX.range_targets(rng, f, verb)
    ref, q = CX.bind_clip(it.get("clip_ref") or default, f)
    return [], ([ref] if ref else []), q


def _range_clips_whole(it: Intent, f: TimelineFacts, verb: str) -> tuple[list[str], str | None]:
    """final sweep 2 r2: the clips a time range covers EXACTLY ("make the first
    4 seconds black and white" put the look on every clip). A range edge inside
    a clip asks — the look is never spread past what was named."""
    rng = it.get("_range")
    if rng is None and it.get("_half"):
        rng = S.TimeRange(kind=it.get("_half"), end=round(float(f.video_end or f.duration) / 2, 3))
    spans = CX.v1_spans(f)
    vend = float(f.video_end or f.duration)
    a, b = rng.resolve(vend)
    edges = {round(x, 3) for _c, s0, e0 in spans for x in (s0, e0)}
    bad = next((x for x in (a, b) if all(abs(x - e) > 0.05 for e in edges)), None)
    if bad is not None:
        return [], (f"{bad:g}s is inside a clip — should I {verb} only the whole clips, or split at {bad:g}s first? "
                    f"Say like 'split at {bad:g}s', then '{verb} the first clip'.")
    return [cid for cid, s0, e0 in spans if s0 >= a - 0.05 and e0 <= b + 0.05], None


#: "so it's 9 seconds", "so it lasts 16 seconds", "to fit 9 seconds": a
#: target LENGTH for the whole video, not an amount of speed.
_TARGET_LEN_RE = re.compile(r"\bso\s+(?:that\s+)?(?:it|the video|the whole thing|the clip)(?:'s|\s+is|\s+lasts|\s+runs|\s+ends up)"
                            r"|\b(?:to\s+fit|to\s+last|lasts?)\s+(?:in\s+)?\d"
                            # final sweep 4: "speed clip 1 up until it's 2 seconds long"
                            r"|\buntil\s+(?:it'?s|it\s+is|the\s+clip\s+is|the\s+video\s+is)\s+\d")
_TARGET_LEN_CLIP_RE = re.compile(r"\bso\s+(?:that\s+)?(?:it|the\s+clip)(?:'s|\s+is|\s+lasts|\s+runs|\s+ends\s+up)\s+\d"
                                 r"|\buntil\s+(?:it'?s|it\s+is|the\s+clip\s+is)\s+\d|\bto\s+(?:fit|last)\s+(?:in\s+)?\d")
_EXPLICIT_FACTOR_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:x|×|times|%|percent)\b|\b(?:double|twice|half|triple)\b")


def _speed_for_length(it: Intent, f: TimelineFacts, clips: list[str]) -> tuple[Intent, str | None]:
    """Final sweep 3: "speed up the whole thing so it's 9 seconds" played at
    1.25x (9.6 s) — the length is the ask: factor = length now / length
    wanted. A length the direction contradicts is a question. Final sweep 4:
    one named clip's length too ("until it's 2 seconds long" played 1.25x)."""
    clause = (it.clause or "").lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)\b", clause)
    target = it.get("duration_s") or (float(m.group(1)) if m else None)
    if not target or it.get("preset") or not _TARGET_LEN_RE.search(clause) or _EXPLICIT_FACTOR_RE.search(clause):
        return it, None
    if clips == ["$v1_all"]:
        vend = float(f.video_end or f.duration or 0.0)
    elif len(clips) == 1 and it.get("_range") is None and not it.get("_half") and _TARGET_LEN_CLIP_RE.search(clause):
        # one named clip's length ("slow down the last 3 seconds" is a RANGE,
        # never a 3 s target for the last clip)
        fact = NX._concrete_clip(clips[0], f)[0]
        vend = float(fact.duration) if fact is not None else 0.0
    else:
        return it, None
    if vend <= 0:
        return it, None
    x = round(vend / float(target), 3)
    faster = (it.get("factor") or 1.25) >= 1
    if (faster and x <= 1.0) or (not faster and x >= 1.0) or not 0.25 <= x <= 4.0:
        return it, (f"The video is {vend:.1f}s now — {float(target):g}s would need {x:g}x, which is not "
                    f"{'faster' if faster else 'slower'} (or is outside 0.25x-4x). Say the speed, like '1.5x'.")
    from dataclasses import replace
    return replace(it, slots={**it.slots, "factor": x}), None


def _x_speed(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    pre, clips, q = _clip_targets(it, f, "speed up" if (it.get("factor") or 1.25) >= 1 else "slow down")
    if q:
        return Expansion(notes=(q,))
    it, q = _speed_for_length(it, f, clips)
    if q:
        return Expansion(notes=(q,))
    preset = it.get("preset")
    if preset:
        # a named speed curve (wave D): the preset itself, checked by name
        label = _SPEED_PRESET_BY_ID[preset].label
        return Expansion(
            steps=tuple(pre) + tuple(step("set_speed", STAGE_CUTS, f"play the {label} speed curve", clip_id=c,
                                          preset=preset) for c in clips),
            postconditions=tuple(pc("speed_equals", f"the clip plays the {label} curve", clip_id=c, preset=preset)
                                 for c in clips))
    if it.get("_curve") and not it.get("factor"):
        # "a speed ramp" with no name: ASK which curve (a pause, no default) —
        # never a constant factor (RD2). The check is bound from the answered
        # step by validate_plan (speed_equals preset=$arg:preset).
        opts = [(p.id, p.label, p.hint) for p in _SPEED_PRESETS if p.menu]
        return Expansion(
            questions=(ask("preset", "Which speed curve?", options=opts),),
            steps=tuple(pre) + tuple(step("set_speed", STAGE_CUTS, "play the chosen speed curve", clip_id=c,
                                          preset=placeholder("preset")) for c in clips))
    factor = it.get("factor")
    if factor is None:
        # Final QA r3: no number — the words' direction, never a speed-up for
        # a "slow … down" the direction table did not read.
        factor = 0.8 if S._SLOW_DOWN_WORDS_RE.search(it.clause or "") else 1.25
    if it.get("_step") and not pre and len(clips) == 1 and clips != ["$v1_all"]:
        cur = NX._concrete_clip(clips[0], f)[0]
        if cur is not None and cur.curve is None and abs(float(cur.speed) - 1.0) > 1e-6:
            factor = round(float(cur.speed) * float(factor), 3)
    if it.get("_scale") and not pre and clips:
        # run 4: "twice as fast", "half speed", "3 times faster" MULTIPLY each
        # clip's own speed (a 2x clip made "twice as fast" was set to 2x again
        # and reported done; "half speed" took it to 0.5x)
        main = [c for c in f.clips if c.id in set(f.v1_clip_ids) and c.freeze is None]
        named = main if clips == ["$v1_all"] else [c for c in main if c.id in {NX._concrete_clip(x, f)[0].id
                                                                              for x in clips
                                                                              if NX._concrete_clip(x, f)[0] is not None}]
        if named and all(c.curve is None for c in named) and any(abs(float(c.speed) - 1.0) > 1e-6 for c in named):
            per = [(c.id, round(min(4.0, max(0.25, float(c.speed) * float(factor))), 3)) for c in named[:20]]
            same = len({x for _c, x in per}) == 1
            return Expansion(steps=tuple(step("set_speed", STAGE_CUTS, f"{CX._label(cid, f)} at {x:g}×", clip_id=cid,
                                              factor=x) for cid, x in per),
                             postconditions=tuple(pc("speed_equals", "the speed matches", clip_id=cid, factor=x)
                                                  for cid, x in per),
                             notes=((f"{'every clip' if clips == ['$v1_all'] else CX._label(per[0][0], f)} "
                                     f"×{float(factor):g} from its own speed" + (f" → {per[0][1]:g}×" if same else ""),)))
    if it.get("_step") and not pre and clips == ["$v1_all"]:
        # final sweep 2 r2: "a bit slower" with clip 2 at 2x set EVERY clip to
        # 0.8x (2x → 0.8x) — each clip steps from its own speed
        main = [c for c in f.clips if c.id in set(f.v1_clip_ids) and c.freeze is None]
        if main and len({round(float(c.speed), 3) for c in main}) > 1 and all(c.curve is None for c in main):
            per = [(c.id, round(min(4.0, max(0.25, float(c.speed) * float(factor))), 3)) for c in main[:20]]
            return Expansion(steps=tuple(step("set_speed", STAGE_CUTS, f"{CX._label(cid, f)} at {x:g}×", clip_id=cid,
                                              factor=x) for cid, x in per),
                             postconditions=tuple(pc("speed_equals", "the speed matches", clip_id=cid, factor=x)
                                                  for cid, x in per),
                             notes=(f"every clip {'slower' if float(factor) < 1 else 'faster'} from its own speed",))
    factor = min(4.0, max(0.25, float(factor)))
    if factor == 1.0 and not pre and clips and clips != ["$v1_all"]:
        # Back to normal on a clip that already plays at 1x (Final QA: it
        # set 1.25x and said done): nothing to do, and say so.
        facts = [NX._concrete_clip(c, f)[0] for c in clips]
        if all(x is not None and x.speed == 1.0 and x.curve is None and x.freeze is None for x in facts):
            who = CX._label(facts[0].id, f) if len(facts) == 1 else f"those {len(facts)} clips"
            return Expansion(notes=(f"{who[:1].upper()}{who[1:]} already plays at normal speed (1×) — "
                                    f"nothing to reset.",))
    steps = list(pre) + [step("set_speed", STAGE_CUTS, f"play at {factor:g}×", clip_id=c, factor=factor)
                         for c in clips]
    if factor < 1.0 and it.get("_smooth"):
        steps += [step("smooth_slow_motion", STAGE_CUTS, "interpolate frames for smooth slow motion",
                       optional=True, clip_id=c, factor=max(2, int(round(1 / factor)))) for c in clips]
    pcs = [pc("speed_equals", "the speed matches", clip_id=c, factor=factor) for c in clips]
    if clips == ["$v1_all"]:
        pcs.append(pc("duration_between", "the duration matches", factor=factor, tol_ratio=0.05))
    return Expansion(steps=tuple(steps), postconditions=tuple(pcs))


def _moment(it: Intent, f: TimelineFacts) -> float | None:
    """The moment a freeze or a split is at: the one the prompt names, else
    the playhead."""
    at = it.get("at")
    if at is None and it.get("_clip_edge") and it.get("_clip_ref"):
        # final sweep 4: "freeze the first / last frame of clip 2" — THAT
        # clip's edge (it froze 0 s, clip 1's frame)
        cid, _q = CX.bind_clip(it.get("_clip_ref"), f)
        span = CX._span_of(cid, f) if cid else None
        if span is not None:
            at = span[0] if it.get("_clip_edge") == "start" else max(span[0], span[1] - 1.0 / max(1, f.fps))
    if at is None and it.get("_at_end"):
        # "freeze the last frame" / "freeze at the end": the final frame.
        at = max(0.0, f.duration - 1.0 / max(1, f.fps))
    if at is None:
        at = f.playhead
    return None if at is None else round(float(at), 3)


#: The shortest hold `validate.ARG_BOUNDS` lets `freeze_frame` make.
FREEZE_MIN_S = 0.5


def _x_freeze(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    """CapCut's Freeze (wave D, freeze_frame): hold the frame at a moment."""
    at = _moment(it, f)
    if at is None or at >= f.duration:
        return Expansion(notes=(f"When should the frame freeze? {at:g}s is past the end of the {f.duration:.1f}s video."
                                if at is not None else "When should the frame freeze? Say like 'freeze frame at 3 seconds'.",))
    dur = it.get("duration_s")
    if dur is not None and float(dur) < FREEZE_MIN_S:
        # "for a quarter second": below the shortest hold the editor makes —
        # say so rather than hold the 3 s default
        return Expansion(notes=(f"The shortest freeze is {FREEZE_MIN_S:g}s — hold the frame for {FREEZE_MIN_S:g}s? "
                                f"Say 'freeze at {at:g}s for {FREEZE_MIN_S:g} seconds'.",))
    args: dict[str, Any] = {"time": at}
    if dur is not None:
        args["duration"] = float(dur)
    return Expansion(
        steps=(step("freeze_frame", STAGE_CUTS, f"hold the frame at {at:g}s", **args),),
        postconditions=(pc("freeze_held", "the frame is held", **({"duration": float(dur)} if dur is not None else {})),))


def _split_step(at: float) -> Expansion:
    return Expansion(steps=(step("split_at", STAGE_CUTS, f"split at {at:g}s", track="v1", time=round(at, 3)),),
                     postconditions=(pc("tool_ok", "the split was made", tool="split_at"),))


def _x_split(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    if it.get("_every") and it.get("_half"):
        # final sweep 4: "split every clip in half" — one split per main-track
        # clip at its midpoint (it split the selected clip once)
        spans = CX.v1_spans(f) or []
        if not spans:
            return Expansion(notes=("There is no clip on the main track to split.",))
        pts = [round((a + b) / 2, 3) for _cid, a, b in spans if b - a > 0.2][:24]
        return Expansion(steps=tuple(step("split_at", STAGE_CUTS, f"split at {t:g}s", track="v1", time=t) for t in pts),
                         postconditions=(pc("tool_ok", "the splits were made", tool="split_at"),),
                         notes=(f"split every clip in half ({len(pts)} splits)",))
    if it.get("clip_ref") is not None or it.get("_half"):
        return _split_named_clip(it, f)
    at = _moment(it, f)
    vend = float(f.video_end or f.duration)
    if at is None or not 0 < at < vend:
        if at is None:
            why = "Say like 'split at 3 seconds', or move the playhead and say 'split here'."
        elif at <= 0 or any(abs(at - b) < 1e-3 for b in f.v1_boundaries) or abs(at - vend) < 1e-3:
            # Final QA r3: "0s is not inside the video" — 0 s is its start
            why = "The playhead is on a cut — move it into a clip, or say like 'split at 3 seconds'."
        else:
            why = f"{at:g}s is not inside the video ({vend:.1f}s long) — it is past the end."
        return Expansion(notes=(f"Where should I split? {why}",))
    more = [float(t) for t in (it.get("_more_times") or ())]
    if more:
        # "split at 2 and 10 seconds": every point, or a question for one
        # that is not inside the video (never a silent half)
        outside = [t for t in more if not 0 < t < vend]
        if outside:
            return Expansion(notes=(f"Where should I split? {outside[0]:g}s is not inside the video "
                                    f"({vend:.1f}s long).",))
        points = sorted({round(t, 3) for t in (at, *more)})
        return Expansion(steps=tuple(step("split_at", STAGE_CUTS, f"split at {t:g}s", track="v1", time=t)
                                     for t in points),
                         postconditions=(pc("tool_ok", "the splits were made", tool="split_at"),))
    return _split_step(at)


def _split_named_clip(it: Intent, f: TimelineFacts) -> Expansion:
    """Final QA r3: "split clip 2" (at the playhead when it is inside that
    clip, else a question the next message answers), "split clip 2 at 7s",
    "cut clip one in half" (its midpoint)."""
    ref = it.get("clip_ref") or "$selected"
    cid, q = CX.bind_clip(ref, f)
    if q:
        return Expansion(notes=(q,))
    ids = list(f.v1_clip_ids)
    cid = {"$v1_first": ids[0] if ids else None, "$v1_last": ids[-1] if ids else None}.get(cid, cid)
    span = next(((a, b) for c, a, b in (CX.v1_spans(f) or []) if c == cid), None)
    if span is None:
        return Expansion(notes=("Where should I split? Say like 'split at 3 seconds'.",))
    a, b = span
    who = CX._label(cid, f)
    n = f"clip {ids.index(cid) + 1}" if cid in ids else who
    if it.get("_half"):
        return _split_step((a + b) / 2)
    at = it.get("at")
    if at is not None:
        at = float(at)
        if not a + 1e-3 < at < b - 1e-3 and 0 < at < b - a:
            at = a + at                      # "split clip 3 at 2 seconds": 2 s into it
        if a + 1e-3 < at < b - 1e-3:
            return _split_step(at)
        return Expansion(notes=(f"Where in {n} should I split? It runs {a:g}–{b:g}s, so {float(it.get('at')):g}s "
                                f"is outside it — say like 'split {n} at {(a + b) / 2:g} seconds'.",))
    ph = f.playhead
    if ph is not None and a + 1e-3 < ph < b - 1e-3:
        return _split_step(float(ph))
    return Expansion(
        questions=(ask("split_time", f"Where in {n} should I split? It runs {a:g}–{b:g}s — say like "
                                     f"'at {(a + b) / 2:g} seconds'.", kind="text"),),
        steps=(step("split_at", STAGE_CUTS, f"split {who}", track="v1", time=placeholder("split_time")),),
        postconditions=(pc("tool_ok", "the split was made", tool="split_at"),))


def _x_reverse(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    """QA-037: play the named clip backwards (or forwards again). The target
    is the clip the prompt names, else the selected clip, else every v1
    clip. A selection is bound to its real id here so the check measures the
    clip that was reversed, not every clip (`$selected` has no verify-time
    meaning)."""
    rev = it.get("reverse")
    rev = True if rev is None else bool(rev)
    ref = it.get("clip_ref")
    if isinstance(ref, str) and ref.startswith(("$v1_nth:", "$v1_at:")):
        ref, q = CX.bind_clip(ref, f)
        if q:
            return Expansion(notes=(q,))
    if ref in (None, "$selected"):
        if f.selection and f.selection in f.clip_ids:
            ref = f.selection
        elif ref == "$selected":
            return Expansion(notes=("select a clip first, or say 'reverse every clip'",))
        else:
            ref = "$v1_all"
    what = {"$v1_all": "every clip", "$v1_first": "the first clip", "$v1_last": "the last clip",
            "$playhead": "the clip at the playhead"}.get(str(ref), f"clip {ref}")
    human = f"{what} plays backwards" if rev else f"{what} plays forwards"
    return Expansion(
        steps=(step("set_clip_reverse", STAGE_CUTS, f"play {what} {'backwards' if rev else 'forwards'}",
                    clip_id=ref, reverse=rev),),
        postconditions=(pc("clip_reversed", human, clip_id=ref, reverse=rev),))


def _trim_music(it: Intent, rng: Any, f: TimelineFacts) -> Expansion:
    """final sweep 2 r2: "remove the last 3 seconds of the music" / "trim the
    music to 6 seconds" / "cut the music at 8 seconds" trim the MUSIC clip
    (they cut the video). Only one bed can be trimmed by these words."""
    beds = sorted((c for c in f.clips if c.track == "music"), key=lambda c: c.start)
    if not beds:
        return Expansion(notes=("There is no music on the timeline to trim.",))
    first, last = beds[0], beds[-1]
    m_end = last.start + last.duration
    spec = it.get("_music")
    if isinstance(spec, dict) and spec.get("end_at") is not None:
        kind, n = "end_at", float(spec["end_at"])
    elif rng is not None and rng.kind in ("first", "last") and rng.end:
        kind, n = rng.kind, float(rng.end)
    else:
        return Expansion(notes=("Which part of the music? Say like 'remove the last 3 seconds of the music' or "
                                "'end the music at 8 seconds'.",))
    if kind == "first":
        c, cut = first, n
        if cut >= c.duration - 0.1:
            return Expansion(notes=(f"The first music clip is only {c.duration:g}s long — how much should go?",))
        new_in = round(c.src_in + cut * (c.speed or 1.0), 3)
        return Expansion(
            steps=(step("trim_clip", STAGE_AUDIO, f"the music starts {cut:g}s later in the song", clip_id=c.id,
                        **{"in": new_in}),),
            postconditions=(pc("clip_duration", "the music's head is trimmed", clip_id=c.id,
                               seconds=round(c.duration - cut, 3), tol=0.05),),
            notes=(f"cut the first {cut:g}s of the music",))
    end_at = m_end - n if kind == "last" else n
    c = next((x for x in beds if x.start < end_at < x.start + x.duration - 1e-6), None)
    if c is None or len([x for x in beds if x.start + x.duration > end_at + 1e-6]) > 1:
        return Expansion(notes=(f"The music has {len(beds)} pieces — select the one to trim, then say it again.",)
                         if len(beds) > 1 else (f"The music runs {first.start:g}–{m_end:g}s — where should it end?",))
    new_out = round(c.src_in + (end_at - c.start) * (c.speed or 1.0), 3)
    return Expansion(
        steps=(step("trim_clip", STAGE_AUDIO, f"the music ends at {end_at:g}s", clip_id=c.id, out=new_out),),
        postconditions=(pc("clip_duration", "the music ends earlier", clip_id=c.id,
                           seconds=round(end_at - c.start, 3), tol=0.05),),
        notes=(f"the music now ends at {end_at:g}s (it ran to {m_end:g}s); the video is unchanged",))


def _x_trim(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    rng = it.get("range")
    if isinstance(rng, str):
        rng = S.extract(f"cut {rng}").range
    if it.get("_music"):
        return _trim_music(it, rng, f)
    if rng is None:
        return Expansion(questions=(ask("range", "Which part should I cut? Reply like 'the first 5 seconds' or 'from 0:05 to 0:12'.",
                                        kind="text"),),
                         steps=(step("cut_range", STAGE_CUTS, "remove the named range", track="v1",
                                     start=placeholder("range"), end=placeholder("range")),))
    # Final QA r3: "the last N seconds" / "the end" is the PICTURE's end. A
    # music bed longer than the video stretched `duration`, so the range
    # landed past the video and cut the music.
    vend = float(f.video_end or f.duration)
    if it.get("_clip_ref") is not None and rng.kind in ("first", "last") and rng.end and not it.get("_keep") \
            and it.get("_max_s") is None:
        return _trim_in_clip(it, rng, f)
    if rng.kind in ("first", "last") and rng.end and vend > 0 and float(rng.end) >= vend - 1e-6 \
            and not it.get("_keep") and it.get("_max_s") is None:
        # "remove 15 seconds from the end" on a 12 s video clamped to a cut of
        # the WHOLE main track — the picture went black under the music
        return Expansion(notes=(f"The video is only {vend:g}s long — the {rng.kind} {float(rng.end):g}s is all of "
                                "it. How much should go?",))
    start, end = rng.resolve(vend)
    if it.get("_keep") and it.get("_max_s") is None:
        return _keep_range(start, end, vend)
    if end - start <= 0.05:
        return Expansion(notes=("that range is empty on this timeline — nothing to cut",))
    max_s = it.get("_max_s")
    if max_s is not None:
        # auto_edit's target length: the cut runs AFTER the tightening cuts on
        # the live timeline (stage 3, "structure" — the same stage `shorts`
        # reshapes the footage at), so the honest check is the final length,
        # not a removed-range arithmetic against the pre-plan duration.
        #
        # QA-069: WHICH part is kept is decided at run time, on the live
        # timeline after those cuts: the best-scoring run of whole sentences
        # that fits (wave C — it used to be the first `max_s` seconds, ending
        # on a sentence), as a tail and a head cut (agent/prompt/live.best_window).
        return Expansion(
            steps=(step("cut_range", STAGE_STRUCTURE,
                        f"keep the best {float(max_s):g}s — whole sentences, dense speech, a strong opening line",
                        optional=bool(it.get("_optional")), track="v1",
                        start=f"{FIT_BEST_PREFIX}{float(max_s):g}", end=round(max(end, f.duration), 3)),),
            postconditions=(pc("duration_leq", "the video fits the target length", max=round(float(max_s) + 0.5, 3)),),
            notes=(f"trimmed to the best {float(max_s):g}s after the cuts, on sentence boundaries",))
    return Expansion(
        steps=(step("cut_range", STAGE_CUTS, f"remove {start:.2f}–{end:.2f}s and close the gap",
                    track="v1", start=round(start, 3), end=round(end, 3)),),
        postconditions=(pc("duration_between", "the duration matches", start=round(start, 3), end=round(end, 3), tol=0.1),))


def _trim_in_clip(it: Intent, rng: Any, f: TimelineFacts) -> Expansion:
    """"cut the first 2 seconds of clip 3" / "trim the last second off clip
    1": the range is measured inside the named clip's span on the timeline."""
    cid, q = CX.bind_clip(it.get("_clip_ref"), f)
    if q or cid is None:
        return Expansion(notes=(q or "Which clip?",))
    span = CX._span_of(cid, f)
    if span is None:
        return Expansion(notes=("Which part should I cut? Say it in video time, like 'from 9s to 10s'.",))
    a, b = span
    n = float(rng.end)
    what = CX._label(cid, f)
    if n >= (b - a) - 0.05:
        return Expansion(notes=(f"{what.capitalize()} is only {b - a:g}s long — the {rng.kind} {n:g}s is all of it. "
                                "Delete the clip, or how much should go?",))
    start, end = (a, a + n) if rng.kind == "first" else (b - n, b)
    return Expansion(
        steps=(step("cut_range", STAGE_CUTS, f"remove the {rng.kind} {n:g}s of {what} ({start:.2f}–{end:.2f}s)",
                    track="v1", start=round(start, 3), end=round(end, 3)),),
        postconditions=(pc("duration_between", "the duration matches", start=round(start, 3), end=round(end, 3),
                           tol=0.1),),
        notes=(f"removed the {rng.kind} {n:g}s of {what}",))


def _keep_range(start: float, end: float, vend: float) -> Expansion:
    """"keep only the first 10 seconds": cut everything OUTSIDE [start, end)
    — the tail first, so the head cut's times stay valid."""
    if end - start <= 0.05:
        return Expansion(notes=("that range is empty on this timeline — nothing to keep",))
    steps = []
    if vend - end > 0.05:
        steps.append(step("cut_range", STAGE_CUTS, f"remove {end:.2f}–{vend:.2f}s (keep {start:.2f}–{end:.2f}s)",
                          track="v1", start=round(end, 3), end=round(vend, 3)))
    if start > 0.05:
        steps.append(step("cut_range", STAGE_CUTS, f"remove 0.00–{start:.2f}s (keep {start:.2f}–{end:.2f}s)",
                          track="v1", start=0.0, end=round(start, 3)))
    if not steps:
        return Expansion(notes=(f"the video is already {vend:.1f}s long — nothing to cut",))
    return Expansion(
        steps=tuple(steps),
        postconditions=(pc("duration_between", "the kept part is the whole video", target=round(end - start, 3),
                           tol=0.1),),
        notes=(f"kept {start:.2f}–{end:.2f}s",))


#: A name card reads longer than a headline: 4 s (the `add_lower_third`
#: handler's own default) against the title's 3 s.
LOWER_THIRD_S = 4.0
TITLE_S = 3.0


_HERE_RE = re.compile(r"\bhere\b|\b(?:at|under|where|from)\s+(?:the\s+)?(?:playhead|cursor|scrubber)(?:\s+is)?\b")


def _unquoted(text: str) -> str:
    """`text` without its quoted words ("add a title 'Right here'")."""
    return re.sub(r"[\"“”'‘][^\"“”‘’]{1,200}[\"“”'’]", " ", text.lower())


def _title_span(it: Intent, f: TimelineFacts, default_dur: float) -> tuple[float, float]:
    """(start, end) for an on-screen card from the `at`/`dur` slots — "at the
    start" is the default, "at the end" backs off from the tail."""
    dur = float(it.get("dur") or default_dur)
    at = it.get("at")
    words = str(it.get("text") or it.get("name") or "").strip().lower()
    place = _unquoted(it.clause or "")
    if words and not re.search(r"[\"“”'‘’]", it.clause or ""):
        # "saying here we go" is the title's words (quoted ones are gone
        # already: "add a title 'Last' on the last clip" kept its clip)
        place = place.replace(words, " ")
    if at is None and f.playhead is not None and _HERE_RE.search(place):
        # final sweep 3: "add a title here saying Look!" went to 0:00
        at = float(f.playhead)
    first_secs = re.search(r"\bfirst\s+(?:\d|few|couple)|\b(?:the\s+)?(?:very\s+)?(?:start|beginning)\b", place)
    on_clip = _named_v1_clip(place, f) if at is None or (at == "start" and not first_secs) else None
    if on_clip is not None:
        # final sweep 3 r2: "add a title 'Pour' on clip 2" went to 0:00 and
        # replaced the step-1 title there — it starts on that clip now and
        # ends with it at the latest
        c_start, c_end = on_clip
        return round(c_start, 3), round(min(c_end, c_start + dur), 3)
    if at in (None, "start"):
        start = 0.0
    elif at == "end":
        # the PICTURE's end (Final QA r3): a long music bed put an end card
        # 28 s after the video ended
        # K3: after a trim planned in the SAME prompt ("trim the last 2
        # seconds and add a title at the end") the end is the trimmed one
        vend = it.get("_video_end") if it.get("_video_end") is not None else (f.video_end or f.duration)
        start = max(0.0, float(vend) - dur)
    else:
        start = max(0.0, float(at))
    end = min(f.duration, start + dur) if f.duration > 0 else start + dur
    return round(start, 3), round(end, 3)


def _named_v1_clip(place: str, f: TimelineFacts) -> tuple[float, float] | None:
    """(start, end) of the ONE main-track clip `place` names ("on clip 2",
    "over the second clip", "on the last clip", "on the pour shot"), else None."""
    from . import grammar as G
    v1 = sorted((c for c in f.clips if c.track == "v1"), key=lambda c: c.start)
    if not v1:
        return None
    ref = G.clip_ref_of(place) or clip_by_name(place, f)
    if not ref or not isinstance(ref, str):
        return None
    pick = None
    if ref == "$v1_first":
        pick = v1[0]
    elif ref == "$v1_last":
        pick = v1[-1]
    elif ref.startswith(G.NTH_REF):
        n = ref[len(G.NTH_REF):]
        if n.lstrip("-").isdigit() and 1 <= abs(int(n)) <= len(v1):
            pick = v1[int(n) - 1] if int(n) > 0 else v1[int(n)]
    elif ref.startswith(G.AT_REF):
        t = float(ref[len(G.AT_REF):])
        pick = next((c for c in v1 if c.start - 1e-6 <= t < c.start + c.duration), None)
    elif ref == "$selected" and f.selection:
        pick = next((c for c in v1 if c.id == f.selection), None)
    elif not ref.startswith("$"):
        pick = next((c for c in v1 if c.id == ref), None)
    if pick is None:
        return None
    return float(pick.start), float(pick.start + pick.duration)


def clip_by_name(text: str, f: TimelineFacts) -> str | None:
    """The id of the ONE main-track clip a noun phrase names by its media or
    shot name — "the pour shot", "the kitchen before shot", "re_kitchen_before"
    (final sweep 3 r2, final sweep 4: `clip_expanders.clip_by_name`)."""
    return CX.clip_by_name(text, f)


#: hh:mm:ss or hh:mm:ss:ff (SMPTE) — the frames need the project's fps.
_TIMECODE_RE = re.compile(r"(?<![\w:.])(\d{1,2}):(\d{2}):(\d{2})(?::(\d{2}))?(?![\w:.])")


def timecodes_to_seconds(prompt: str, fps: int | float | None) -> str:
    """"00:00:07:15" → "7.5s" at 30 fps, "00:01:02" → "62s" (final sweep 4:
    the split / cut readers knew mm:ss only, so a timecode asked "which
    part?" or "where should I split?")."""
    rate = float(fps or 30) or 30.0

    def _sub(m: re.Match) -> str:
        h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
        ff = int(m.group(4)) if m.group(4) is not None else 0
        secs = h * 3600 + mi * 60 + s + ff / rate
        return f"{round(secs, 3):g}s"
    return _TIMECODE_RE.sub(_sub, prompt or "")


def clip_names_to_numbers(prompt: str, facts: TimelineFacts) -> str:
    """"slow down the pour shot" → "slow down clip 2", "the before shots" →
    "clips 2 and 4" (final sweep 3 r2 / final sweep 4: no rule read a clip
    by its name, so a named shot widened to EVERY clip). Quoted words are
    left alone; a name that matches nothing is left as it was, so the
    expander asks instead of widening (`clip_expanders.names_to_numbers`).
    A timecode becomes seconds at the project's fps (the same pre-pass the
    contract reads)."""
    return timecodes_to_seconds(CX.names_to_numbers(prompt, facts), facts.fps)


def _x_lower_third(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    """A name + handle card through `add_lower_third` (spec §2.4 row 8): the
    handler draws the name line over the handle line in the `lower_third`
    role at the platform's lower-third line (0.74·h on 9:16, 0.80·h else —
    the same look as presets/text_styles/clean_lower.json), so the safe-zone
    check holds without a preset. A handle alone is still an identity card
    (`name=@handle`); with NEITHER the plan asks for the name — never "what
    should the title say?", which is the wrong question for a name card."""
    start, end = _title_span(it, f, LOWER_THIRD_S)
    name = (it.get("name") or "").strip()
    handle = (it.get("handle") or "").strip()
    if not name and not handle:
        return Expansion(
            questions=(ask("name", "Whose name should the lower third show?", kind="text"),),
            steps=(step("add_lower_third", STAGE_TEXT, "name lower third", name=placeholder("name"),
                        start=start, end=end),),
            postconditions=(pc("text_present", "the lower third is on screen", contains=f"{ARG_REF}name",
                               role="lower_third"),
                            pc("overlays_inside_safe_zone", "text stays clear of the platform UI")))
    shown = name or handle
    s = step("add_lower_third", STAGE_TEXT, "name + handle lower third" if name and handle else "name lower third",
             name=shown, handle=(handle if name and handle else None), start=start, end=end)
    return Expansion(
        steps=(s,),
        postconditions=(pc("text_present", "the lower third is on screen", contains=shown, role="lower_third"),
                        pc("overlays_inside_safe_zone", "text stays clear of the platform UI")))


#: A title's LOOK cannot change from the Prompt bar yet (no plan tool
#: restyles a text); say where it is done — never add a new title.
TITLE_RESTYLE_REPLY = ("The Prompt bar cannot restyle a title yet — select it on the timeline and change its "
                       "colour, size or outline in the Inspector (Text).")


def _x_title(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    from .presets import text_styles
    if it.get("_restyle"):
        # K3: a title's look is a plan edit now (set_text_style)
        return NX.x_restyle_text(it, f, ctx)
    if it.get("_retime"):
        return NX.x_retime_text(it, f)
    text = (it.get("text") or "").strip()
    if it.get("_lower_third") and text and not it.get("name"):
        # Final QA: "add a lower third saying Jane Doe, Producer" became a
        # big top-of-frame SUPER title (and replaced the user's title as an
        # overlap) — a lower third is a name card whatever slot the words
        # arrived in; a comma starts its second line.
        from dataclasses import replace
        first, _, rest = text.partition(",")
        it = replace(it, slots={**it.slots, "name": first.strip(), "text": None,
                                "handle": it.get("handle") or (rest.strip() or None)})
    if it.get("name") or it.get("handle") or it.get("_lower_third"):
        return _x_lower_third(it, f, ctx)
    if it.get("_countdown"):
        return _x_countdown(it, f)
    start, end = _title_span(it, f, TITLE_S)
    if not text:
        # `$arg:text` binds the check to the answer on resume, and ties the
        # check to the step so a twice-refused answer drops both together.
        return Expansion(questions=(ask("text", "What should the title say?", kind="text"),),
                         steps=(step("add_text", STAGE_TEXT, "title text", text=placeholder("text"),
                                     start=start, end=end, role="super"),),
                         postconditions=(pc("text_present", "the text is on screen", contains=f"{ARG_REF}text"),))
    preset = (text_styles() or {}).get(it.get("_style") or "bold_pop")
    if preset is not None:
        args = preset.add_text_args(text=text[:120], start=start, end=end,
                                    canvas_w=f.canvas_w, canvas_h=f.canvas_h, aspect=f.aspect)
    else:
        args = {"text": text[:120], "start": start, "end": end, "role": "super"}
    look = it.get("_look") or {}
    # final sweep 2 r2: "add a title 'Intro' and make it red" was white
    if look.get("color"):
        args["color"] = look["color"]
    if look.get("size"):
        args["size"] = float(look["size"])
    elif look.get("size_scale") and args.get("size"):
        args["size"] = round(float(args["size"]) * float(look["size_scale"]), 1)
    if look.get("upper") is not None:
        args["upper"] = bool(look["upper"])
    _place_new_title(args, look, f)
    if _texts_overlapping(f, start, end) and _named_v1_clip(_unquoted(it.clause or ""), f) is not None:
        # final sweep 4: a second title ON A CLIP that already has one was
        # REPLACING it (add_text's overlap rule), and the safety net refused
        # the plan — a stacked title gets its own lane, as the Text panel's.
        # A title placed by time alone still replaces the one at that spot
        # (and says so), as before.
        args["allow_stack"] = True
    return Expansion(
        steps=(step("add_text", STAGE_TEXT, "title text overlay", **args),),
        postconditions=(pc("text_present", "the text is on screen", contains=text[:120]),
                        pc("overlays_inside_safe_zone", "text stays clear of the platform UI")))


def _texts_overlapping(f: TimelineFacts, start: float, end: float) -> bool:
    return any(t.role not in ("caption", "watermark") and float(t.start) < end - 1e-6 and float(t.end) > start + 1e-6
               for t in f.texts)


COUNTDOWN_CARD_S = 1.0


def _x_countdown(it: Intent, f: TimelineFacts) -> Expansion:
    """A 3 · 2 · 1 countdown (final sweep 4: "add a countdown" got the
    generic Trim / Speed / Title menu): three one-second cards "3", "2", "1"
    on their own lane, starting at the named clip's start, a named time, or
    the playhead — so it counts down instead of showing one static card."""
    from .presets import text_styles
    at = it.get("at")
    place = _unquoted(it.clause or "")
    on_clip = _named_v1_clip(place, f) if at is None else None
    if on_clip is not None:
        start = float(on_clip[0])
    elif at is not None and at not in ("start", "end"):
        start = max(0.0, float(at))
    elif at == "start":
        start = 0.0
    else:
        start = float(f.playhead) if f.playhead is not None and at != "end" else 0.0
    vend = float(f.video_end or f.duration or 0.0)
    if at == "end" and vend > 0:
        start = max(0.0, vend - 3 * COUNTDOWN_CARD_S)
    if vend > 0 and start >= vend - 0.05:
        return Expansion(notes=(f"Where should the countdown go? {start:g}s is at the end of the {vend:.1f}s video — "
                                "say like 'add a countdown at 0:10' or 'on clip 2'.",))
    preset = (text_styles() or {}).get("bold_pop")
    steps_, pcs = [], []
    for i, word in enumerate(("3", "2", "1")):
        s = round(start + i * COUNTDOWN_CARD_S, 3)
        e = round(s + COUNTDOWN_CARD_S, 3)
        if vend > 0:
            e = min(e, round(vend, 3))
        if e - s < 0.1:
            break
        if preset is not None:
            args = preset.add_text_args(text=word, start=s, end=e, canvas_w=f.canvas_w, canvas_h=f.canvas_h, aspect=f.aspect)
        else:
            args = {"text": word, "start": s, "end": e, "role": "super"}
        args["allow_stack"] = True
        steps_.append(step("add_text", STAGE_TEXT, f"countdown card {word}", **args))
        pcs.append(pc("text_present", f"the {word} card is on screen", contains=word, start_geq=round(s - 0.01, 3)))
    return Expansion(steps=tuple(steps_), postconditions=tuple(pcs),
                     notes=(f"3 · 2 · 1 countdown at {start:g}–{start + len(steps_) * COUNTDOWN_CARD_S:g}s",))


def _place_new_title(args: dict[str, Any], look: dict[str, Any], f: TimelineFacts) -> None:
    """A new title's outline, box, font, motion and place from its own words
    (final sweep 3 r2, HIGH: "small … at the bottom", "top right corner",
    "in a black box", "with no outline", "slides in" all got the default big
    centred look). Places stay inside the platform safe zone the
    `overlays_inside_safe_zone` check holds the title to."""
    from .presets import safe_zone_for
    if look.get("stroke_w") is not None:
        args["stroke_w"] = float(look["stroke_w"])
    if look.get("background") is not None:
        args["background"] = look["background"] or None
    if look.get("font"):
        args["font"] = look["font"]
    for k in ("anim_in", "anim_out"):
        if look.get(k):
            args[k] = look[k]
    z = safe_zone_for(f.aspect)
    h, w = float(f.canvas_h or 1080), float(f.canvas_w or 1920)
    y = {"top": z.y_min + 0.07, "middle": 0.5, "bottom": z.lower_third_y}.get(str(look.get("position") or ""))
    if y is not None:
        args["y"] = round(h * y, 1)
    x = {"left": 0.3, "right": min(0.7, z.x_max - 0.15)}.get(str(look.get("_x") or ""))
    if x is not None:
        args["x"] = round(w * x, 1)


def _x_brand(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    handle = (it.get("handle") or f.brand_handle or "").strip()
    questions: tuple[NeedsInput, ...] = ()
    handle_arg: Any = handle
    if not handle:
        questions = (ask("handle", "Which handle should the watermark show? Reply like @yourname.", kind="text"),)
        handle_arg = placeholder("handle")
    hashtags = list(it.get("hashtags") or ())
    palette = list(it.get("palette") or ())
    pcs = [pc("brand_watermark_present", "the brand watermark is placed"),
           pc("brand_kit_set", "the brand kit is recorded"),
           # `$arg:handle` resolves in validate_plan once the answer is bound,
           # so a plan that ASKED for the handle is verified with the same
           # three checks as one that had it inline (it used to get two).
           pc("text_present", "the end card shows the handle", contains=handle or f"{ARG_REF}handle",
              start_geq=max(0.0, f.duration - 3.5))]
    return Expansion(
        steps=(step("apply_brand_kit", STAGE_TEXT, "watermark + end card from the brand kit",
                    handle=handle_arg, hashtags=hashtags or None, palette=palette or None),),
        postconditions=tuple(pcs), questions=questions)


def _x_end_card(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    if "brand" in ctx.recipes or f.brand_handle:
        return Expansion(notes=("the brand kit already places an end card in the last 3 s — not adding a second one",))
    handle = (it.get("handle") or "").strip()
    questions: tuple[NeedsInput, ...] = ()
    handle_arg: Any = handle
    if not handle:
        questions = (ask("handle", "Which handle should the end card show? Reply like @yourname.", kind="text"),)
        handle_arg = placeholder("handle")
    start = max(0.0, f.duration - 3.0)
    return Expansion(
        steps=(step("apply_text_template", STAGE_TEXT, "end card with the handle in the last 3 s",
                    name="end_card_handle", fields={"handle": handle_arg}, start=round(start, 3), end=round(f.duration, 3)),),
        postconditions=(pc("text_present", "the end card is on screen", contains=handle or None, start_geq=start),
                        pc("overlays_inside_safe_zone", "text stays clear of the platform UI")),
        questions=questions)


#: Up to this many seams get one plan step each (a look cycles its types
#: across them, and the run log names each seam). More than that — "between
#: every clip" on a chopped-up edit — becomes ONE `$v1_seams` fan-out step, so
#: the plan stays inside MAX_STEPS and nothing is dropped (QA-070: the old cap
#: silently stopped at 12 of 16 seams).
MAX_TRANSITIONS = 12
SEAM_SNAP_S = 1.5


def _seams_for(at: Any, boundaries: list[float]) -> tuple[list[float], str | None]:
    """The seams a transition intent names: all / first / last / the seam
    nearest a time (within SEAM_SNAP_S). Second value is a note when the
    request could not be honoured exactly."""
    if not boundaries:
        return [], None
    if at in (None, "all"):
        return list(boundaries), None
    if at == "first":
        return [boundaries[0]], None
    if at == "last":
        return [boundaries[-1]], None
    try:
        t = float(at)
    except (TypeError, ValueError):
        return list(boundaries), f"did not understand where {at!r} is — using every seam"
    nearest = min(boundaries, key=lambda b: abs(b - t))
    if abs(nearest - t) > SEAM_SNAP_S:
        return [], f"no clip change within {SEAM_SNAP_S:g}s of {t:g}s — a transition needs a seam between two clips"
    if abs(nearest - t) > 0.05:
        return [nearest], f"snapped {t:g}s to the nearest clip change at {nearest:g}s"
    return [nearest], None


def _x_transitions(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    """§2.4 `transitions` + the CapCut catalog: either a LOOK (a cycle of
    types at every seam) or one named TYPE at the seams `at` names, each
    with the catalog's per-transition default duration unless the user
    said one. Stage 5 — before reframe/captions/text, because a cross-fade
    shortens the timeline and does not ripple overlays."""
    if it.get("_from_type") or it.get("_retime") or it.get("_nth") is not None \
            or (it.get("_to_duration") is not None and f.transitions):
        return _change_existing_transitions(it, f, ctx)
    if it.get("_to_duration") is not None and not it.get("duration"):
        it = Intent(it.recipe, {**it.slots, "duration": it.get("_to_duration")}, it.score, it.clause)
    if not f.v1_boundaries and not (ctx.has_cut_steps and len(f.v1_clip_ids) >= 2):
        return Expansion(notes=("only one clip on v1 — there is no seam to put a transition on",))
    seam_index = it.get("_seam_index")
    if seam_index is not None and not ctx.has_cut_steps:
        # "between the first and second clip" / "after clip 2" (wave D3, E3):
        # the N-th seam, not every seam.
        n = int(seam_index)
        if n < 0:                               # "between the last two clips"
            n = len(f.v1_boundaries) + 1 + n
        if not 1 <= n <= len(f.v1_boundaries):
            return Expansion(notes=(f"Which cut? There {'is' if len(f.v1_boundaries) == 1 else 'are'} "
                                    f"{len(f.v1_boundaries)} cut(s) between clips on the main track.",))
        it = Intent(it.recipe, {**it.slots, "at": f.v1_boundaries[n - 1]}, it.score, it.clause)
    notes: list[str] = []
    if ctx.has_cut_steps:
        # The cuts earlier in this plan move every seam, so per-seam times
        # computed now are wrong by construction. One step with the seam
        # sentinel fans out over the LIVE seams at dispatch time (executor
        # `resolve_step_args`, spec §1.1 fan-out) — the way `$v1_all` follows
        # the fragments the cuts create. Skipping transitions here (the old
        # behaviour) shipped hard cuts on every multi-clip auto edit.
        return _transitions_after_cuts(it, f, notes)
    ttype = it.get("type")
    if ttype:
        entry = transition_entry(str(ttype))
        if entry is None:
            return Expansion(notes=(f"{ttype!r} is not a transition in the catalog",))
        types: tuple[str, ...] = (entry.name,)
        duration = float(it.get("duration") or entry.duration)
        seams, note = _usable_seams(it.get("at"), f, notes)
        if note:
            notes.append(note)
        if not seams:
            return Expansion(notes=tuple(notes))
        label = entry.label
    else:
        look_name = it.get("look") or "smooth"
        looks = transition_looks()
        look = looks.get(look_name) or looks.get("smooth")
        if look is None:
            return Expansion(notes=("no transition looks are installed under presets/transitions",))
        types = look.types
        duration = float(it.get("duration") or look.duration)
        seams, note = _usable_seams(it.get("at"), f, notes)
        if note:
            notes.append(note)
        if not seams:
            return Expansion(notes=tuple(notes))
        label = f"{look.name} look"
    duration = min(2.0, max(0.1, duration))
    if len(seams) > MAX_TRANSITIONS:
        # Too many for a step each: ONE fan-out over the live seams, with the
        # same sliver rule and an honest cap (live.seam_fanout).
        steps = [step("add_transition", STAGE_TRANSITIONS, f"{types[0]} at every one of the {len(seams)} seams",
                      at=SEAM_SENTINEL, type=types[0], duration=round(duration, 3))]
        if len(types) > 1:
            notes.append(f"{len(seams)} seams: every one gets {types[0]} (a look cycles only up to "
                         f"{MAX_TRANSITIONS} seams)")
    else:
        steps = [step("add_transition", STAGE_TRANSITIONS,
                      f"{types[i % len(types)]} at the {smpte(at, f.fps)} seam",
                      at=at, type=types[i % len(types)], duration=round(duration, 3))
                 for i, at in enumerate(seams)]
    pcs = [pc("transitions_count_geq", "transitions were added", n=len(seams),
              type=types[0] if len(types) == 1 or len(steps) == 1 else None)]
    notes.append(f"{len(seams)} × {label} ({duration:g}s)")
    # final sweep 3 r2 (HIGH): existing captions are NOT re-laid. The re-lay
    # rebuilt them from the FIRST v1 clip's transcript — a voice-over's or a
    # second clip's captions were lost, restyled or merged, and the safety
    # net turned "add a crossfade" into a dead-end question. The renderer
    # already maps every overlay through the seam overlap
    # (EDL.transition_overlap), so captions stay on the words they belong to.
    return Expansion(steps=tuple(steps), postconditions=tuple(pcs), notes=tuple(notes))


#: Cross-fade kinds one generic word ("the crossfade", "the dissolve") names.
_CROSSFADE_TYPES = frozenset({"fade", "dissolve", "crossdissolve"})


def _change_existing_transitions(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    """Final QA r2: edit the transitions ALREADY on the timeline.

    "change the glitch transition to a dissolve" replaces only the seams that
    hold a Glitch (the crossfade beside it became a dissolve too); "make the
    transitions longer / shorter" scales each one's length by 1.5 and keeps
    its type (both became SHORTER cross dissolves)."""
    from ...render.transitions import canonical, display_name
    if ctx.has_cut_steps:
        return Expansion(notes=("Change the transitions in their own prompt: the cuts in this one move the seams.",))
    if not f.transitions:
        return Expansion(notes=("There are no transitions on the timeline to change — say like 'add a crossfade "
                                "between every clip'.",))
    src = it.get("_from_type")
    nth = it.get("_nth")
    picked = [tr for tr in f.transitions if canonical(tr.type) == canonical(src)] if src else list(f.transitions)
    if src and not picked and canonical(src) in _CROSSFADE_TYPES:
        # "the crossfade" / "the dissolve" names any cross-fade kind
        picked = [tr for tr in f.transitions if canonical(tr.type) in _CROSSFADE_TYPES]
    if nth is not None:
        # "the second transition": that one seam, in timeline order
        ordered = sorted(f.transitions, key=lambda tr: tr.at)
        k = int(nth) - 1 if int(nth) > 0 else len(ordered) + int(nth)
        if not 0 <= k < len(ordered):
            return Expansion(notes=(f"Which transition? There {'is' if len(ordered) == 1 else 'are'} "
                                    f"{len(ordered)} on the timeline.",))
        picked = [ordered[k]]
    if not picked:
        return Expansion(notes=(f"There is no {display_name(src)} transition on the timeline to change.",))
    steps, notes = [], []
    if (src or nth is not None) and it.get("type"):
        entry = transition_entry(str(it.get("type")))
        if entry is None:
            return Expansion(notes=(f"{it.get('type')!r} is not a transition in the catalog",))
        for tr in picked:
            dur = float(it.get("duration") or tr.duration or entry.duration)
            steps.append(step("add_transition", STAGE_TRANSITIONS, f"{entry.name} at the {smpte(tr.at, f.fps)} seam",
                              at=tr.at, type=entry.name, duration=round(min(2.0, max(0.1, dur)), 3)))
        notes.append(f"{len(picked)} × {display_name(src or picked[0].type)} → {entry.label}")
        check = pc("transitions_count_geq", "transitions were changed", n=len(picked), type=entry.name)
    else:
        factor = float(it.get("_retime") or 1.0)
        to = it.get("duration") if (src or nth is not None) else it.get("_to_duration")   # "… transition 1 second"
        for tr in picked:
            base = tr.duration if tr.duration is not None else (
                (transition_entry(tr.type) or transition_entry("fade")).duration)
            dur = round(min(2.0, max(0.1, float(to) if to is not None else float(base) * factor)), 3)
            steps.append(step("add_transition", STAGE_TRANSITIONS,
                              f"{tr.type} at the {smpte(tr.at, f.fps)} seam, {dur:g}s", at=tr.at, type=tr.type,
                              duration=dur))
        notes.append(f"{len(picked)} transition(s) set to {float(to):g}s" if to is not None else
                     f"{len(picked)} transition(s) made {'longer' if factor > 1 else 'shorter'}")
        check = pc("transitions_count_geq", "the transitions are still there", n=len(picked))
    return Expansion(steps=tuple(steps), postconditions=(check,), notes=tuple(notes))


def _usable_seams(at: Any, f: TimelineFacts, notes: list[str]) -> tuple[list[float], str | None]:
    """`_seams_for`, minus seams next to a clip too short to carry a
    transition (QA-070: silence removal leaves 0.1 s slivers, and five
    0.10 s "transitions" on them were three-frame flickers)."""
    usable, short = seams_from_boundaries(f.v1_boundaries, f.duration)
    if short:
        notes.append(f"{short} seam(s) skipped: a neighbouring clip is shorter than "
                     f"{MIN_TRANSITION_NEIGHBOUR_S:g} s")
    if not usable:
        return [], None
    return _seams_for(at, usable)


def _transitions_after_cuts(it: Intent, f: TimelineFacts, notes: list[str]) -> Expansion:
    ttype = it.get("type")
    if ttype:
        entry = transition_entry(str(ttype))
        if entry is None:
            return Expansion(notes=(f"{ttype!r} is not a transition in the catalog",))
        ttype, label, default = entry.name, entry.label, entry.duration
    else:
        looks = transition_looks()
        look = looks.get(it.get("look") or "smooth") or looks.get("smooth")
        if look is None:
            return Expansion(notes=("no transition looks are installed under presets/transitions",))
        ttype, label, default = look.types[0], f"{look.name} look", look.duration
        if len(look.types) > 1:
            notes.append(f"{look.name} look after cuts: every seam gets {ttype} (the seams are only known after the cut)")
    duration = min(2.0, max(0.1, float(it.get("duration") or default)))
    at = it.get("at")
    where = "every seam" if at in (None, "all") else "every seam (the seams move with the cuts, so 'at' cannot be honoured)"
    notes.append(f"{label} at {where}, resolved after the cuts ({duration:g}s)")
    return Expansion(
        steps=(step("add_transition", STAGE_TRANSITIONS, f"{ttype} at every seam left after the cuts",
                    at=SEAM_SENTINEL, type=ttype, duration=round(duration, 3)),),
        postconditions=(pc("transitions_count_geq", "transitions were added", n=1, type=ttype),),
        notes=tuple(notes))


def _preset_for(platform: str | None, ratio: str | None) -> str | None:
    """§2.5 conflict rule: an explicit ratio re-derives the platform preset."""
    if platform and (ratio is None or S.PLATFORM_RATIO[platform] == ratio):
        return platform
    if ratio == "9:16":
        return "shorts" if (platform or "").startswith("youtube") else (platform if platform in ("reels", "tiktok", "story") else "reels")
    if ratio == "16:9":
        return "youtube_4k" if platform == "youtube_4k" else "youtube_16x9"
    if ratio == "1:1":
        return "ig_feed_1x1"
    if ratio == "4:5":
        return "ig_feed_4x5"
    return platform


def _x_export_preset(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    name = _preset_for(it.get("platform"), it.get("_ratio"))
    if name is None:
        return Expansion(questions=(ask("platform", "Which platform is this for?",
                                        options=[(p, p.replace("_", " ")) for p in _PLATFORMS]),),
                         steps=(step("apply_export_preset", STAGE_EXPORT, "platform export preset", name=placeholder("platform")),),
                         postconditions=(pc("export_preset_applied", "the export preset is set"),))
    prereq: tuple[Intent, ...] = ()
    ratio = S.PLATFORM_RATIO[name]
    if f.aspect != ratio and "reframe" not in ctx.recipes:
        # apply_export_preset changes w/h without rescaling overlays or
        # cropping clips; the reframe recipe (auto_reframe + fit=cover) first.
        prereq = (Intent("reframe", {"ratio": ratio, "platform": name}),)
    return Expansion(
        steps=(step("apply_export_preset", STAGE_EXPORT, f"{name} canvas, bitrate and loudness", name=name),),
        postconditions=(pc("export_preset_applied", "the export preset is set", name=name),
                        pc("no_letterbox", "no black bars")),
        prerequisites=prereq)


#: A request to MAKE a new voice-over (not to change an existing sound).
_NEW_VO_RE = re.compile(r"\b(?:add|put|record|generate|create|make|need|want|give|write|lay|include|narrate|say|"
                        r"saying|says|read|reading|tts|text[- ]to[- ]speech|ai voice)\b|^(?:an?\s+)?(?:ai\s+)?"
                        r"(?:voice[- ]?over|narration)$")


def _x_voiceover(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    text = (it.get("text") or "").strip()
    clause = S.normalize(it.clause or "")
    if not text and clause and (G.clip_ref_of(clause) is not None or not _NEW_VO_RE.search(clause)):
        # review RE: a phrase about an EXISTING sound ("make the voice on clip
        # 1 deeper", "lower the voiceover") must never open with a 60 MB voice
        # download and "what should it say?"
        return Expansion(notes=("Do you want a NEW spoken voice-over, or to change a clip's sound? Say like "
                                "'add a voiceover saying \"welcome\"', 'make the voice on clip 1 deeper' or "
                                "'lower the voiceover by 6 dB'.",))
    voice = it.get("voice") or "en_US-amy-medium"
    questions: tuple[NeedsInput, ...] = ()
    downloads: tuple[DownloadNeeded, ...] = ()
    notes: list[str] = []
    text_arg: Any = text[:600]
    if not text:
        questions = (ask("text", "What should the voiceover say?", kind="text"),)
        text_arg = placeholder("text")
    if not f.is_cached(f"piper:{voice}"):
        if ctx.allow_downloads:
            downloads = (download(f"piper:{voice}", "tts_voiceover"),)
        else:
            cached = next((v for v in VOICE_IDS if f.is_cached(f"piper:{v}")), None)
            if cached is None:
                return Expansion(notes=(f"voiceover skipped — no Piper voice is downloaded (needs {voice}, 60 MB)",))
            notes.append(f"using the {cached} voice — {voice} is not downloaded")
            voice = cached
    start = it.get("start")
    if start is None:
        est = 0.6 + 0.4 * len(text.split()) if text else 2.0
        start = max(0.0, f.duration - est - 0.3) if it.get("_at_end") else 0.0
    return Expansion(
        steps=(step("tts_voiceover", STAGE_TEXT, "synthesise the line with Piper and place it on the vo track",
                    text=text_arg, voice=voice, start=round(float(start), 3), volume_db=0.0),),
        postconditions=(pc("vo_present", "the voiceover is on the timeline"),),
        questions=questions, downloads=downloads, notes=tuple(notes))


def _x_stabilize(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    clip, q = CX.bind_clip(it.get("clip_ref") or "$v1_all", f)
    if q:
        return Expansion(notes=(q,))
    return Expansion(
        steps=(step("stabilize", STAGE_CUTS, "stabilise the footage", optional=True, clip_id=clip),),
        postconditions=(pc("clip_src_changed", "the clip was stabilized"),))


def _x_upscale(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    clip, q = CX.bind_clip(it.get("clip_ref") or "$v1_all", f)
    if q:
        return Expansion(notes=(q,))
    factor = int(it.get("upscale_factor") or 2)
    return Expansion(
        steps=(step("upscale", STAGE_CUTS, f"upscale {factor}× with Real-ESRGAN", optional=True, clip_id=clip, factor=factor),),
        postconditions=(pc("clip_src_changed", "the clip was upscaled"),))


#: "turn the volume up/down" (no object) moves the export loudness target.
LOUDNESS_STEP_LU = 3.0

#: QA-018 defaults: a programme fade reads as a second; a music bed breathes
#: out over two; "turn it down/up" moves the bed by 6 dB (half/double loudness).
FADE_DEFAULT_S = 1.0
MUSIC_FADE_IN_S = 1.0
MUSIC_FADE_OUT_S = 2.0
FADE_MAX_S = 10.0
VOLUME_STEP_DB = 6.0
VOLUME_MIN_DB, VOLUME_MAX_DB = -40.0, 6.0
#: The bed's level when facts cannot say (the music recipe's own default).
MUSIC_DEFAULT_DB = -14.0


def _no_music(f: TimelineFacts, ctx: Context) -> bool:
    return not f.has_music and "music" not in ctx.recipes


def _fade_seconds(it: Intent, default: float, f: TimelineFacts) -> float:
    d = float(it.get("duration_s") or default)
    d = min(FADE_MAX_S, max(0.1, d))
    if f.duration > 0:
        d = min(d, max(0.1, f.duration / 2))
    return round(d, 3)


def _x_fade(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    """"fade in the first clip", "add a fade in at the start", "fade out at the
    end", "fade the music out over 3 seconds" (QA-018). The picture fades
    from/to black (`set_video_fade`) and the sound with it (`add_fade`) on the
    first / last v1 clip — sentinels, so a cut earlier in the plan cannot
    leave a stale id; the music fades on the bed via `fit_music_to_video`,
    which also ends the bed with the video (a fade at a music end past the
    last frame is inaudible)."""
    target = it.get("target") or "video"
    edge = it.get("edge") or "both"
    if it.get("_off"):
        return _fades_off(it, target, f, ctx)
    if it.get("_relative"):
        which = {"in": "fade in", "out": "fade out"}.get(edge, "fade")
        return Expansion(notes=(f"How long should the {which} be? Say like 'make the {which} "
                                f"{'2' if it.get('_relative') == 'longer' else '0.5'} seconds'.",))
    if target == "music":
        return _music_fade(edge, it.get("duration_s"), f, ctx, in_s=it.get("_in_s"), out_s=it.get("_out_s"))
    picture = _picture_fade(it, edge, target, f)
    if not it.get("_music_edge"):
        return picture
    # "fade in the video and fade out the music": both, in one recipe.
    music = _music_fade(it.get("_music_edge"), it.get("_music_duration_s"), f, ctx)
    return Expansion(steps=picture.steps + music.steps, postconditions=picture.postconditions + music.postconditions,
                     notes=picture.notes + music.notes)


def _fades_off(it: Intent, target: str, f: TimelineFacts, ctx: Context) -> Expansion:
    """"take the fade off the last clip" / "remove the fades": every fade on
    the named clip (or on every main-track clip) goes to 0 — the picture's
    and the sound's. It used to ADD a 1 s fade in and out."""
    edge = it.get("edge") or "both"
    sides = {"in": {"in_s": 0.0}, "out": {"out_s": 0.0}}.get(edge, {"in_s": 0.0, "out_s": 0.0})
    if target == "music":
        if _no_music(f, ctx):
            return Expansion(notes=("there is no music on the timeline",))
        msides = {{"in_s": "fade_in", "out_s": "fade_out"}[k]: v for k, v in sides.items()}
        return Expansion(steps=(step("fit_music_to_video", STAGE_AUDIO, "take the music's fades off", **msides),),
                         postconditions=(pc("music_within_video_extent", "music does not outlast the video"),),
                         notes=("the music's fades are off",))
    ref = it.get("clip_ref")
    ids = list(f.v1_clip_ids)
    if ref is not None and ref != "$v1_all":
        cid, q = CX.bind_clip(ref, f)
        if q:
            return Expansion(notes=(q,))
        cid = {"$v1_first": ids[0] if ids else None, "$v1_last": ids[-1] if ids else None}.get(cid, cid)
        targets = [cid] if cid else []
    else:
        targets = ids
    if not targets:
        return Expansion(notes=("there is no clip on the timeline to take a fade off",))
    steps: list[Step] = []
    for cid in targets[:20]:
        # only the side the words name ("remove the fade in" keeps the fade out)
        if target == "video":
            steps.append(step("set_video_fade", STAGE_TRANSITIONS, "no picture fade", clip_id=cid, **sides))
        steps.append(step("add_fade", STAGE_AUDIO, "no sound fade", clip_id=cid, **sides))
    what = CX._label(targets[0], f) if len(targets) == 1 else "every clip"
    which = {"in": "fade-ins", "out": "fade-outs"}.get(edge, "fades")
    return Expansion(steps=tuple(steps), notes=(f"took the {which} off {what}",))


def _music_fade(edge: str, duration_s: float | None, f: TimelineFacts, ctx: Context, *,
                in_s: float | None = None, out_s: float | None = None) -> Expansion:
    if _no_music(f, ctx):
        return Expansion(notes=("there is no music on the timeline to fade — add a track first",))
    it = Intent("fade", {"duration_s": duration_s})
    fin = _fade_seconds(Intent("fade", {"duration_s": in_s}) if in_s else it, MUSIC_FADE_IN_S, f) \
        if edge in ("in", "both") else None
    fout = _fade_seconds(Intent("fade", {"duration_s": out_s}) if out_s else it, MUSIC_FADE_OUT_S, f) \
        if edge in ("out", "both") else None
    what = " and ".join(x for x in (f"in over {fin:g}s" if fin else "", f"out over {fout:g}s" if fout else "") if x)
    return Expansion(
        steps=(step("fit_music_to_video", STAGE_AUDIO, f"fade the music {what}", fade_in=fin, fade_out=fout),),
        postconditions=(pc("music_fade_set", "the music fades", in_s=fin, out_s=fout),
                        pc("music_within_video_extent", "music does not outlast the video")),
        notes=(f"music fades {what}; the bed ends with the video",))


_EVERY_CLIP_RE = re.compile(r"\b(?:every|each|all(?:\s+(?:the|of the|my))?)\s+(?:clips?|shots?|parts?)\b")


def _picture_fade(it: Intent, edge: str, target: str, f: TimelineFacts) -> Expansion:
    if not f.v1_clip_ids and "v1" not in f.track_ids:
        return Expansion(notes=("there is no clip on the timeline to fade",))
    d = _fade_seconds(it, FADE_DEFAULT_S, f)
    ref = it.get("clip_ref")
    each = False
    if ref is not None:
        ref, q = CX.bind_clip(ref, f)
        if q:
            return Expansion(notes=(q,))
        # "fade out every clip" fades each clip; "fade out the video" (also
        # $v1_all) fades the video's own ends (final sweep 3: every-clip
        # asks faded only the last clip)
        each = ref == "$v1_all" and bool(_EVERY_CLIP_RE.search((it.clause or "").lower()))
        ref = "$v1_all" if each else None if ref == "$v1_all" else ref
    fc = f.clip(ref) if ref and not each else None
    if fc is not None and re.fullmatch(r"v\d+", fc.track or "") and fc.track != "v1":
        return _overlay_fade(fc.id, edge, d)
    picture = target == "video"
    steps: list[Step] = []
    pcs: list = []

    def _add(clip: str, **sides: float) -> None:
        if picture:
            steps.append(step("set_video_fade", STAGE_TRANSITIONS,
                              f"picture fades {'/'.join(k[:-2] for k in sides)} over {d:g}s", clip_id=clip, **sides))
            pcs.append(pc("video_fade_set", "the picture fades", clip_id=clip, **sides))
        steps.append(step("add_fade", STAGE_AUDIO, f"sound fades {'/'.join(k[:-2] for k in sides)} over {d:g}s",
                          clip_id=clip, **sides))
        pcs.append(pc("audio_fade_set", "the sound fades", clip_id=clip, **sides))

    first = ref or "$v1_first"
    last = ref or "$v1_last"
    if edge == "both" and first == last:
        _add(first, in_s=d, out_s=d)
    else:
        if edge in ("in", "both"):
            _add(first, in_s=d)
        if edge in ("out", "both"):
            _add(last, out_s=d)
    where = {"in": "in at the start", "out": "out at the end", "both": "in at the start and out at the end"}[edge]
    if each:
        where = {"in": "in", "out": "out", "both": "in and out"}[edge] + " on every clip"
    elif ref:
        where = {"in": "in", "out": "out", "both": "in and out"}[edge] + " on the named clip"
    return Expansion(steps=tuple(steps), postconditions=tuple(pcs),
                     notes=(f"{'picture and sound' if picture else 'sound'} fade {where} ({d:g}s)",))


def _overlay_fade(cid: str, edge: str, d: float) -> Expansion:
    """Final QA r3: a fade on an OVERLAY clip ("put the pip in multiply and
    make it fade in") is its Fade In / Fade Out animation — `set_video_fade`
    is main-track only, and the fade used to land on the first MAIN clip."""
    args: dict[str, Any] = {"clip_id": cid}
    check: dict[str, Any] = {"clip_id": cid}
    dur = round(min(3.0, max(0.1, d)), 2)
    if edge in ("in", "both"):
        args.update({"in": "fade_in", "in_duration": dur})
        check["in"] = "fade_in"
    if edge in ("out", "both"):
        args.update({"out": "fade_out", "out_duration": dur})
        check["out"] = "fade_out"
    where = {"in": "in", "out": "out", "both": "in and out"}[edge]
    return Expansion(steps=(step("set_animation", STAGE_LOOK, f"the overlay fades {where}", **args),),
                     postconditions=(pc("animation_is", f"the overlay fades {where}", **check),),
                     notes=(f"the overlay fades {where} ({dur:g}s)",))


def _x_volume(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    """"turn the music down", "lower the music to -20 dB", "voice louder"
    (QA-018). `db` is an absolute level; `change` moves the CURRENT level by
    `delta_db` (default 6 dB) — relative to what facts measured, so "down"
    twice goes down twice."""
    target = it.get("target") or "music"
    change = it.get("change")
    db = it.get("db")
    delta = float(it.get("_delta_db") or VOLUME_STEP_DB)
    if it.get("clip_ref") is not None and target != "music":
        return _clip_volume(it, f, change, db, delta)
    if target == "vo" and it.get("_vo_soft") and not any(c.track == "vo" for c in f.clips):
        # final sweep 4: "the narration" on a project with no voice-over lane
        # is the clips' own speech, not a missing lane to ask about
        target = "voice"
    if target == "vo":
        vo = [c for c in f.clips if c.track == "vo"]
        track = "vo"
        if not vo:
            named, q = VX._voiceover_elsewhere(f)       # a voice file dropped on the Music lane
            if q is not None:
                return q
            vo = [c for c in f.clips if c.id in named]
            track = vo[0].id if len(vo) == 1 else ""
        if not vo:
            return Expansion(notes=("There is no voice-over on the timeline — which clip's level did you mean? "
                                    "Name it like 'lower the second clip by 6 dB'.",))
        if len(vo) > 1 and (track != "vo" or (db is None and len({round(c.gain_db, 2) for c in vo}) > 1)):
            if db is None:
                return _clip_volumes_relative(f, change, delta, ids=[c.id for c in vo])
            return Expansion(notes=("Which voice clip? Select it and say 'this clip'.",))
        current, label = vo[0].gain_db, "voice-over"
    elif target == "music":
        if _no_music(f, ctx):
            return Expansion(notes=("there is no music on the timeline to turn up or down",))
        # review RE: "make the music quieter" set EVERY music clip to the first
        # one's level − 6 dB (−12 / −3 became −18 / −18): clips at different
        # levels each move by the amount instead, one step per clip
        beds = [c for c in f.clips if c.track == "music"]
        if db is None and len({round(c.gain_db, 2) for c in beds}) > 1:
            return _clip_volumes_relative(f, change, delta, ids=[c.id for c in beds], what="music")
        current = f.music_gain_db if f.music_gain_db is not None else MUSIC_DEFAULT_DB
        track, label = "music", "music"
    else:
        # the programme's own sound: relative to the clips' CURRENT gain (wave
        # E, F4b) — when they differ, each clip moves by the same amount
        gains = {round(c.gain_db, 2) for c in f.clips if c.id in set(f.v1_clip_ids)}
        if db is None and len(gains) > 1:
            return _clip_volumes_relative(f, change, delta)
        current = next(iter(gains)) if gains else 0.0
        track, label = "v1", "original sound"
    if db is None:
        level = current + (delta if change == "up" else -delta)
    else:
        level = float(db)
    level = round(min(VOLUME_MAX_DB, max(VOLUME_MIN_DB, level)), 2)
    if db is not None and target == "music" and f.music_gain_db is not None and abs(level - current) < 0.05:
        # "turn the music down to 20%" on a bed already at -14 dB (20 % IS
        # -14 dB): say so instead of a step that changes nothing and verifies.
        return Expansion(notes=(f"The music is already at {level:g} dB ({_pct(level)}) — what level should it be? "
                                "Say like 'turn the music down to 10%'.",))
    notes = [f"{label} {current:g} dB → {level:g} dB"]
    if target == "music" and f.music_muted:
        notes.append("the music track is muted — say 'unmute the music' to hear it")
    return Expansion(
        steps=(step("set_volume", STAGE_AUDIO, f"set the {label} level to {level:g} dB", target=track, db=level),),
        postconditions=(pc("volume_db", "the level is set", target=track, db=level),),
        notes=tuple(notes))


def _clip_volume(it: Intent, f: TimelineFacts, change: Any, db: Any, delta: float) -> Expansion:
    """One clip's level ("lower the volume of the second clip", wave D3 E3):
    `set_volume` on that clip id. A relative change moves the clip's CURRENT
    gain (`facts.clips`, wave E F4b) — it used to start from 0 dB, so
    "lower it" on a clip already at -6 dB set -6 dB again: a no-op that
    verified."""
    cid, q = CX.bind_clip(it.get("clip_ref"), f)
    if q:
        return Expansion(notes=(q,))
    if cid == "$v1_all":
        gains = {round(c.gain_db, 2) for c in f.clips if c.id in set(f.v1_clip_ids)}
        if db is None and len(gains) > 1:
            return _clip_volumes_relative(f, change, delta)
        cid = "v1"                                   # every clip: the main track's level
    elif cid in ("$v1_first", "$v1_last", "$playhead"):
        ids = list(f.v1_clip_ids)
        cid = {"$v1_first": ids[:1], "$v1_last": ids[-1:]}.get(cid, [None])[0] if ids else None
        if cid is None:
            return Expansion(notes=("Which clip? Name it like 'the second clip'.",))
    fact = f.clip(cid) if cid != "v1" else None
    if cid == "v1":
        gains = [c.gain_db for c in f.clips if c.id in set(f.v1_clip_ids)]
        current = gains[0] if gains else 0.0
    else:
        current = fact.gain_db if fact is not None else 0.0
    level = float(db) if db is not None else current + (delta if change == "up" else -delta)
    level = round(min(VOLUME_MAX_DB, max(VOLUME_MIN_DB, level)), 2)
    who = "every clip's" if cid == "v1" else ("the clip's" if fact is None else f"{CX._label(cid, f)}'s")
    if abs(level - current) < 0.05:
        edge = "loudest" if level >= VOLUME_MAX_DB else ("quietest" if level <= VOLUME_MIN_DB else "")
        return Expansion(notes=(f"{who.capitalize()} sound is already at {current:g} dB"
                                + (f", the {edge} this sets" if edge else "") + " — what level should it be?",))
    return Expansion(
        steps=(step("set_volume", STAGE_AUDIO, f"set {who} level to {level:g} dB", target=cid, db=level),),
        postconditions=(pc("volume_db", f"{who} level is set", target=cid, db=level),),
        notes=(f"{who} sound {current:g} dB → {level:g} dB",))


def _clip_volumes_relative(f: TimelineFacts, change: Any, delta: float, *, ids: list[str] | None = None,
                           what: str = "clip") -> Expansion:
    """"make my voice louder" when the main-track clips (or the music clips,
    `ids`) sit at DIFFERENT levels: each moves by the same amount (one
    `set_volume` per clip, one undo step), so a balance the user set
    survives."""
    wanted = set(f.v1_clip_ids if ids is None else ids)
    clips = [c for c in f.clips if c.id in wanted]
    step_db = delta if change == "up" else -delta
    steps, pcs = [], []
    for c in clips[:20]:
        level = round(min(VOLUME_MAX_DB, max(VOLUME_MIN_DB, c.gain_db + step_db)), 2)
        steps.append(step("set_volume", STAGE_AUDIO, f"{CX._label(c.id, f)}: {c.gain_db:g} → {level:g} dB",
                          target=c.id, db=level))
        pcs.append(pc("volume_db", f"{CX._label(c.id, f)}'s level is set", target=c.id, db=level))
    if len(clips) > 20:
        return Expansion(notes=(f"The {len(clips)} clips sit at different levels — select the ones to change, "
                                "or set one level for all, like 'set the voice to -3 dB'?",))
    return Expansion(steps=tuple(steps), postconditions=tuple(pcs),
                     notes=(f"every {what} clip's sound {step_db:+g} dB from its own level",))


def _pct(db: float) -> str:
    return f"{10 ** (db / 20.0) * 100:.0f}%"


_VO_TAKE_RE = re.compile(r"voice[ _-]?over|narration|^vo_\d", re.I)


def _mute_everything(it: Intent, f: TimelineFacts, muted: bool) -> Expansion:
    """"mute everything (except the voiceover)" (Final QA): every sound lane
    — the main track's own sound, the music, the voice-over and any other
    audio / overlay lane with sound — muted in one plan, the excepted lane
    left playing, and a check on EACH lane (the kept one included)."""
    verb = "mute" if muted else "unmute"
    keep = it.get("_keep")
    word = it.get("_keep_word")
    keep_clip = None
    if it.get("_keep_clip"):
        cid, q = CX.bind_clip(it.get("_keep_clip"), f)
        ids = list(f.v1_clip_ids)
        cid = {"$v1_first": ids[0] if ids else None, "$v1_last": ids[-1] if ids else None}.get(cid, cid)
        if q or cid not in ids:
            return Expansion(notes=(q or "Which clip should keep its sound? Name it like 'clip 1'.",))
        keep_clip = cid
    if word and keep is None:
        return Expansion(notes=(f"Which sound should keep playing? I don't know '{word}' — say like "
                                "'mute everything except the voiceover' or '… except the music'.",))
    by_lane: dict[str, list[tuple[str, str]]] = {}   # track id → [(clip id, kind)]
    for c in f.clips:
        # the main track is handled above; an overlay without sound has nothing to mute
        if c.track == "v1" or (c.track[:1] == "v" and c.track[1:].isdigit() and c.has_audio is False):
            continue
        kind = ("vo" if c.track == "vo" or _VO_TAKE_RE.search(c.name or "")
                else "music" if c.track == "music" else "other")
        by_lane.setdefault(c.track, []).append((c.id, kind))
    kinds = {k for items in by_lane.values() for _, k in items}
    if keep in ("vo", "music") and keep not in kinds:
        noun = "voice-over" if keep == "vo" else "music"
        return Expansion(notes=(f"There is no {noun} on the timeline to keep playing — say 'mute everything' "
                                "to mute all the sound.",))
    steps, pcs = [], []
    if keep_clip is not None:
        # every main-track clip but the one named, one by one
        for cid in f.v1_clip_ids:
            if cid == keep_clip:
                pcs.append(pc("clips_muted", f"{CX._label(cid, f)} keeps its sound", clip_id=cid, muted=False))
                continue
            steps.append(step("set_clip_muted", STAGE_AUDIO, f"{verb} {CX._label(cid, f)}", clip_id=cid, muted=muted))
            pcs.append(pc("clips_muted", "the clip audio is muted", clip_id=cid, muted=muted))
    elif keep != "voice" and f.v1_clip_ids:
        steps.append(step("set_clip_muted", STAGE_AUDIO, f"{verb} the main track's own sound",
                          clip_id="$v1_all", muted=muted))
        pcs.append(pc("clips_muted", "the main track's sound is muted" if muted else "the main track plays",
                      clip_id="$v1_all", muted=muted))
    for tid, items in sorted(by_lane.items()):
        lane_kinds = {k for _, k in items}
        if lane_kinds == {keep}:
            pcs.append(pc("track_muted", f"the {tid} lane keeps playing", track=tid, muted=False))
            continue
        if keep not in lane_kinds:
            steps.append(step("set_track_muted", STAGE_AUDIO, f"{verb} the {tid} lane", track=tid, muted=muted))
            pcs.append(pc("track_muted", f"the {tid} lane is muted" if muted else f"the {tid} lane plays",
                          track=tid, muted=muted))
            continue
        # A lane holding BOTH (a voice-over dropped on the music lane): the
        # clips one by one, so the kept one still plays.
        for cid, k in items:
            if k == keep:
                pcs.append(pc("clips_muted", "the kept sound plays", clip_id=cid, muted=False))
                continue
            steps.append(step("set_clip_muted", STAGE_AUDIO, f"{verb} {cid} on the {tid} lane",
                              clip_id=cid, muted=muted))
            pcs.append(pc("clips_muted", "the clip is muted" if muted else "the clip plays",
                          clip_id=cid, muted=muted))
    if not steps:
        return Expansion(notes=("There is no other sound on the timeline to mute.",))
    kept = {"vo": "the voice-over", "music": "the music", "voice": "the main track's sound"}.get(keep or "") \
        or (CX._label(keep_clip, f) if keep_clip else None)
    return Expansion(steps=tuple(steps), postconditions=tuple(pcs),
                     notes=((f"muted everything except {kept}" if kept else f"{verb}d every sound lane"),))


def _x_mute(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    target = it.get("target") or "music"
    muted = it.get("muted")
    muted = True if muted is None else bool(muted)
    verb = "mute" if muted else "unmute"
    if it.get("_everything"):
        return _mute_everything(it, f, muted)
    if target == "vo":
        if not any(c.track == "vo" for c in f.clips):
            return Expansion(notes=(f"There is no voice-over on the timeline to {verb}.",))
        return Expansion(
            steps=(step("set_track_muted", STAGE_AUDIO, f"{verb} the voice-over", track="vo", muted=muted),),
            postconditions=(pc("track_muted", "the voice-over is muted" if muted else "the voice-over plays",
                               track="vo", muted=muted),))
    if target == "music":
        if _no_music(f, ctx):
            return Expansion(notes=(f"there is no music on the timeline to {verb}",))
        if f.has_music and f.music_muted == muted and "music" not in ctx.recipes:
            return Expansion(notes=(f"the music is already {'muted' if muted else 'playing'}",))
        return Expansion(
            steps=(step("set_track_muted", STAGE_AUDIO, f"{verb} the music track", track="music", muted=muted),),
            postconditions=(pc("track_muted", "the music track is muted" if muted else "the music track plays",
                               track="music", muted=muted),))
    pre, clips, q = _clip_targets(it, f, verb)
    if q:
        return Expansion(notes=(q,))
    where = "every v1 clip" if clips == ["$v1_all"] else ("the named clip" if len(clips) == 1 else f"{len(clips)} clips")
    return Expansion(
        steps=tuple(pre) + tuple(step("set_clip_muted", STAGE_AUDIO, f"{verb} the original sound of {where}",
                                      clip_id=c, muted=muted) for c in clips),
        postconditions=tuple(pc("clips_muted", "the clip audio is muted" if muted else "the clip audio plays",
                                clip_id=c, muted=muted) for c in clips))


def _x_fit_music(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    if _no_music(f, ctx):
        return Expansion(notes=("there is no music on the timeline to fit",))
    fout = _fade_seconds(it, MUSIC_FADE_OUT_S, f) if it.get("duration_s") else None
    pcs = [pc("music_within_video_extent", "music does not outlast the video")]
    if fout:
        pcs.append(pc("music_fade_set", "the music fades", out_s=fout))
    return Expansion(
        steps=(step("fit_music_to_video", STAGE_AUDIO, "end the music with the video, fading out",
                    fade_out=fout),),
        postconditions=tuple(pcs), notes=("the music now ends with the video",))


def _x_remove_music(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    """"remove the music" (QA-018 class): the bed's clips go, and the check
    measures an empty music lane. It used to read as `add_music` and answer
    "music is already on the timeline — say 'another track'"."""
    if not f.has_music:
        return Expansion(notes=("there is no music on the timeline to remove",))
    if not f.music_clip_ids:
        return Expansion(notes=("cannot remove the music — its clips are not known; remove it on the timeline",))
    return Expansion(
        steps=(step("bulk_delete", STAGE_MUSIC, "take the music bed off the timeline",
                    clip_ids=list(f.music_clip_ids)),),
        # "remove the music and add a chill track": the new bed's own checks apply.
        postconditions=() if "music" in ctx.recipes else (pc("music_present", "the music is gone", count=0),),
        notes=("the music bed is removed",))


def _x_preview(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    return Expansion(
        steps=(step("render_preview", STAGE_AUDIT, "render the preview of the finished edit", optional=True),),
        postconditions=(pc("tool_ok", "the preview rendered", tool="render_preview"),))


def _x_ask(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    return Expansion()


EXPANDERS: dict[str, Callable[[Intent, TimelineFacts, Context], Expansion]] = {
    "transcribe": _x_transcribe, "captions": _x_captions, "translate_captions": _x_translate,
    "remove_silences": _x_remove_silences, "remove_fillers": _x_remove_fillers, "tighten": _x_tighten,
    "shorts": _x_shorts, "reframe": _x_reframe, "music": _x_music, "duck": _x_duck, "beat_sync": _x_beat_sync,
    "hook": _x_hook, "color_look": _x_color_look, "clean_audio": _x_clean_audio, "loudness": _x_loudness,
    "speed": _x_speed, "freeze": _x_freeze, "split": _x_split, "reverse": _x_reverse, "trim": _x_trim, "title": _x_title, "brand": _x_brand, "end_card": _x_end_card,
    "transitions": _x_transitions, "export_preset": _x_export_preset, "voiceover": _x_voiceover,
    "stabilize": _x_stabilize, "upscale": _x_upscale, "ask": _x_ask,
    "fade": _x_fade, "volume": _x_volume, "mute": _x_mute, "fit_music": _x_fit_music, "preview": _x_preview,
    "remove_music": _x_remove_music,
    # Wave D3 (E3): the CapCut clip edits (agent/prompt/clip_expanders.py)
    "delete_clip": CX.x_delete_clip, "duplicate": CX.x_duplicate, "move_clip": CX.x_move_clip,
    "zoom": CX.x_zoom, "rotate": CX.x_rotate, "adjust": CX.x_adjust,
    # Wave E (F4b): edits by name (agent/prompt/name_expanders.py)
    "remove_feature": NX.x_remove_feature, "clip_length": NX.x_clip_length, "flip": NX.x_flip,
    "retext": NX.x_retext,
    # Wave E (F2): CapCut Canvas and blend modes (agent/prompt/canvas_expanders.py)
    "canvas": KX.x_canvas, "blend": KX.x_blend,
    # Wave E (F3): CapCut's voice changer (agent/prompt/voice_expanders.py)
    "voice_effect": VX.x_voice,
    # Wave E (F1): CapCut clip animations (agent/prompt/anim_expanders.py)
    "animation": AX.x_animation,
}


def expand_auto_edit(it: Intent, f: TimelineFacts, exclusions: frozenset[str]) -> list[Intent]:
    """§2.4 `auto_edit`: the ordered sub-recipes, gated on facts and
    exclusions. A template name in `_template` supplies its own list."""
    tpl_name = it.get("_template")
    if tpl_name:
        tpl = edit_templates().get(tpl_name)
        if tpl is not None:
            ex = exclusions | frozenset(tpl.exclusions)
            out = []
            for name, slots in tpl.recipes:
                if name in ex:
                    continue
                merged = {**slots, **{k: v for k, v in it.slots.items() if k in RECIPE_SLOTS[name] and v is not None}}
                if name == "captions" and it.get("language"):
                    merged["target"] = it.get("language")
                if name == "music" and it.get("mood"):
                    merged["mood"] = it.get("mood")
                out.append(Intent(name, normalize_slots(name, merged), it.score, it.clause))
            out.extend(_target_length(it, f, ex))
            out.append(Intent("_audit", {}, it.score, it.clause))
            return out
    platform = it.get("platform")
    language = it.get("language")
    mood = it.get("mood")
    look = it.get("look")
    ratio = it.get("_ratio") or _platform_ratio(platform)
    out: list[Intent] = []
    speech_known_absent = f.has_transcript and not f.has_speech
    if "tighten" not in exclusions and not speech_known_absent and "remove_silences" not in exclusions:
        out.append(Intent("tighten", {}, it.score, it.clause))
    if look and "color_look" not in exclusions:
        out.append(Intent("color_look", {"look": look}, it.score, it.clause))
    if len(f.v1_clip_ids) >= 2 and "transitions" not in exclusions:
        out.append(Intent("transitions", {"look": "smooth"}, it.score, it.clause))
    if ratio and ratio != f.aspect and "reframe" not in exclusions:
        out.append(Intent("reframe", {"ratio": ratio, "platform": platform}, it.score, it.clause))
    if "captions" not in exclusions:
        out.append(Intent("captions", {"style": it.get("_caption_style") or "ig_chunky",
                                       "position": it.get("_caption_position") or "bottom",
                                       "target": language}, it.score, it.clause))
    if "hook" not in exclusions:
        out.append(Intent("hook", {"text": it.get("_hook_text")}, it.score, it.clause))
    if f.brand_handle and "brand" not in exclusions:
        out.append(Intent("brand", {"handle": f.brand_handle}, it.score, it.clause))
    if "music" not in exclusions:
        out.append(Intent("music", {"mood": mood or "chill"}, it.score, it.clause))
    if "clean_audio" not in exclusions:
        # Loudness only — NOT clean_audio (QA-028). The auto-edit used to add
        # noise_reduce 0.85 to every platform recipe, on clean speech nobody
        # asked to denoise, costing ~10 dB of dialogue under the music bed.
        # Asking for it ("… and remove the background noise") still adds the
        # clean_audio recipe through the grammar. With an export preset the
        # preset records the loudness target, as clean_audio's own rule did.
        if it.get("_lufs") is not None or not (platform and "export_preset" not in exclusions):
            if "loudness" not in exclusions:
                out.append(Intent("loudness", {"lufs": it.get("_lufs"), "_platform": platform},
                                  it.score, it.clause))
    if platform and "export_preset" not in exclusions:
        out.append(Intent("export_preset", {"platform": platform, "_ratio": ratio}, it.score, it.clause))
    out.extend(_target_length(it, f, exclusions))
    out.append(Intent("_audit", {}, it.score, it.clause))
    return out


def _target_length(it: Intent, f: TimelineFacts, exclusions: frozenset[str]) -> list[Intent]:
    """"make this a 30s reel": keep the first N seconds AFTER the tightening
    cuts. `cut_range` sees the post-cut timeline inside the batch (stage 3,
    after every stage-2 cut), so `start=N` trims whatever is left past N; the
    verifier's `duration_leq` then measures the real length. Optional: if the
    tightening already brought the video under N there is nothing past N to
    cut and the handler must not fail the run. Both auto_edit paths (plain and
    template) go through here — the length used to be extracted and dropped."""
    target = it.get("_duration_s")
    if not target or "trim" in exclusions or float(target) >= f.duration:
        return []
    return [Intent("trim", {"range": S.TimeRange(kind="abs", start=float(target), end=None),
                            "_optional": True, "_max_s": float(target)}, it.score, it.clause)]


def audit_expansion() -> Expansion:
    return Expansion(steps=(step("audit_aesthetic", STAGE_AUDIT, "final aesthetic audit of the edit"),),
                     postconditions=(pc("audit_ok", "the aesthetic audit passes"),))


EXPANDER_EXPORTS: tuple[str, ...] = (
    "EXPANDERS", "expand_auto_edit", "audit_expansion", "beat_split_times", "heuristic_hook",
    "RECIPE_COST", "DEFAULT_STEP_COST", "step_cost", "estimate_seconds", "_preset_for",
    "MAX_BEAT_SPLITS", "MIN_SHOT_S", "WORD_EDGE_TOLERANCE_S", "PULSE_SCALE", "PULSE_RISE_S",
    "MAX_TRANSITIONS", "SEAM_SNAP_S",
)
__all__ = [n for n in EXPANDER_EXPORTS if not n.startswith("_")]
