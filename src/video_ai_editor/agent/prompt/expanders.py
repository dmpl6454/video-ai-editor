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

from typing import Any, Callable

from . import slots as S
from .facts import TimelineFacts, VOICE_IDS
from .presets import bed_for_mood, edit_templates, transition_entry, transition_looks
from .recipes import (_PLATFORMS, _RATIOS, FILLERS_STRICT, RECIPE_SLOTS, Context, Expansion, Intent, ask,
                      download, normalize_slots, pc, placeholder, step)
from .costs import DEFAULT_STEP_COST, RECIPE_COST, estimate_seconds, step_cost   # noqa: F401 — re-exported
from .heuristics import (_CANNED_HOOK, MAX_BEAT_SPLITS, MIN_SHOT_S, PULSE_RISE_S, PULSE_SCALE,  # noqa: F401
                         WORD_EDGE_TOLERANCE_S, beat_split_times, heuristic_hook)
from .schema import (ARG_REF, SEAM_SENTINEL, STAGE_AUDIO, STAGE_AUDIT, STAGE_CAPTIONS, STAGE_CUTS, STAGE_EXPORT,
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


def _x_captions(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    style = it.get("style") or "ig_chunky"
    position = it.get("position") or "bottom"
    target = it.get("target")
    max_chars = int(it.get("max_chars") or 42)
    max_chars = min(60, max(16, max_chars))
    notes: list[str] = []
    downloads: list[DownloadNeeded] = []
    pcs = [pc("captions_cover", "captions cover the speech", min_ratio=0.9),
           pc("captions_nonempty", "captions were laid"),
           pc("captions_within_extent", "no caption runs past the video"),
           pc("captions_style", "captions use the requested style", style=style)]
    if f.has_captions:
        notes.append("replacing the existing captions")
    needs_translation = target in ("hi", "hinglish", "es")
    if needs_translation and not f.is_cached("madlad"):
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
        return Expansion(
            steps=(step("auto_caption", STAGE_PREREQ if not f.has_transcript else STAGE_CAPTIONS,
                        "language change or model upgrade needs a fresh transcription",
                        style=style, position=position, target=target, max_chars=max_chars, model=model),),
            postconditions=tuple(pcs), downloads=tuple(downloads), notes=tuple(notes))
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
        if target in ("hi", "hinglish", "es") and not f.is_cached("madlad"):
            if ctx.allow_downloads:
                downloads.append(download("madlad", "translate_captions"))
            else:
                notes.append("translation skipped — the MADLAD model (3 GB) is not downloaded")
                return Expansion(notes=tuple(notes))
    prereq = () if f.has_captions or "captions" in ctx.recipes else (Intent("captions"),)
    return Expansion(
        steps=(step("translate_captions", STAGE_CAPTIONS, "translate the caption track", target_lang=target_arg),),
        postconditions=(pc("captions_language", "captions are in the requested language", target=target),),
        questions=questions, downloads=tuple(downloads), notes=tuple(notes), prerequisites=prereq)


def _x_remove_silences(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    pcs = [pc("speech_preserved", "no kept word was cut"),
           pc("silence_total_leq", "little silence remains", max_total_s=1.0)]
    if f.silence_seconds >= 1.0:
        pcs.insert(0, pc("duration_shrank", "the video got shorter", min_ratio=0.02))
    return Expansion(
        steps=(step("remove_silences", STAGE_CUTS, "cut the silent pauses on v1", track="v1",
                    threshold_db=float(it.get("threshold_db", -30)), min_dur=float(it.get("min_dur", 0.5)),
                    keep_pad=float(it.get("keep_pad", 0.1))),),
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
    if f.has_music and not it.get("_replace"):
        return Expansion(notes=("music is already on the timeline — say 'another track' to replace it",))
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
    duck = True if duck is None else bool(duck)
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
    to_db = float(it.get("to_db", -18.0))
    to_db = min(0.0, max(-40.0, to_db))
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
        else:
            text = _CANNED_HOOK
            notes.append("hook text: generic (no transcript yet) — reply with your own line to change it")
            questions = (ask("hook_text", "What should the hook say?", kind="text", default=_CANNED_HOOK, required=False),)
    text = text[:60]
    return Expansion(
        steps=(step("apply_hook_stack", STAGE_TEXT, "text + punch-in + audio fade in the first seconds",
                    text=text, duration=duration, visual="punch_in", audio="fade_boost"),),
        postconditions=(pc("hook_text_starts_leq", "the hook starts immediately", t=0.5),
                        pc("hook_axes_geq", "the hook works on several axes", n=3)),
        questions=questions, notes=tuple(notes), content_brain=content_brain)


def _x_color_look(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    look = it.get("look") or "teal_orange.cube"
    intensity = float(it.get("intensity", 0.8))
    intensity = min(1.0, max(0.0, intensity))
    return Expansion(
        steps=(step("apply_lut", STAGE_LOOK, f"apply the {look.replace('.cube', '')} look to every v1 clip",
                    clip_id="$v1_all", src=look, intensity=intensity),),
        postconditions=(pc("effect_present", "the look is applied", type="lut", track="v1", all=True),))


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
    target = min(-9.0, max(-24.0, target))
    stage = STAGE_EXPORT if (explicit and "export_preset" in ctx.recipes) else STAGE_AUDIO
    return Expansion(
        steps=(step("set_loudness_target", stage, f"set the export loudness target to {target:g} LUFS", lufs=target),),
        postconditions=(pc("loudness_target_set", "the loudness target is recorded", lufs=target),
                        pc("loudness_within", "the render hits the loudness target", tol=1.0)))


def _x_speed(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    factor = float(it.get("factor") or 1.25)
    factor = min(4.0, max(0.25, factor))
    clip = it.get("clip_ref") or "$v1_all"
    steps = [step("set_speed", STAGE_CUTS, f"play at {factor:g}×", clip_id=clip, factor=factor)]
    if factor < 1.0 and it.get("_smooth"):
        steps.append(step("smooth_slow_motion", STAGE_CUTS, "interpolate frames for smooth slow motion",
                          optional=True, clip_id=clip, factor=max(2, int(round(1 / factor)))))
    pcs = [pc("speed_equals", "the speed matches", clip_id=clip, factor=factor)]
    if clip == "$v1_all":
        pcs.append(pc("duration_between", "the duration matches", factor=factor, tol_ratio=0.05))
    return Expansion(steps=tuple(steps), postconditions=tuple(pcs))


def _x_trim(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    rng = it.get("range")
    if isinstance(rng, str):
        rng = S.extract(f"cut {rng}").range
    if rng is None:
        return Expansion(questions=(ask("range", "Which part should I cut? Reply like 'the first 5 seconds' or 'from 0:05 to 0:12'.",
                                        kind="text"),),
                         steps=(step("cut_range", STAGE_CUTS, "remove the named range", track="v1",
                                     start=placeholder("range"), end=placeholder("range")),))
    start, end = rng.resolve(f.duration)
    if end - start <= 0.05:
        return Expansion(notes=("that range is empty on this timeline — nothing to cut",))
    max_s = it.get("_max_s")
    if max_s is not None:
        # auto_edit's target length: the cut runs AFTER the tightening cuts on
        # the live timeline (stage 3, "structure" — the same stage `shorts`
        # reshapes the footage at), so the honest check is the final length,
        # not a removed-range arithmetic against the pre-plan duration.
        return Expansion(
            steps=(step("cut_range", STAGE_STRUCTURE, f"keep the first {float(max_s):g}s (target length)",
                        optional=bool(it.get("_optional")), track="v1", start=round(start, 3), end=round(end, 3)),),
            postconditions=(pc("duration_leq", "the video fits the target length", max=round(float(max_s) + 0.5, 3)),),
            notes=(f"trimmed to the first {float(max_s):g}s after the cuts",))
    return Expansion(
        steps=(step("cut_range", STAGE_CUTS, f"remove {start:.2f}–{end:.2f}s and close the gap",
                    track="v1", start=round(start, 3), end=round(end, 3)),),
        postconditions=(pc("duration_between", "the duration matches", start=round(start, 3), end=round(end, 3), tol=0.1),))


#: A name card reads longer than a headline: 4 s (the `add_lower_third`
#: handler's own default) against the title's 3 s.
LOWER_THIRD_S = 4.0
TITLE_S = 3.0


def _title_span(it: Intent, f: TimelineFacts, default_dur: float) -> tuple[float, float]:
    """(start, end) for an on-screen card from the `at`/`dur` slots — "at the
    start" is the default, "at the end" backs off from the tail."""
    dur = float(it.get("dur") or default_dur)
    at = it.get("at")
    if at in (None, "start"):
        start = 0.0
    elif at == "end":
        start = max(0.0, f.duration - dur)
    else:
        start = max(0.0, float(at))
    end = min(f.duration, start + dur) if f.duration > 0 else start + dur
    return round(start, 3), round(end, 3)


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


def _x_title(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    from .presets import text_styles
    text = (it.get("text") or "").strip()
    if it.get("name") or it.get("handle") or (it.get("_lower_third") and not text):
        return _x_lower_third(it, f, ctx)
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
    return Expansion(
        steps=(step("add_text", STAGE_TEXT, "title text overlay", **args),),
        postconditions=(pc("text_present", "the text is on screen", contains=text[:120]),
                        pc("overlays_inside_safe_zone", "text stays clear of the platform UI")))


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


MAX_TRANSITIONS = 12
SEAM_SNAP_S = 1.5


def _seams_for(at: Any, boundaries: list[float]) -> tuple[list[float], str | None]:
    """The seams a transition intent names: all / first / last / the seam
    nearest a time (within SEAM_SNAP_S). Second value is a note when the
    request could not be honoured exactly."""
    if not boundaries:
        return [], None
    if at in (None, "all"):
        return boundaries[:MAX_TRANSITIONS], None
    if at == "first":
        return [boundaries[0]], None
    if at == "last":
        return [boundaries[-1]], None
    try:
        t = float(at)
    except (TypeError, ValueError):
        return boundaries[:MAX_TRANSITIONS], f"did not understand where {at!r} is — using every seam"
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
    if not f.v1_boundaries and not (ctx.has_cut_steps and len(f.v1_clip_ids) >= 2):
        return Expansion(notes=("only one clip on v1 — there is no seam to put a transition on",))
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
        seams, note = _seams_for(it.get("at"), f.v1_boundaries)
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
        seams, note = _seams_for(it.get("at"), f.v1_boundaries)
        if note:
            notes.append(note)
        if not seams:
            return Expansion(notes=tuple(notes))
        label = f"{look.name} look"
    duration = min(2.0, max(0.1, duration))
    steps = [step("add_transition", STAGE_TRANSITIONS, f"{types[i % len(types)]} at the {at:g}s seam",
                  at=at, type=types[i % len(types)], duration=round(duration, 3))
             for i, at in enumerate(seams)]
    pcs = [pc("transitions_count_geq", "transitions were added", n=len(steps),
              type=types[0] if len(types) == 1 else None)]
    notes.append(f"{len(steps)} × {label} ({duration:g}s)")
    if f.has_captions and "captions" not in ctx.recipes:
        # A cross-fade shortens the timeline and does not ripple overlays, so
        # existing captions would overhang; re-lay them after the seams move.
        steps.append(step("add_caption_track", STAGE_CAPTIONS, "re-lay captions after transitions shortened the timeline",
                          style=f.caption_style or "ig_chunky", position="bottom"))
        pcs += [pc("captions_within_extent", "no caption runs past the video"),
                pc("captions_sync", "captions stay in sync across cuts", tol=0.1)]
        notes.append("captions re-laid so they follow the shortened timeline")
    return Expansion(steps=tuple(steps), postconditions=tuple(pcs), notes=tuple(notes))


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


def _x_voiceover(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    text = (it.get("text") or "").strip()
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
    clip = it.get("clip_ref") or "$v1_all"
    return Expansion(
        steps=(step("stabilize", STAGE_CUTS, "stabilise the footage", optional=True, clip_id=clip),),
        postconditions=(pc("clip_src_changed", "the clip was stabilized"),))


def _x_upscale(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    clip = it.get("clip_ref") or "$v1_all"
    factor = int(it.get("upscale_factor") or 2)
    return Expansion(
        steps=(step("upscale", STAGE_CUTS, f"upscale {factor}× with Real-ESRGAN", optional=True, clip_id=clip, factor=factor),),
        postconditions=(pc("clip_src_changed", "the clip was upscaled"),))


def _x_ask(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    return Expansion()


EXPANDERS: dict[str, Callable[[Intent, TimelineFacts, Context], Expansion]] = {
    "transcribe": _x_transcribe, "captions": _x_captions, "translate_captions": _x_translate,
    "remove_silences": _x_remove_silences, "remove_fillers": _x_remove_fillers, "tighten": _x_tighten,
    "shorts": _x_shorts, "reframe": _x_reframe, "music": _x_music, "duck": _x_duck, "beat_sync": _x_beat_sync,
    "hook": _x_hook, "color_look": _x_color_look, "clean_audio": _x_clean_audio, "loudness": _x_loudness,
    "speed": _x_speed, "trim": _x_trim, "title": _x_title, "brand": _x_brand, "end_card": _x_end_card,
    "transitions": _x_transitions, "export_preset": _x_export_preset, "voiceover": _x_voiceover,
    "stabilize": _x_stabilize, "upscale": _x_upscale, "ask": _x_ask,
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
        out.append(Intent("clean_audio", {"lufs": it.get("_lufs"), "_platform": platform}, it.score, it.clause))
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
