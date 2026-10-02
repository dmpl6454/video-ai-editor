# Changelog

All notable changes to Video AI Editor. Versioning follows the `VERSION` file
at the repo root, surfaced at `/api/version` and in the editor's top bar.

## 0.8.2

A fix-and-foundation release on top of 0.8.0. Opening a video now just works,
Windows exports and cancels behave, and the first piece of the Editor Brain
ships switched off. Nothing changes in the editor unless you turn that
switch on. (0.8.1 was never published; everything in it is here.) Projects
from 0.8.0 open unchanged.

### Open and import
- Picking a video, audio or picture file in **Open** now imports it into the Media panel and says so ("IMG_0623.MOV is a video, not a project"), instead of refusing it as a bad `.vae` project.
- The project menu has an **Import media…** entry that opens a media picker. A saved `.vae` still opens as a project, and anything else still gets its plain explanation.

### Export and sound
- Hall voice effect: previews and exports no longer fail on ffmpeg 6.1.
- NTSC (29.97 fps) exports deliver exactly the planned length of sound; a one-sample shortfall that ffmpeg 9 exposed is padded with silence, and no placed sound moves.
- A timeline with very many clips no longer fails to export on Windows ("command line too long"); the filter graph is passed in a file when it would not fit.
- Windows: cancelling a preview, proxy build or export now stops the whole ffmpeg process tree. Before, the encoder could keep running at full CPU after a cancel.
- VideoToolbox export: if the delivered bitrate lands more than 10 % under the target, one corrective pass runs and the closer result is kept. Deliveries already within 10 % are unchanged.
- ffmpeg 8 and 9 are the supported versions; the README says so.

### Editor Brain (off by default)
The first piece of an editing brain that plans from what is actually in your footage, on your Mac, with no key and nothing leaving the machine. Ask the Prompt bar for "a 45-second reel" or "tighten this podcast" and get one reviewable plan: fillers and dead air removed on quiet moments, camera cuts to whoever speaks next, hidden jump cuts, captions for every speaker, and a Plan tab that explains each step. Apply is one step and one Undo; each run is saved as a version you can restore, and versions travel with the project.

It is off until you turn it on. There is no Settings switch yet: set `VAI_BRAIN_ENABLED=1` before starting the app, or `"brain": {"enabled": true}` in the settings file. With it off, the Prompt bar behaves exactly as in 0.8.0.

## 0.8.0

The pro-editor release. Working editors drove the app the way they cut every
day, and this release is what they asked for: CapCut's speed curves, freeze
frames, clip animations, canvas backgrounds, blend modes and voice changer;
an export that plays exactly the frames the preview shows; a Prompt bar that
shows you what it will change before it changes anything; and a long list of
fixes to cuts, sound, overlays and transitions. Projects from 0.7.x open
unchanged.

### Import and organise
- Two imports with the same name no longer overwrite each other, and the media library shows each file's original name.
- Your footage keeps its frame rate: 23.976, 24, 25, 29.97, 50, 59.94 and 60 fps sources are no longer forced to 30 fps.
- Portrait video keeps its full resolution, 4K stays 4K, and HDR footage is tone-mapped to standard colour instead of coming out washed out.
- Anamorphic footage (PAL, HDV, DV) previews and exports at its real shape instead of squeezed.
- HEIC photos from an iPhone import like any other picture.
- Adding media with the playhead inside a main-track clip inserts it there and splits that clip, as CapCut does, and the drop label says so.
- An imported voiceover keeps its file name in the media library, on the timeline and in the Inspector.
- Audio panel › Add music… always puts the file on the Music track, whatever the "Add imports to the timeline" setting says.
- Uploads show their progress and can be cancelled, and upload errors say what went wrong in plain words.
- Projects have names you can rename instead of ids.
- A custom LUT is saved inside the project file, so the project opens and exports on another Mac, and Save warns if a LUT is missing.
- MPEG-TS, M2TS, MPG and VOB files added by an AI agent are converted like an upload, and a photo added that way becomes a 5-second clip you can lengthen.

### Cut, trim and arrange
- Every cut is frame-exact: split, trim, move and every automatic cut land on a frame boundary, so a split no longer exports a duplicate frame and picture and sound stay in sync.
- The clock, the ruler and every timing field show SMPTE timecode, and you can type a timecode into any of them.
- A timeline toolbar holds split, freeze, delete, duplicate, snap, zoom and fit, with real move and trim cursors and zoom anchored on the cursor.
- Snapping is visible and includes markers.
- Lock track works, deleting a multi-selection ripples the titles and captions with it, and duplicate and paste create independent clips.
- Titles, stickers, captions and picture-in-picture stay over the footage they were placed on when you delete, trim, speed up, freeze, duplicate, reorder or insert clips on the main track, cut a range, or remove silences or filler words.
- One placed past the end of the video, or over a gap, stays where you put it.
- Transitions stay on their cut when you delete, trim, cut, reorder, duplicate, drag or change the speed of a clip on the main track, and a transition whose clip is deleted goes with it.
- Adding a transition where there is no cut is refused, and the message lists the cuts.
- A clip's detached sound stays with its picture, including a J or L cut offset you set, when you trim, delete, slow down, freeze, duplicate or drag an earlier clip.
- Dragging, resizing or turning an animated picture-in-picture in the preview adds a keyframe at the playhead instead of deleting the animation.
- Splitting a clip with keyframes keeps its animation where it was.
- Typing a later Start for a main-track clip, or nudging the last clip right, is refused with a message saying main-track clips sit end to end, instead of opening a black gap.
- Splitting (⌘B) or trimming to the playhead (Q/W) on a voiceover, music or audio clip cuts exactly at the playhead when there are transitions on the main track.
- After transitions, the Inspector's Start, End and Duration match the ruler, and a typed Start lands where you typed it.
- Timelines with more than 200 different clips preview and export.
- History shows plain names and timecodes: where each clip landed, the transition and caption style names the panels use, and no internal ids.
- Redo brings back the edit's own name in History.

### Speed, curves and freeze frames
- Speed curves, like CapCut's: open Speed in the Inspector, switch from Normal to Curve, and pick Montage, Hero, Bullet, Jump Cut, Flash In, Flash Out, or Custom, where you drag, add and remove points.
- The clip's new length is shown before you commit, the export plays exactly the frames the preview shows, and Keep pitch works on curves.
- Freeze frame holds the frame under the playhead for 3 seconds, from the toolbar, the clip's right-click menu or the Prompt bar, and pressing Freeze again on a still makes it longer.
- Picture-in-picture clips take speed, speed curves, freeze frames and reverse, picture and sound, in the preview and the export.
- Every retimed clip shows what it does on the timeline: "2.0x", "0.5x", "Hero", "Curve" or "Freeze".
- Splitting, trimming or cutting a clip with a curve, a freeze or reverse keeps exactly the frames it showed, in order.
- The Normal speed slider on a curve clip starts at the curve's average speed, and a curve clip's Duration field sets the length you type.
- Keep pitch no longer fails an export that has a sped-up or slowed-down clip.
- Reversed clips at 2x or 4x no longer shift everything after them by a frame.
- Speeding up or slowing down a captioned clip moves its captions, titles and stickers to where the words now play, and AI › Smooth slow-mo does the same.
- Speeding up a main-track clip, or giving it a curve, ends the music with the picture instead of over a black tail.

### Text, titles and captions
- Hindi, Arabic and other complex scripts export with the right letter shapes and joining, matching the preview.
- Preview and export place text identically, including outline weight, rotation, scale and position keyframes, letter and line spacing, background box and shadow.
- Text and stickers appear on the right frame in 50 and 60 fps exports of a 24, 25 or 30 fps project, and animated ones move every frame.
- A title with emoji exports its emoji without an internet connection, and the emoji in the preview and the sticker picker show offline too.
- Double-clicking a title in the preview or on the timeline puts the cursor in its Text box.
- New text, and the Title box, Subtitle band, Side label, Quote and Neon styles, go above the captions when captions are already there.
- Text added over another text at the same time goes on its own new Text track, and a sticker added over another goes on its own sticker lane, so both can be selected and trimmed.
- Text on higher tracks draws on top in the preview, as it does in the export.
- TikTok-style captions come in short phrases on real word timing, and changing the caption style keeps your manual edits.
- Generate captions captions every clip that speaks, the voiceover and speech on a picture-in-picture lane, so both people in a stacked podcast get captions.
- Captions made from a voiceover stay on its words when you later add a transition or edit the main track, and moving or splitting the voiceover moves its captions with it.
- Captions, Remove silences and filler removal follow speed-curve and reversed clips exactly.
- Hinglish captions are transliterated on your Mac instead of needing a 3 GB download.
- AI › Cut range, Super text, Lower third and AI voiceover use the times you see on the ruler.
- The Transform x and y fields are wide enough for four-digit positions.

### Animations, canvas and blend modes
- Clip animations: In, Out and Combo animations (Fade, Zoom, Slide, Rotate, Spin, Bounce, Blur and more) for main clips, overlays and stickers.
- Canvas: fill the bars around a letterboxed clip with a colour, a blurred copy of the clip or a picture, and the background stays still while the clip moves or fades over it.
- Blend modes for overlay clips: Screen, Multiply, Overlay, Add and ten more.
- Mirror and Flip for any clip, overlay or sticker, with the older Flip effects on the same buttons.
- Keyframed position, zoom, rotation and opacity export exactly as keyed on a clip anywhere on the timeline, sped up, slowed down, on a curve, reversed or frozen.
- Keyframed opacity and rotation on an overlay clip animate in the export instead of sticking at their last value.
- An overlay's Out animation finishes exactly where the overlay ends.
- A picture-in-picture with a blend mode exports its Zoom, Spin, Bounce or keyframed size animation, and a spinning one no longer shows a black square behind it.
- Stickers and picture overlays keep their true colours on every video.
- Inspector › Framing says where the bars really are, and "no bars" when the clip fills the frame.
- Inserting a clip into the main track no longer moves titles, stickers, captions and picture-in-picture that sit over a gap or past the end of the video.

### Sound and voice
- Voice changer: Robot, Echo, Chipmunk, Deep, Monster, Telephone, Megaphone, Radio, Underwater, Vibrato and Hall.
- Detach audio for J and L cuts, volume keyframes, solo, per-clip fades, gain above +6 dB, and ducking under voiceover and dialogue with a real depth.
- Waveforms are accurate, and the preview is loudness-matched to the export.
- The Voice panel says when a clip has no sound instead of silently doing nothing.
- A voiceover you import or record starts at the playhead, even after transitions.
- A voiceover, music or audio clip that runs across or past main-track transitions plays to its end in the export, and the timeline block and the Inspector show its full length.
- Music laid to the end of the video ends with the picture, with its fade-out, when the main track has transitions, and "Trim to video" ends the music exactly with the picture.
- Two voiceover lines, AI voiceover lines or songs with a short gap between them no longer play over each other after a transition.
- Detaching the sound of two neighbouring clips with a transition between them keeps the second sound on time.
- Splitting a clip with the Telephone, Radio or Megaphone voice effect no longer puts a click at each split, and pitch and vibrato effects no longer leave a gap at a split.
- Soloing a track or changing the music ducking updates the preview's sound straight away.
- Exporting a WAV with Loudness Off no longer distorts when a loud picture-in-picture plays over the main track.
- Exports with dynamic music reach their loudness target instead of coming out 1-2 LU quiet.
- Exports at 29.97 and 59.94 fps no longer lose the last few milliseconds of sound.
- "Cut away the dead air" no longer leaves tiny slivers of silence where a silence crossed a cut, and Remove silences reports an error when its check fails instead of saying there was nothing to cut.
- AI panel › Cut to the beat leaves shots of at least 0.8 seconds by default.
- Exporting at a different frame rate from the project's no longer adds a black frame and a short audio dropout at cuts, or ends on a black frame with a short silence.

### Colour and effects
- The Effects panel can import your own LUT (.cube), and "Apply look to all clips" copies a clip's look to every main-track clip, replacing their looks instead of stacking them.
- An imported LUT keeps its own name in the Effects panel and in History.
- Importing a .cube checks the file first: a 1D LUT, a cut-off file, an unsupported size or old Mac line endings is refused with a message saying what is wrong, and a LUT whose comments use accented or Cyrillic letters is accepted.
- A LUT whose file name has an apostrophe, comma, semicolon or brackets no longer breaks preview and export.
- If a look already on a clip cannot be read, preview and export name that look and say to remove it, instead of blaming the clip.
- Applying a filter to every clip no longer stacks a second copy on a clip that already has it.
- Clips with and without colour information can share the main track and keep their colours in the export, including under a Canvas Blur background and picture-in-picture.
- In the macOS app, the preview shows the main video and overlays with the same colours as the export, instead of slightly brighter.
- An effect setting outside its range is refused instead of being kept and breaking every preview and export.
- The green-screen key colour must be a colour, and a project file holding anything else opens with the default green key.

### Preview
- Previews always show your latest edit, superseded renders are cancelled, and only the part of the timeline you changed re-renders.
- A small "≈" mark appears on the preview when what you see is a close approximation of the export rather than an exact match, and hovering over it says why.
- The regular preview no longer plays loud overlapping sound too quietly the first time you preview a project or after you add a loud music track.
- Editing while an export or preview is being made can no longer mix two versions of the project in one file.
- Crossfades start on the exact frame in both the preview and the export, and previews of clips whose frame rate differs from the project's pick the same frames the export shows.
- **Instant preview (beta)** is a new choice in Settings: Auto, Always or Off (the default).
- When it is on, cuts, splits, trims, moves, deletes and undo show up in the preview immediately, frame-exact and with sound in sync, while effects it cannot draw yet still come from the rendered preview.
- Instant preview plays at the project's loudness target, as the rendered preview and the export do.
- Loud mixes are limited exactly as the rendered preview limits them, and one loud moment in a long clip marks only that stretch.
- Music and voiceovers imported as audio files play in Instant preview.
- Pressing Play while a new clip's sound is still loading waits for it briefly, and a stretch whose sound is not ready yet is marked "≈ Sound loading" instead of playing silent.
- Videos whose sound starts a moment after the picture play in sync.
- A clip whose preview copy fails keeps showing exact frames from its original file, a slow or failing preview file no longer freezes the picture, and a stuck playback restarts itself.
- When a clip's preview data keeps failing to load, or the disk is full, the preview switches to its slower fallback instead of waiting under a spinner.
- Pressing Play right after moving the playhead no longer sometimes stops by itself, and turning Instant preview on while the video plays pauses cleanly.
- J and repeated L say that reverse and fast shuttle need Instant preview off, instead of silently doing nothing.
- The "≈" mark says in plain words what is approximate, such as "Canvas behind a moved or resized clip", and appears over sped-up clips with Keep pitch off and over reversed overlays.

### Export
- A proper Export dialog with real resolution, frame rate, quality, loudness target and a size estimate.
- Platform presets set a real bitrate, loudness and frame rate, and every export keeps its own file.
- A timeline with sped-up or speed-curved clips no longer ends on a black, silent frame.
- After you cut the main track into pieces, a picture-in-picture, keyframed text and animated titles or stickers export on the right frame.
- A clip one frame long keeps its picture instead of turning black.
- A split in a clip filmed at a different frame rate from the project is exact in the export.
- A transition after a sped-up or slowed-down clip is applied instead of silently skipped.
- When the disk is full, or the encoder is stopped because memory runs low, the error says so instead of blaming your clip.
- An export that would have no picture fails with a clear message instead of producing a sound-only file.
- If the app quits during an export, the leftover encoder is stopped and its partial file is deleted the next time the app starts.

### The Prompt bar (no API key) and the assistant
- **Preview, then apply.** Without a Claude key, the Prompt bar now shows what it would change before changing anything: each change on its own line, with clip names and times, under the words "Nothing has changed yet".
- Press Apply (Enter) to make the edit or Change (Esc) to go back to your sentence, and one ⌘Z undoes an applied edit.
- The list names every change, and a card with many changes opens "and N more changes" so you can see all of them; the chat pane lists them too.
- If the timeline changes while a card is open, Apply shows a fresh preview instead of applying the old one, and a new sentence typed while a card is open replaces it.
- Typing "yes" or "no" in the bar while a card is open applies or drops it, and "undo" typed while a card is open drops the card and undoes nothing.
- New setting: Settings › Prompt bar › "Ask before applying Prompt bar edits", on by default; turn it off to apply Prompt bar edits straight away.
- A run that did something you did not ask for, or did not do all of it, is undone in full and asks what you meant, leaving nothing in History.
- Apple Intelligence works as an on-device brain on macOS 26 and later when it is turned on in System Settings.
- The Prompt bar's Undo only appears while the prompt's change is the latest edit.
- The Prompt bar understands the new tools: speed curves ("add a hero speed ramp", "flash in speed curve on the second clip"), freeze frames ("freeze frame at 5 seconds"), animations ("make the logo pop in and spin out"), canvas ("put a blue background behind it"), blend modes ("set the overlay's blend to screen"), voice effects ("make the voice on clip 2 robotic") and Mirror and Flip.
- Many more everyday phrasings make the right edit or ask one clear question, and common typos such as "spead up teh second clip" are understood.
- Speed requests read the way you say them: "50% faster" is 1.5×, "25% slower" is 0.75×, "three quarter speed" is 0.75×, "reset the speed" is 1×, and "slow it down to 2x" asks instead of speeding up.
- Level requests move from the current level: "bring the music down 4 dB" lowers it by 4 dB, "clip 2 volume +2" raises clip 2, "mute everything except the voiceover" mutes the music too, and "the music's too loud" lowers the music.
- Requests naming clips change exactly those clips: "speed up clip 2 and clip 3", "delete clips 1-2", "every clip except the first", "the clip under the playhead", "slow down the pour shot" and "clip2" without a space.
- In a request with several steps, "it" means the clip named earlier, and a step that could not be done is never listed as done.
- Title requests restyle or retime your title and keep its words: "make the title red", "title font size 80", "put the title at the top", "make the title last 5 seconds", "change the Summer Trip text to Winter Trip" and "make the title say …".
- New titles take the look you ask for, such as "small", "in a black box", "with no outline", a font name, "fades in" or "on clip 2", and quoted words like 'Like & Subscribe' are kept exactly.
- Caption requests change the captions' look without rebuilding them: "make the captions yellow", "make the subtitles bigger", "captions at the top please".
- Transition requests act on the transition you name: "change the crossfade to a wipe" changes that one, "make all transitions 1 second long" keeps their types, "put a wipe between clip 1 and clip 2" adds one wipe, and "take the transitions out" removes them.
- Removal requests remove what they name: "turn the robot voice off", "take the black and white off", "remove the fade in" keeps the fade out, "remove the screen blend" goes back to Normal, and "remove all animations" takes them off.
- Cut requests cut what they say: "keep only the first 10 seconds" keeps that part, "cut the first 2 seconds of clip 3" cuts inside that clip, "split clip 3 at 10 seconds" splits there, and a cut that would empty the main track is refused unless you say "all".
- Music requests stay on the music: "music fade in 1s and fade out 3s", "cut the music at 8 seconds", "replace the music with silence" and "remove the music ducking" never touch the picture or turn ducking on.
- "Add captions" captions every clip that speaks and the voiceover, and "remove the silences" keeps a B-roll shot that has no sound of its own.
- Cuts, deletes, speed changes, freezes and reorders work on a project with a picture-in-picture or B-roll overlay, and the overlay moves with its picture.
- "Make this a 30s reel" ends on a complete sentence, and "add transitions between every clip" covers every clip.
- A request that would change nothing says "Nothing to change", a request the bar cannot apply asks one clear question, and on a full disk the bar says the disk is full.
- A new request typed after a question is planned as a request, not taken as the answer, and an unanswered question left over from before a restart no longer blocks a new instruction.
- After "I undid that", picking an option carries out that edit or asks its next question, and never runs the same wrong edit again.
- A sentence that only starts with "undo" or "revert", such as "revert clip 2 to normal speed", previews that change instead of undoing your last edit.
- Edits from the chat assistant or an AI agent are refused while a Prompt bar run is working, instead of reporting success and then vanishing, and long agent tools no longer freeze the editor.
- Changing a main-track clip's speed, In or Out through the chat assistant or an AI agent keeps the clips end to end.
- Model downloads always state their size and ask first, and Chat is docked in the right panel and closed by default.
- Projects made by a newer version open safely: an effect, blend mode or background this version does not know is skipped and logged.

### Keyboard and accessibility
- Eight tools in the left rail: Media, Audio, Text, Stickers, Effects, Transitions, Captions and AI, each opening in one tool panel, with Help, Keyboard shortcuts and Settings at the foot of the rail.
- A calmer top bar: safe zones are in the Ratio menu, a single activity chip shows a recording or a captions run with its Stop or Cancel button, and the bar tidies itself as the window narrows.
- The Media, Audio, Text, Effects and Captions panels list their AI tools, and clicking one opens it in the AI panel with a back button.
- The Inspector has a row of jump buttons at the top, and its sections also show the names CapCut uses (Background, Voice changer, Adjust, Blend mode).
- ⌥1 … ⌥8 show a rail panel and put focus on it, ⌥\ hides or shows the tool panel, ⌥9 shows the Inspector and ⌥0 the Chat.
- F6 and ⇧F6 move between the top bar, the rail, the tool panel, the Prompt bar, the timeline, the right panel and the rail's foot.
- ⌘E opens Export, ⌥⌘K opens Keyboard shortcuts, and ⌥T adds text at the playhead and puts you straight into it.
- C (Final Cut, CapCut) or D (Premiere) selects the clip under the playhead, and ↑ and ↓ select the previous or next clip and move the playhead to it.
- Space, Enter, ⌘Z, J, K, L and N keep working wherever focus is, including on a rail tab, a toolbar button, a checkbox, the Speed section, and right after you apply a transition or add a clip.
- Tab reaches every button in the app, and Esc in an empty Prompt bar or Chat box returns to the timeline.
- Every icon button has an accessible name, menus work from the keyboard, and text meets 4.5:1 contrast.
- A screen reader hears why a Prompt bar preview was refreshed and what it would change, and the "Undid …" reply names the clip.
- The Inspector's Video fade in and Video fade out fields apply when you press Enter, and the left-rail tooltip no longer blocks the button under it.
- Disabled toolbar buttons look disabled, the Run key hint has better contrast, and long media names are tidy in a narrow window.

### Security and privacy
- A web page open in your browser on another local address can no longer create, change, upload into or delete your projects; only the editor's own window can.
- The preview link only serves this project's own preview renders.
- Opening a project file is limited to what it can really unpack to and to the free space, so a malformed file can no longer fill the disk.
- Opening a project file can no longer make the next Save copy a private file from your Mac into it.
- Templates you save are kept in your user folder, not inside the app, so saving one no longer damages the app's signature.
- Nothing in the Prompt bar downloads a model or a voice without your yes.

### Fixes
- When the disk is full, Save no longer leaves a broken project file or overwrites your previous save, an edit that could not be saved does not stay on the timeline, and every action says the disk is full instead of "internal server error".
- Undo and Redo on a full disk no longer lose the Redo history, and a damaged step in a project's undo history stops Undo there with a plain message.
- A project file with a damaged meta.json no longer breaks the project list and New project.
- Viewing the history of a project that does not exist no longer creates an empty project.
- If the editor's engine stops, an offline banner says so and the app recovers cleanly when it is back.
- History shows an added clip, sticker, text or transition at the time the ruler shows after a transition.
- Inspector › Timing for a music bed that ends with the video shows End and Duration where the music actually stops.
- An Inspector time field shows the clip's real value again when a typed value changes nothing.
- The Transitions panel targets the cut under the playhead, and its arrows move the playhead onto the cut, so a transition never replaces one on a different cut.
- With transitions on the main track, clip names on the timeline are no longer covered by the transition icons.
- The Inspector's jump buttons no longer hide the section title, and Inspector › Canvas › Colour keeps "Custom" and the colour code on one line.
- The folded Prompt bar result no longer covers the top of the preview, and a preview card never takes the keyboard from a field you are typing in.
- Undo updates the Canvas tab correctly.
- Typing a clip's start as the previous clip's end on a 25 fps project no longer leaves a one-frame flash of the wrong shot at the end of the export.
- Instant preview (beta): pressing Play again after playback stopped at the end restarts from the start every time. Before, on a timeline whose start was no longer buffered, the picture could stay on the last frame (or show frame 0 for a second) and playback stop by itself.
- Exporting a WAV or M4A with Loudness Off no longer clips when a single clip's gain pushes it past full scale (for example +12 dB on a loud clip): the sound goes through the export's peak limiter, and a clip that stays under the ceiling is delivered exactly as it was.
- Instant preview (beta): when a clip's preview data cannot be built at all (the drive holding the cache dropped out, or ffmpeg is missing), the preview now switches to its fallback at once instead of retrying the same failed build every time it asks for the next piece.
- Generate captions on a project that already has a transition now captions every word a voiceover or picture-in-picture speaks during the cross-fade. Before, a caption that started and ended inside the dissolve (a sped-up voiceover's word, a short phrase) was silently left out.
- Opening a large project file no longer freezes the rest of the editor while it unpacks (about two seconds per gigabyte of media); the preview, thumbnails and the timeline keep responding.
- A damaged project file whose media list points at the project's own timeline file, or at the whole archive, no longer opens as an empty project; the timeline is kept, and if it cannot be, the open is refused instead.

- Prompt bar: "make the voiceover 3 dB quieter", "lower the narration by 3 dB" and "make the narration louder" change the voice-over lane's level; they no longer offer a 60 MB voice download or change the main-track clips.
- Prompt bar: a look, adjustment or other clip edit named by the footage ("make the kitchen before shot black and white", "the before shots", "re_kitchen_before and re_living_before") lands on those clips only, and a name that matches nothing asks which clip instead of changing every clip.
- Prompt bar: "add a title 'Hold it' on clip 4" stacks a second title on a clip that already has one instead of refusing; "add a title 'BEFORE' on clip 2 and 'AFTER' on clip 3" adds both titles on their clips.
- Prompt bar: "make clip 2 and clip 4 black and white" (the noun repeated) edits both clips; "silence the first two clips" mutes them instead of cutting the pauses out of every clip; "take the warm off clip 1" removes the look; "un-mute clip 3" unmutes it.
- Prompt bar: "keep the logo on screen for the whole video" and "show the logo until the end" stretch the sticker to the end of the video instead of asking for a watermark handle, and when combined with a corner request the position note is still shown.
- Prompt bar: "lower the music while the coach is talking", "whenever someone is talking" and "under his voice" duck the music instead of lowering it by a fixed amount.
- Prompt bar: "remove the 2 seconds after the playhead", "the second before the playhead" and "from 1s to the playhead" cut exactly that span instead of everything to the end or from the start.
- Prompt bar: a stated amount is the amount — "drop the bed to -30", "voice up 3, music down 3", "over half a second", "slow it to half", "until it's 2 seconds long", "brightness +0.2", "saturation by half" and "duck … to -20" no longer fall back to the default step.
- Prompt bar: "scale clip 1 down to 80%" zooms to 80% instead of 180%; "cut out the part between clip 1 and clip 3" removes clip 2 only (and says so when the two clips sit next to each other); "at the very end" and "from the playhead" place a title where they say.
- Prompt bar: "freeze the first frame of clip 2" freezes that clip's frame, "split every clip in half" splits each clip, "mirror the middle clip vertically" flips one axis, "transition between clips 1-2 only" adds one transition, and "fade the music in over 1 second and out over 3" sets both fades.
- Prompt bar: "auto captions in yellow" and "add subtitles and make them bold and big" lay the captions with that look in the same step; "make the warm look on clip 1 weaker" steps that clip's look; "remove all looks" is no longer refused.
- Prompt bar: "add a 3 2 1 countdown at the start of clip 2" adds a 3 · 2 · 1 countdown (three one-second cards); a picture-in-picture request explains how to add one from the PIP / overlay lane instead of the generic menu.
- Prompt bar: everyday verbs and forms are understood — "nuke the last clip", "bin clip 1", "dupe clip 3", "make clip 2 go away", "throw away everything after 0:10", "reverse the order", "razor at 3.25", "lop off the last half second", "keep 3s through 9s", "keep everything from 4 seconds on", "back to widescreen", "rename Day One to Day Two", timecodes such as 00:00:07:15, "the last clip 20% brighter", the typo lines "trm teh frist 2 secs" and "mkae clp 3 balck adn wihte", and "the title should show for the whole video" keeps the retime instead of undoing it.
- Prompt bar: a preview step that hits a full disk says the disk is full instead of showing an error number.
- ⌘, (Ctrl+, on Windows) opens Settings from the Prompt bar's text field and brain button too, like ⌘E.

### Known issues
- Instant preview (beta): a clip with the Hall voice effect can show the "≈ Limiter on loud sound" mark even when the sound is nowhere near the limit.
- The Prompt bar still asks a question instead of editing for some phrasings; rewording the sentence, or picking one of its options, gets the edit.
- A full manual VoiceOver pass of the new panels is still owed, so a few controls may read less clearly than they look.
- The Prompt bar does not yet read "shave a second off each clip", "make it 21:9" or "make the canvas 1280 by 720"; use Inspector › Timing and Inspector › Canvas for those.

### Upgrading from 0.7.3

Your projects open unchanged, and nothing about your media, settings or
projects is rewritten. The first time you open a project, cached previews
re-render once, because previews and exports now share one exact frame grid.
Two behaviours are settings rather than surprises: Instant preview is an
opt-in beta, Off by default, under Settings; and the Prompt bar's
"Ask before applying Prompt bar edits" is On by default, so a key-free prompt
shows you a preview card and changes nothing until you press Apply, and you
can turn it off under Settings › Prompt bar to get the old immediate
behaviour.

## 0.7.3

### Security
- **A web page could read local files through the running editor (DNS rebinding).**
  The local server's Host allowlist accepted any name that merely *started*
  with `127.` — `127.attacker.example`, `127.0.0.1.nip.io` — which are DNS
  names an attacker controls. After rebinding such a name to 127.0.0.1, a
  malicious page became same-origin with the editor and could create a
  session, point a sticker at any file on the Mac, and download its bytes
  through `/api/sessions/{sid}/sticker/{id}`. Chrome's local-network prompt
  blocks this by default; Safari and Firefox did not. The Host check now
  accepts only real IP literals plus `localhost`, so a rebinding request gets
  **421**. As defence in depth, the sticker route now serves a file outside
  the project folder only if its bytes are actually an image (PNG, JPEG, GIF,
  WebP).
- **`/mcp` executed tool calls sent as `text/plain` or a form.** Those content
  types need no CORS preflight, so any cross-origin page could trigger tools
  (it could not read the answer). `/mcp` now requires
  `Content-Type: application/json`, which forces the preflight the CORS policy
  refuses, and answers anything else with **415** before a tool runs.

Upgrade recommended for everyone running 0.7.2 or earlier. No settings,
projects or media change.

## 0.7.2

### Fixed
- **Opening a saved project could fail with "415 Unsupported Media Type".**
  `POST /api/load_project` judged the upload by its file NAME — a case-sensitive
  `.vae` / `.zip` suffix check — and never looked at the bytes. So the same valid
  project was refused the moment its name did not match: renamed in uppercase
  (`P.VAE`), saved without an extension, or — the case the packaged app hit on
  its own — saved by WebKit as `<id>.vae.txt`, because the `.vae` download was
  served as `text/plain` (Python's `mimetypes` has no entry for `.vae`) and
  macOS appends `.txt` to an unknown-extension text attachment. Projects are
  now recognised by CONTENT: a zip carrying `manifest.json` is a project
  whatever it is called. `.vae` downloads are served as `application/zip`, the
  packaged app saves projects through the native Save-As dialog exactly as
  exports already do (the WKWebView ignores the anchor's `download` attribute
  and navigates instead), and the error toast shows the server's reason from
  the standard error envelope (`error.message`) instead of the bare status
  line — the frontend had been reading `detail`, which the envelope never
  carries.

## 0.7.1

### Changed
- **The iPhone companion and local-network pairing are TEMPORARILY DISABLED, so
  the desktop editor ships standalone.** Nothing was deleted: `api/pairing.py`,
  `api/pair_routes.py`, `api/auth.py`, the frontend Phone panel, the `mobile/`
  iOS app and every one of their tests are all still here, unchanged in
  behaviour. They are gated off behind ONE flag —
  `PHONE_PAIRING_ENABLED` in `src/video_ai_editor/api/pairing.py`, read through
  `pairing.phone_pairing_enabled()`, `False` by default and `True` when the
  environment variable `VAE_PHONE_PAIRING` is set to `1`/`true`/`yes`
  (case-insensitive). Turning it back on is that one variable and nothing else;
  with it on, the behaviour is exactly 0.7.0's. The reason to ship this way is
  that pairing is the one feature whose value depends on a second device and a
  cooperative local network, and a desktop editor should not ask a first-time
  user to debug Wi-Fi isolation before it cuts a clip. A future release turns it
  back on rather than re-implementing it.
- **`GET /api/version` now reports `phone_pairing: bool`** (the live value of
  `phone_pairing_enabled()`, honoured per call, not cached at import). That key
  is the only channel the frontend consults to decide whether the phone
  affordance exists at all, so one flag moves the backend posture and the UI
  together. `version` and `build` keep their exact meaning and types.
- **While the flag is off:** every `/api/pair/*` route answers **404** with the
  app's standard error envelope (`{"error":{"code":"NOT_FOUND","message":…}}`),
  saying the phone feature is not available in this build; `lan_enabled()` is
  `False` whatever `settings.json` says; `set_lan_enabled(True)` refuses instead
  of writing; `auth_required()` is `False` unless the socket is genuinely bound
  to a public interface; and `desktop.py` binds loopback regardless of the
  persisted setting. The LAN posture ladder (a request arriving on a
  non-loopback interface still arms authentication and the path allowlist) is
  untouched — a flag that could disarm auth in front of an already-public socket
  would be a security regression, not a shipping decision.
- **Your existing pairing settings are left alone.** `settings.json` — its
  `lan_enabled` value and every remembered device — is never rewritten, cleared
  or migrated by this change; it is simply not consulted while the feature is
  off, and it is read as-is again the moment `VAE_PHONE_PAIRING=1` comes back.
  Turning the flag off is not an unpair.

### Docs
- README, CLAUDE.md and `docs/PROMPT_EDITOR.md` now say the phone is
  temporarily unavailable in this build wherever they described pairing or
  answering a clarification from the phone, and name `VAE_PHONE_PAIRING` /
  `PHONE_PAIRING_ENABLED` as the one-line way a developer re-enables it;
  `.env.example` documents the variable and ships it off.

## 0.7.0

### Added
- **Edit with a prompt, no API key.** A Prompt bar above the preview (`/`
  focuses it) turns one sentence into a verified edit through a ladder of
  brains: a grammar-and-recipe planner (`agent/prompt/{grammar,slots,recipes,
  planner}.py`) answers instantly at confidence ≥ 0.75; an on-device language
  model normalises the 0.4–0.75 band — Apple Intelligence through a
  network-free Swift helper (`tools/fm-planner`, macOS 26+, only when the
  feature is on in System Settings) then `mlx-community/Qwen2.5-7B-Instruct-4bit`
  via MLX (`uv sync --extra local-llm`; tiered by RAM, loaded only from the
  local Hugging Face cache); Claude is a rung only when `ANTHROPIC_API_KEY` is
  set. Every reply says which brain answered and, when a better rung was
  unavailable, exactly why and how to fix it (`GET /api/prompt/brains`). The
  on-device brains receive recipe cards and emit an intent draft; the same
  recipe table expands it into a staged plan, so an LLM plan gets stages,
  postconditions, prerequisites and idempotence for free.
- **Plans are verified, not assumed.** Every run ends with measured
  postconditions read from the EDL, the transcript mapped through
  `agent/timemap`, ffprobe or one 360p verify render made through the export
  audio path: captions cover of speech in timeline seconds, no cue past the
  picture, loudness on the render, no overlay outside the platform safe zone,
  splits on beats, fillers remaining. Failed checks lead the reply with
  measured vs expected. A whole prompt run is **one op, one undo step**;
  execution outlives the SSE connection (a phone that locks does not roll
  back a caption pass) and `GET …/prompt/run` reconnects to a running or
  finished run. Five new SSE events — `brain`, `plan`, `step`, `verify`,
  `clarify` — beside the six the phone knows; the first `text_delta` of every
  turn starts with `via <Brain> —` so the iPhone app shows the brain with zero
  changes.
- **Clarifications instead of guesses.** The planner asks only when it cannot
  know (caption language, brand handle, voiceover text, a ratio on an
  already-vertical source), plus two gates: a first-use download (MADLAD 3 GB,
  large-v3 3.1 GB, a Piper voice 60 MB) and a run over 90 s. From the phone
  you answer with a whole word (`hi`, `first`, `haan`, `skip`); a partial
  match never counts.
- **All the transitions from CapCut.** `presets/transitions` names every look
  in `render/transitions.py` — all 58 native ffmpeg xfade transitions plus the
  custom ones — with a CapCut-style display name, category (Basic, Wipe,
  Slide, Zoom, Blur, Shape, Glitch/Stylised, Light), default duration and a
  one-line description; `add_transition`/`list_transitions` accept and
  advertise every catalog name; the compositor honours per-transition default
  durations; the desktop gains a Transitions panel (category tabs, grid,
  local hover preview, apply-to-selected-cut, keyboard); and the prompt takes
  transition intents — "smooth zoom between every clip", "add a glitch
  transition at the hook".
- **`transcribe`** — a non-mutating tool that persists the first v1 clip's
  transcript without laying captions; the prompt path's prerequisite, so
  "remove the ums" on a fresh upload never means a second transcription pass
  or an unwanted caption track. Refuses a model that is not on disk.
- **Presets**: six text styles, four transition looks, five edit templates,
  and four procedural music beds (`scripts/gen_music_beds.py` — 48 kHz,
  180 s, −18 LUFS, an exact kick grid with a `.json` sidecar; generated at
  build time, never committed).
- **The CapCut-parity benchmark** (`tests/benchmark/`, `docs/BENCHMARK.md`):
  26 prompts through the real route on synthesized media with ground truth to
  the millisecond, measured independently of the app's verifier, with a
  socket-level guard that fails any network egress. `pytest -m benchmark`
  opts in; the media pipeline and the guard are in the default run.
- iOS build number 2.

### Changed
- **One source↔timeline mapping for every transcript consumer** (the
  `d06d1c7` baseline of this release): `remove_fillers`, `add_caption_track`
  and `auto_caption` go through `agent/timemap.py`, and footage is removed by
  transcript/source time only via `_cut_source_ranges`, which re-maps through
  the live EDL after every cut. Before: after `remove_silences`, `remove_fillers`
  removed 1 of 4 fillers and cut two stretches of real speech, and captions
  generated after cuts were laid at source time — 9.7 s past the end of the
  video, rendering over black. `export_srt/vtt/ass` deliberately stay
  source-timed.
- **Every argument a handler reads is advertised in its schema**
  (`tests/test_tool_schema_completeness.py` pins the census at zero). The
  day the rule was introduced, 13 tools read 22 arguments their schema never
  mentioned (`auto_reframe.subject_track`, `add_text.size`, `apply_lut.lut_path`,
  …); `add_music(gain_db)` was silently ignored. Unknown arguments are now a
  rejected plan, not a no-op.
- `add_music` gains `loop`: a bed shorter than the video is laid back-to-back
  until the extent is covered, only the last piece fading out.
- Caption and lower-third defaults sit higher on vertical canvases (the
  TikTok/Reels UI covers the bottom ~20 % and the right rail): `y` 0.76 /
  0.74 on 9:16, unchanged on 16:9. `SAFE_ZONES` is one table shared by the
  handlers and the verifier.
- Chat without a key no longer answers "ANTHROPIC_API_KEY is not set": it
  delegates to the same prompt service, and the startup message says the bar
  runs on local brains. `/dispatch` answers `409 prompt_running` while a prompt
  run holds the session lock (the desktop shows "Prompt running — wait or
  cancel"); chat history is written under a per-session history lock so the
  route's save and the run thread's finalize can land in either order.
- The feature report memo moved from `main.py` into `ai/features.py`
  (`cached_feature_report`), so the planner's facts never trigger the 2.2 s
  cold probe twice.

### Security
- Every plan — from any brain — is validated against an explicit allowlist
  before any dispatch: allow-listed tools only (`undo/redo`, `set_property`,
  `add_clip`, `add_sticker`, `add_effect`, `repair_*`, the export writers and
  a dozen more are denied), no unknown argument, enum whitelists for every
  free string that reaches the filesystem or the network (whisper models,
  Piper voices, fonts, LUT names, caption targets, preset names, transition
  names), numeric bounds, and **no model-authored file path** — a plan may
  read only files the session already offers.
- The prompt path never downloads a model or a voice without a **yes**;
  "no cloud key" is not "no network", the rule is no network without an
  answer. Model downloads are loopback-only routes (a paired phone cannot
  start a 4 GB download on the Mac). The Apple Intelligence helper is a
  network-free child process with a scrubbed environment and an empty working
  directory; its source is tripwired against `URLSession`, `Network`,
  sockets and `Data(contentsOf:)`, and `Package.swift` declares zero
  dependencies. The MLX loader sets `HF_HUB_OFFLINE=1` in-process and loads
  from a directory, never a repo id; its download path excludes `*.py`.
- The on-device brains receive no secrets, no absolute paths and no tool
  schemas; only the cloud brain may emit raw tool steps, and those are
  validated the same way.

### Docs
- `docs/PROMPT_EDITOR.md` (the ladder, the prompts, the questions, answering
  from the phone, transitions, environment), `docs/BENCHMARK.md` (ground
  truth, measurement rules, tiers, the claim procedure), `tests/benchmark/README.md`;
  README "Edit with a prompt, no API key"; CLAUDE.md sections for
  `agent/prompt/`, `brains/` + `tools/fm-planner/`, `agent/timemap.py` (two
  clocks) and `tests/benchmark/`; `.env.example` marks the key optional and
  documents `VAI_BRAIN`, `VAI_MLX_MODEL`, `VAI_PROMPT_CLOUD`.

## 0.6.0

### Added
- **iPhone companion app.** A real editing client for the phone: the timeline,
  the AI tool catalog, chat, import and export all live on iOS, while the Mac
  does every heavy thing — the 108 dispatch tools, the Claude agent loop, and
  every ffmpeg render. The phone never pretends it can do AI work on its own;
  each screen that needs the Mac says so, and the connection state is visible
  at all times.
- **LAN mode — opt-in, and off by default.** Reaching the Mac from a phone
  means putting the editor's HTTP socket on your local network, which is a real
  change in exposure, so it is a switch you turn on in the desktop app's Phone
  panel rather than something a release quietly does for you. With the switch
  off, the app binds `127.0.0.1` exactly as it always has and none of the code
  below runs. The setting is stored in `settings.json` alongside the app's logs
  (an environment variable would have been unreachable in a double-clicked
  `.app`) and changing it asks you to restart, because the bind address is
  chosen once at launch.
- **Pairing.** The Phone panel shows a QR code carrying a single-use, ten-minute
  claim code; scanning it gives that phone a bearer token. Devices are listed by
  name and can be revoked one at a time. Native media loaders, which cannot
  reliably carry an `Authorization` header, use a separate 60-second token that
  is accepted on media URLs only.
- **`GET /api/pair/*`** — `info`, `lan`, `new`, `claim`, `whoami`, `devices`,
  `revoke`, `media_token`. The four that change the security posture are
  loopback-only: a paired phone cannot pair a second phone.
- **An upload limit that actually exists.** `VAI_MAX_UPLOAD_BYTES` (4 GiB by
  default) is enforced from the declared `Content-Length` before a byte is read,
  again as a running total mid-stream, and once more as a free-space
  precondition — and is reported by `/api/health` so the phone can refuse an
  over-sized pick before spending your battery on it. There was previously no
  cap of any kind.

### Security
- **Closes an arbitrary file READ and an arbitrary file WRITE reachable through
  the tool dispatcher.** Six tool arguments — `import_srt.path`,
  `multicam.srcs`, `find_broll.bin` and the `path` of `export_srt`,
  `export_vtt` and `export_ass` — took a caller-supplied filesystem path and
  used it without ever consulting the path allowlist. Reachable from both
  `POST /api/sessions/{id}/dispatch` and `/mcp`. The read half could pull any
  file the user could read and hand it back through `/transcript`; the write
  half ran `mkdir(parents=True)` and then wrote to any destination, which on a
  path like `~/.zshrc` or `~/Library/LaunchAgents/` is code execution at the
  next login. `set_property`'s `value` was a seventh route to the same read.
  All seven now resolve through the allowlist, with a narrower list for writes
  than for reads, and `tests/test_path_guards.py` derives the set of
  path-typed arguments from `/api/tools` so a new tool cannot be added without
  a guard decision recorded for it.
- **Path restriction is forced on whenever the socket is not loopback-only.**
  Tools may then read inside the editor's workdir, `~/Movies`, `~/Downloads`
  and `~/Pictures`, and write only to the first three; `VAI_ALLOWED_ROOTS` adds
  more. Turning LAN mode off releases the restriction again, so nothing that
  worked on the desktop stops working.
- **DNS-rebinding defence, on every route in every posture.** The server
  answers only to a loopback name or a bare IP literal in `Host` — a page on
  `evil.com` that re-resolves its own name to your Mac's LAN address gets a 421
  instead of a session. It runs with LAN mode OFF too, which is the posture
  every default install ships in; only `/livez` and `/readyz` are exempt, so a
  monitor with its own `Host` header is unaffected. Requests labelled
  `Sec-Fetch-Site: cross-site` are refused, and API routes require an
  `X-VAE-Client` header, which a browser can only send after a preflight this
  app's CORS policy denies.
- **Closes an arbitrary file MOVE through `POST /api/load_project`.** A `.vae`
  is a zip, and its `manifest.json` named each bundled media file as a string
  that was joined onto the unpack directory — but `Path("/a/b") / "/etc/passwd"`
  discards the base entirely, so an absolute path in a hand-made manifest was
  enough to `shutil.move` any readable file on the Mac into a session's
  `uploads/imported/`, from where it could be downloaded over the media route.
  No traversal was needed and none of the tool-dispatcher path guards were in
  the way, because `load_project` never consulted them. Manifest entries that
  resolve outside the archive are now refused.
- **Closes an arbitrary `.json` write and read through the show templates.**
  `save_show_template.name` and `apply_show_template.name` were interpolated
  straight into a path; the most damaging target was the app's own
  `settings.json`, which parses as a template and comes back with an empty
  device list — silently unpairing every phone. Names are now a strict
  `[A-Za-z0-9._-]` leaf inside `presets/shows`. The guard table in
  `tests/test_path_guards.py` now derives `name` arguments too, since the
  reason this one was missed is that it was not called `path`.
- **Auth lockout** after 60 rejected requests a minute from one peer, with
  media paths excluded from the count — a single filmstrip paint is two dozen
  requests, and a phone with a stale token must not be able to lock itself out.

### Changed
- `/api/health` now also reports `max_upload_bytes`.
- `desktop.py` splits the bind address from the window URL: with LAN mode on it
  binds `0.0.0.0` while the webview and the voice-over bridge keep talking to
  `127.0.0.1`. A socket bound to a public interface requires authentication
  even if the LAN switch is later turned off — a toggle cannot un-bind a
  socket, so it must not be able to disarm the auth in front of one. That rung
  is now enforced by observation as well as by intent: a request that arrives on
  a non-loopback interface arms authentication and the path allowlist before it
  is answered, so a hand-rolled `uvicorn --host 0.0.0.0` with `VAE_LAN` unset is
  no longer wide open.
- `settings.json` is written `0600` inside a `0700` directory, through an
  unpredictable temp file rather than a predictable `.json.tmp`. It is the only
  record of which phones are paired; on a shared Mac the process umask had been
  making it world-readable.
- Every read-modify-write of the pairing settings now holds one lock end to
  end. Revoking a phone while it was still polling could be undone by the
  `last_seen` write from a request that had already loaded the old device list:
  the panel showed the phone gone and the phone kept working.
- The upload cap's second and third layers now cover `sticker_upload`,
  `subtitle_upload` and `load_project`, which still had raw read loops. The
  `Content-Length` middleware cannot see a body sent with
  `Transfer-Encoding: chunked`, so those three had no limit at all against one.

### Fixed (iPhone companion)
- A request that timed out was reported as a user cancellation, so a sleeping
  Mac left the connection bar reading "Connected" while edits, uploads and
  exports failed in silence. Timeouts are now their own failure and move the
  connection state.
- The automatic reconnect had no caller: `retrying`, `unreachable` and
  `throttled` were terminal states whose copy promised a recovery. One owner
  now drives them, and the app re-probes when it returns to the foreground.
- The phone asked for new ops with the last op's sequence number, but the Mac
  treats that value as a list index — so a fresh project reported its own
  `init` op as "the project changed while you were looking at it" every six
  seconds, permanently.
- Queue depth never worked: the phone asked `GET /api/jobs`, which is not a
  route. Every queued render said "waiting" instead of how many jobs were ahead.
- Signed media URLs are no longer cached past the life of the 60-second token
  inside them, and a thumbnail failure is confirmed as a real refusal before the
  clip's filmstrip is written off. Preview playback re-signs and resumes when a
  range request is refused mid-stream rather than stalling with no explanation.
- Cold start no longer holds a blank screen for up to twelve seconds waiting on
  a Mac that is not answering.
- The project list is virtualized and fetches posters only for the rows on
  screen.
- A `.local` address is refused up front with an explanation, because the Mac
  will always answer it with a 421; a Tailscale (100.64/10) address is now
  accepted with a note about the VPN instead of being reported as an attack.
- Both the desktop's Phone panel and the phone's Connect screen now say that
  the connection is not encrypted, which is the one exposure arming LAN mode
  adds that a user cannot see for themselves.

## 0.5.0

### Added
- **AI panel.** A second tab in the left pane ("Media | AI") lists every
  AI/auto-edit tool that was chat- or MCP-only (silence and filler removal,
  beat cuts, auto-reframe, shorts, diarization, translation, stems, upscale,
  stabilize, background removal, object erase, motion tracking, …) as a
  searchable catalog with generated forms, background jobs for the slow ones
  (Cancel where the backend can actually honour it — today `auto_caption`),
  tool-specific result views, and inline "what's missing and how to fix it"
  from the new `GET /api/features` (the `check_features` report over HTTP).
- **Safe-zone overlay.** Approximate TikTok / Reels / Shorts UI occlusion
  guides over the 9:16 preview so captions and lower-thirds land where the
  app's own chrome won't cover them.
- **`GET /api/tools` now reports `cancellable` / `reports_progress`** per tool,
  derived from the handler signature, so UIs stop promising a Cancel the
  backend can't deliver.
- **`POST /api/sessions/{sid}/subtitle_upload`** — stores a .srt/.vtt/.ass in
  the session so `import_srt` works from the browser without typing a path.

### Fixed
- **Composite AI tools (remove silences/fillers, beat cut, hook stack, templates) are now a single undo step.**
  Each of them used to commit once per internal edit, so one click needed a
  dozen undos to take back; `EDLStore.batch()` collapses the run into one op.
- **`build_app.sh` no longer overwrites `Video AI Editor.spec`.** PyInstaller's
  CLI mode wrote its generated spec into the repo root, clobbering the
  committed Windows spec on every macOS build. It now lands in
  `build/pyinstaller-spec/` (and `--add-data` sources are absolute, which
  `--specpath` requires).
- **Finder "Get Info" showed 0.0.0** for the macOS app: PyInstaller's CLI mode
  never sets a bundle version, so `build_app.sh` now stamps
  `CFBundleShortVersionString`/`CFBundleVersion` from `VERSION` before signing.
- **Version drift.** `pyproject.toml`, `package.json`, `__version__` and
  `uv.lock` had fallen to 0.3.7/0.1.0 while `VERSION` read 0.4.1; all agree
  now and a test keeps them that way. (0.4.x shipped without a changelog
  entry — its build-identity work is documented in CLAUDE.md "Release
  identity".)
- **Job errors no longer show a `RuntimeError:` prefix** in toasts and the AI
  panel.

### Notes
- Chat turns use Anthropic prompt caching: the tool schemas + static system
  prompt are one cached prefix and the trailing user/tool-result block is a
  second breakpoint, so each tool round re-reads the previous one from cache.
  The per-call live timeline context now travels as an extra block AFTER that
  breakpoint (inside the trailing user message, never in `system`), so a
  mutating tool round or a moved playhead no longer invalidates the cached
  conversation. Behaviour is identical; cached input tokens bill at ~10%.
  `VAI_PROMPT_CACHE=0` disables it; a backend that rejects `cache_control`
  disables it for the process automatically.
- `add_caption_track` and `get_transcript` now resolve the transcript the same
  way the subtitle exporters do: an imported `<session>/transcript.json` wins
  over Whisper's `ingest.json`, so "Import subtitles → Captions from
  transcript" lays the imported cues on the timeline.
- **Notarized macOS release pipeline.** `build_notarize.sh` takes
  `build_app.sh`'s ad-hoc-signed bundle, re-signs it inside-out with a
  Developer ID (every `.so`/`.dylib` first, then the bundle with
  `entitlements.plist`, no `--deep`), notarizes and staples the app, then
  builds, signs, notarizes and staples the DMG, and refuses to finish unless
  `spctl` reports `Notarized Developer ID` for both. `build_app.sh` is
  unchanged (still the ad-hoc dev path); `build_dmg.sh` gained `VAE_APP` /
  `VAE_DMG` overrides with the same defaults. `VAE_SIGN_IDENTITY=- … --sign-only`
  is a keychain-free dry run. Setup in CLAUDE.md → "Notarized release".

## 0.3.7

### Added
- **A freshly-recorded voiceover is now highlighted on the timeline.** When a
  voiceover finishes encoding, its clip is selected (selection ring) and briefly
  flashed (a white-over + blue border that clears after ~0.6s) so you can see
  where it landed. Generic `flashClip(id)` lives in the store for reuse.

### Notes
- The timeline is canvas-rendered (no DOM clip nodes), so the literal
  `scrollIntoView`/CSS-keyframe approach doesn't apply; the flash is drawn on the
  canvas instead. The VO track sits in the visible track stack, so no scroll is
  needed. The flash auto-clear is guarded by the flash timestamp so re-flashing
  the same clip restarts rather than self-cancels.
- Verified live by pixel-diffing the timeline canvas: the highlight draws over
  the clip during the window (Δ7.1M) and reverts exactly to baseline (Δ0) after.

## 0.3.6

### Fixed
- **Sticker/emoji picker now closes on outside click.** The picker stayed open
  until you toggled it again; it now dismisses when you click anywhere outside.
  The ref wraps the toggle button too, so clicking the toggle to close it
  doesn't fall through the outside handler and immediately re-open.
- Verified live: opens on toggle, closes on an outside mousedown, stays open
  when picking an emoji inside, and the toggle still closes cleanly.

## 0.3.5

### Added
- **Undo toast on delete.** Deleting a clip now shows a bottom-center toast —
  "Clip deleted" (or "N clips deleted") with an **Undo** button — that
  auto-dismisses after ~4s. The toast system gained inline action buttons, and
  the prompt fires from the store's dispatch on `ripple_delete` / `bulk_delete`,
  so it covers every delete path (keyboard, Properties Delete, timeline context
  menu) from one place. Undo uses the app's own backend undo.
- Toasts moved to bottom-center (out from under the chat panel) and gained an
  explicit ✕ dismiss.
- Verified live: delete (2→1 clips) → "Clip deleted" + Undo → click Undo →
  restored (1→2); toast auto-dismisses.

## 0.3.4

### Changed
- **Stale download links are now flagged "(outdated)" instead of vanishing.**
  Each export/`.vae` link is stamped with the history length (`ops.length`) it
  was made at. When you edit past that point, the link stays — you can still
  grab the last render — but shows **↓ MP4 (outdated)** / **↓ .vae (outdated)**
  in amber with a strike-through, so nobody ships a stale file by mistake.
  Re-exporting (or re-saving) refreshes the stamp and the link goes green again.
  Supersedes the 0.2.7 behavior, which hard-cleared the link on any edit.
- Verified live: export → fresh "↓ MP4" → an edit advances history → "↓ MP4
  (outdated)" (strike-through) → re-export → fresh again.

## 0.3.3

### Fixed
- **Voiceover recorder could get stuck / leave the mic hot.** The button now
  returns to idle (and releases the microphone) on every exit path:
  - `onstop` unconditionally tears down (stops the mic stream + elapsed ticker)
    and sets `recording = false` before any early-return, so a too-short or
    empty capture can't strand the UI — and a too-short capture now says so
    instead of failing silently.
  - If recording fails *after* the mic was granted (unsupported recorder, a
    throw past `getUserMedia`), the start handler releases the stream so the OS
    mic indicator doesn't stay lit looking like it's "still recording."
  - Added a `MediaRecorder.onerror` handler and mic release on unmount.
- Verified live: with mic permission denied, the button falls back to
  "🎙 Record voiceover" with an error instead of hanging on "Requesting mic
  access…".

## 0.3.2

### Fixed
- **Space didn't play/pause when a slider was focused.** The keymap skipped
  every focused `INPUT`, so after nudging a color/transform/zoom slider, Space
  was swallowed instead of toggling playback. The handler now only bows out for
  genuine **text-entry** fields (textarea, contentEditable, text/number/date/…
  inputs); for non-text controls (range, checkbox, button, select) global
  shortcuts win — above all Space → play/pause. It runs in the **capture phase**
  and `preventDefault`s, so the focused control doesn't also react (e.g. a
  button "clicking" on Space). A focused slider still keeps its own arrow / Home
  / End / PageUp / PageDown keys for stepping.
- Verified live: Space with a focused range slider plays; with a text field it's
  ignored (typing preserved); ArrowRight on a focused slider isn't hijacked;
  Space on the page still plays.

## 0.3.1

### Added
- **📂 Open .vae… inside the project switcher dropdown.** The `{project} ▾`
  switcher already had ＋ New project, the recent-sessions list (current one
  highlighted), and click-outside-to-close — it was just missing an in-dropdown
  way to open a saved `.vae` bundle (it existed only as a separate toolbar
  button). Added it next to ＋ New project, reusing the existing file picker.
- Verified live: dropdown shows current (highlighted) + ＋ New project + 📂 Open
  .vae… + recents; switching projects works; opens on click and closes on
  outside mousedown.

## 0.3.0

### Added
- **Direct sticker manipulation on the canvas.** Stickers are now interactive,
  not just rendered:
  - Click a sticker to select it (canvas hit-testing, rotation-aware).
  - Drag the body to move it; drag a corner handle to resize. A dashed bounding
    box + corner handles show on the selected sticker. Live feedback during the
    gesture; the server is hit once on release (`set_clip_transform`).
  - The Properties panel gains a **Sticker** inspector — X, Y, Scale, Rotation,
    Opacity, Start and Duration — all editable, with the canvas and panel kept
    in sync.
  - New `StickerLayer` owns sticker drawing + interaction; `TextLayer` is now
    text-only. Shared geometry/keyframe helpers live in `lib/overlay.ts` so the
    hit-box always matches the painted glyph.
- **Backend:** `set_clip_transform` now works on stickers (not just media
  clips); new `set_clip_timing` sets start/end on overlay clips (stickers/text).
- Verified live: insert → select → drag (Δx/Δy exact) → corner-resize (1×→2×) →
  edit Duration (3s→1.5s, start preserved), all committing to the EDL. Backend
  suite 259 passed.

> Stickers already inserted at the playhead with a 3-second default and rendered
> on a dedicated Stickers track; this release makes them directly editable.

## 0.2.9

### Added
- **Live value readouts on the Color sliders.** Brightness, Contrast,
  Saturation, Temp and Tint now show their current value (right-aligned, like
  the Speed and Audio sliders), updating live as you drag — the slider is
  controlled now, while still committing to the server only on release. Formats:
  Brightness `±0.00`, Contrast/Saturation `0.00×`, Temp `±N` (−100..+100), Tint
  `±0.00`. (Transform sliders already showed `scale 1.00 / rotation 0° /
  opacity 1.00`.)
- Verified live: each color slider shows the correct initial value, the readout
  tracks a drag (e.g. Brightness `+0.30` / `−0.24`), and the 280px panel stays
  overflow-free.

## 0.2.8

### Fixed
- **Properties panel overflowed its fixed-width column.** Several issues, all in
  the right sidebar:
  - The In/Out and Fade-in/out rows used flex with non-shrinking number inputs;
    they're now a `1fr 1fr` grid with `min-width: 0` inputs so they stay equal
    and fit.
  - `.props input { width: 100% }` was stretching the *Mute* checkbox and
    blowing out its label — the rule now excludes checkbox/radio.
  - Long filenames (`a_b_c.normalized.mp4`) and clip ids are unbreakable tokens
    that painted past the panel edge; `overflow-wrap: anywhere` lets them wrap.
  - Flex rows (sliders, Transform x/y, action buttons) wrap instead of
    overflowing; the sidebar is `min-width: 0` + `overflow-x: hidden`.
- Verified live by measuring the DOM: the Timing row is a grid of two equal
  125.5px columns, all inputs are `border-box`, and the 280px sidebar reports
  **zero** horizontal overflow with a clip selected.

## 0.2.7

### Added
- **Export progress modal.** Export now opens a modal with a **live progress
  bar** (real ffmpeg progress, not a fake animation), an **ETA**, a **Cancel**
  button, and a success **toast** + auto-download on completion. Progress comes
  from ffmpeg `-progress` streamed into the background job; the bar shows an
  indeterminate sweep until the first real sample lands.
- **Cancellable exports.** `POST /api/jobs/{id}/cancel` terminates the running
  ffmpeg and marks the job `cancelled` (no orphan processes, no partial files —
  the atomic `.part` write is cleaned up). The job system gained cooperative
  `set_progress` / `cancel_event` hooks, injected only into job fns that opt in.
- **Toast notifications** (`toast.success/error/info`) with a bottom-right host.

### Changed
- A new edit clears the previous export's stale **↓ MP4** download link, so the
  navbar never offers an out-of-date render.

### Notes
- Verified live: progress climbed 9 → 39 → 69%, ETA counted down ~10s → ~2s,
  Cancel produced a "cancelled" toast and closed the modal, completion fired the
  success toast + download, and the stale link cleared after an edit. Backend
  suite 258 passed; preview render path is untouched (progress is export-only).

## 0.2.6

### Changed
- **Narrow-window strategy: hold-and-scroll instead of reflow.** A pro 3-pane
  editor needs width, so rather than collapsing the panes into a single column
  on small windows, the layout now holds a **900px minimum width** and scrolls
  horizontally (`#root`) below that. The toolbar stays on one line and scrolls
  internally. A dismissible banner appears under 900px nudging the user to a
  wider window. This supersedes the 0.2.1 reflow (which stacked panes vertically
  under 1024px). Modal dialogs keep their `min(…, 92vw)` sizing.
- Verified live across widths: at 1280px the 3-pane grid is intact with no
  banner and no scroll; at 700px the layout holds at 900px (panes don't
  collapse), `#root` scrolls horizontally, and the banner shows and dismisses.

## 0.2.5

### Fixed
- **Playback froze when the preview couldn't decode.** The playhead clock was
  driven by `requestVideoFrameCallback`, which only fires when the `<video>`
  actually decodes a frame — so an undecodable preview stopped the timeline, the
  red playhead line, and the time readout even though playback was "running."
  The clock is now an independent rAF wall clock: it follows the video's
  `currentTime` for exact A/V sync when the video is advancing, and free-runs on
  wall time when the video stalls or can't render — clamping to the clip
  duration and stopping cleanly at the end. Per-frame work is wrapped so a
  render failure is non-fatal.
- Verified live: normal playback advances then freezes on pause; with the video
  deliberately sabotaged so it can never decode a frame, the playhead still
  advances (free-run) instead of freezing.

## 0.2.4

### Fixed
- **Preview scrubbing broke on torn/half-written files ("mp4box invalid box").**
  Two root causes:
  1. *Non-atomic renders.* ffmpeg wrote preview/export output straight to the
     served path; `-y` truncates first, so a render that was killed or fetched
     mid-write left a 0-byte or partial `.mp4` that mp4box rejected. Renders now
     go to a temp sibling and `os.replace()` into place atomically — readers
     only ever see a complete file or none. The serving endpoint also treats a
     0-byte leftover as missing and re-renders.
  2. *No demux fallback.* When mp4box/WebCodecs can't parse a file (edit lists,
     unusual codecs, a genuinely odd box), the `FrameScrubber` now falls back to
     a hidden `<video>` it seeks via `currentTime` and paints to the canvas on
     `seeked` — so scrubbing keeps working instead of silently disabling. The
     fallback `<video>` carries no `src` until mp4box actually fails (no
     double-fetch on the happy path).
- Verified live: atomic render emits a valid `ftyp` file with zero `.part`/
  0-byte leaks, and the fallback paints a real frame (100% non-black) from the
  served preview.

## 0.2.3

### Added
- **`ANTHROPIC_API_KEY` for the shipped app.** The dev server reads the repo
  `.env`, but a double-clicked `.app` can't (its project root is inside the
  read-only bundle). Config now also loads `.env` from a stable user-writable
  location — `~/Library/Application Support/Video AI Editor/.env` — so the
  in-app Claude chat works from the DMG. Repo `.env` still wins for dev; the
  shell env still wins over both. Keys are never bundled or committed.

## 0.2.2

### Fixed
- **The shipped `.app` couldn't find `ffmpeg` — so import, preview, scrubbing,
  export and captions all failed when launched by double-click.** A
  Finder-launched macOS app inherits launchd's minimal `PATH`
  (`/usr/bin:/bin:/usr/sbin:/sbin`), which excludes `/opt/homebrew/bin` where
  `ffmpeg`, `ffprobe` and `whisper-cli` live — every shell-out died with
  `FileNotFoundError: 'ffmpeg'`. (Running from a terminal masked it, because the
  shell supplies Homebrew's `PATH`.) The app now appends the common Homebrew /
  MacPorts / `~/.local/bin` locations to `PATH` at startup, so those binaries
  resolve no matter how it's launched. Verified end-to-end under a simulated
  launchd environment: upload, preview, and waveform all succeed.

> Note: this resolves the binaries on a machine that already has them
> (e.g. `brew install ffmpeg whisper-cpp`). A fully self-contained build that
> bundles `ffmpeg` for machines without Homebrew is tracked separately.

## 0.2.1

### Fixed
- **Responsive layout for iOS / iPadOS Safari.** The 3-column desktop grid had a
  hard ~1292px content floor that spilled off-screen on phones and tablets
  (902px overflow on iPhone, 458px on iPad). Below 1024px the editor now reflows
  to a single scrolling column — preview, then a horizontally-scrollable
  timeline, then media / properties / history — with zero horizontal overflow.
  The toolbar scrolls internally instead of widening the page; the Help modal is
  now `min(540px, 92vw)`.
- Verified clean across a 5-environment matrix: WebKit @ iPhone portrait +
  landscape, iPad, macOS Safari desktop, and Chromium desktop.
- **macOS `.dmg` packaging shipped stale code.** PyInstaller was reusing a
  cached analysis, so the bundle carried an old `main.py` (no `/api/version`)
  and an old frontend build. Builds now run `--clean`, the spec bundles the
  `VERSION` file, and `Info.plist` (`CFBundleShortVersionString`) tracks
  `VERSION` automatically. The shipped `.app` now reports 0.2.1 at runtime and
  in Finder, verified end-to-end from the mounted DMG volume.

## 0.2.0

The "make it real" release — everything since the first editable timeline.

### Added
- **Customizable keyboard shortcuts** with CapCut / Premiere Pro / Final Cut Pro
  presets, click-to-rebind, conflict detection, persisted to localStorage (⌨ in
  the top bar).
- **MCP server** at `/mcp` — drive the editor from Claude Code / Cursor / Codex.
- **Local CLIP visual search** (`search_media`) — find footage by visual content.
- **Best-quality Hindi/English auto-captions** (`auto_caption`, Whisper large-v3
  on Metal) with broadcast-grade cue formatting.
- **3-axis hook stack** (`apply_hook_stack`) + audit scoring.
- **~45-transition catalog** (was 7), all rendering correctly.
- **Full-window drag-and-drop import** — drop a video anywhere.
- **Parallel chunk rendering** (3–4× faster multi-clip cold renders).
- **Client-side live transform preview** (no render storm while dragging).
- **macOS `.app` + `.dmg`** build (AI-bundled), app versioning.

### Fixed
- Video import / preview failures return clean 422s instead of bare 500s.
- Silent-source videos (no audio track) now normalize + render.
- Corrupt chunk cache auto-detects + rebuilds.
- Bundle path resolution (frontend/presets/fonts/workdir) for the shipped `.app`.
- Whisper Hindi auto-detect (`-l auto`), 48 kHz preview audio, sharper preview.
- `_STORES` race + LRU bound; per-session data in a user-writable dir.

## 0.1.0
- Initial editable timeline, chat agent, ingest, render, export.
