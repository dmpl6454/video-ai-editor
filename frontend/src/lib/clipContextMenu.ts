// The clip context menu (design §3; brief §6 "Video context menu S34"): the
// observed order for a video clip and for an audio clip, each item either a
// real command or disabled WITH ITS REASON — "Enablement must depend on clip
// type, selection count, clipboard contents and project state. Do not make
// them permanently disabled to match one screenshot." Pure: the menu's JSX
// renders what this returns, and a test can read every row.
import type { EDL, Track, AnyClip } from '../types'
import { isMediaClip } from '../types'

export interface MenuRow {
  label: string
  key?: string
  action?: () => void
  disabled?: boolean
  /** Why it is disabled, or what it does — the row's tooltip. */
  title?: string
  submenu?: boolean
  sep?: boolean
}

export interface MenuContext {
  edl: EDL
  track: Track
  clip: AnyClip
  selectedCount: number
  clipboardCount: number
  hasAttributes: boolean
  playhead: number
  /** The keymap's label for a chord ("⌘C"). */
  key(chord: string): string
  do: {
    copy(): void
    cut(): void
    copyAttributes(): void
    pasteAttributes(): void
    remove(): void
    split(): void
    splitScenes(): void
    transcript(): void
    isolateVoice(): void
    extractAudio(): void
    recoverAudio(): void
    deactivate(): void
    trim(): void
    replace(): void
    openFile(): void
    editEffects(): void
    freeze(): void
    duplicate(): void
    muteTrack(): void
    lockTrack(): void
  }
}

const NOT_IN_BUILD = 'Not available in this build'

export function clipMenuRows(c: MenuContext): MenuRow[] {
  const media = isMediaClip(c.clip)
  const audioLane = ['audio', 'music', 'vo'].includes(c.track.type)
  const video = media && c.track.type === 'video'
  const multi = c.selectedCount > 1
  const muted = !!(c.clip as unknown as { audio?: { mute?: boolean } }).audio?.mute
  const detached = (c.clip as unknown as { audio?: { detached?: boolean } }).audio?.detached === true
  const off = (c.clip as unknown as { disabled?: boolean }).disabled === true
  const sp = (c.clip as unknown as { speed?: unknown }).speed
  const curve = media && sp !== null && typeof sp === 'object'
  const first: MenuRow[] = [
    { label: 'Copy', key: c.key('Mod+KeyC'), action: c.do.copy },
    { label: 'Cut', key: c.key('Mod+KeyX'), action: c.do.cut, title: 'Copy, then delete' },
    { label: 'Copy attributes', key: c.key('Mod+Shift+KeyC'), action: c.do.copyAttributes, disabled: !media, title: media ? 'Copy this clip\'s transform, grade, speed and audio settings' : 'Only a media clip carries attributes' },
    { label: 'Paste attributes', key: c.key('Mod+Shift+KeyV'), action: c.do.pasteAttributes, disabled: !c.hasAttributes || !media, title: c.hasAttributes ? 'Apply the copied attributes to this clip' : 'Copy attributes from a clip first' },
    { label: 'Delete', key: c.key('Delete'), action: c.do.remove },
    { sep: true, label: '' },
  ]
  if (audioLane) {
    return [
      ...first,
      { label: 'Transcript', action: c.do.transcript, title: 'Transcribe this clip\'s words (opens Auto captions)' },
      { label: 'Isolate voice', submenu: true, action: c.do.isolateVoice, title: 'Keep the voice, drop the music (demucs) — opens the tool' },
      { label: 'Recover audio', action: c.do.recoverAudio, disabled: !detachedSource(c), title: detachedSource(c) ? 'Put this sound back into the video clip it came from' : 'This clip was not extracted from a video clip' },
      { sep: true, label: '' },
      { label: 'Create compound clip (subproject)', key: c.key('Alt+KeyG'), disabled: true, title: `${NOT_IN_BUILD}: compound clips` },
      { label: 'Group', key: c.key('Mod+KeyG'), disabled: true, title: multi ? `${NOT_IN_BUILD}: clip groups` : 'Select two or more clips' },
      { label: 'Ungroup', key: c.key('Mod+Shift+KeyG'), disabled: true, title: 'Nothing is grouped' },
      { sep: true, label: '' },
      { label: off ? 'Activate clip' : 'Deactivate clip', key: 'V', action: c.do.deactivate, title: 'Mute this clip without removing it' },
      { label: 'Link to media', action: c.do.openFile, title: 'Show this clip\'s file in the Media tab' },
      { label: 'Open file location', disabled: true, title: 'Not available in a browser; the file is in the project folder' },
      { sep: true, label: '' },
      { label: 'Range', submenu: true, action: c.do.trim, title: 'Trim this clip\'s start and end in the inspector' },
    ]
  }
  return [
    ...first,
    { label: 'Edit', submenu: true, action: c.do.trim, title: 'Open this clip in the inspector' },
    { label: 'Split scenes', action: c.do.splitScenes, disabled: !video, title: 'Cut at every scene change (opens Find moments)' },
    { label: 'Transcript', action: c.do.transcript, title: 'Transcribe this clip\'s words (opens Auto captions)' },
    { label: 'Isolate voice', submenu: true, action: c.do.isolateVoice, disabled: !media, title: 'Keep the voice, drop the music (demucs) — opens the tool' },
    { label: 'Extract audio', action: c.do.extractAudio, disabled: !video || detached || muted, title: detached ? 'Already extracted — Recover audio on the audio clip puts it back' : muted ? 'This clip\'s sound is muted' : 'Move this clip\'s sound to an audio track, aligned, so picture and sound cut separately' },
    { label: 'Sync video and audio', disabled: true, title: multi ? `${NOT_IN_BUILD}: waveform sync of separate clips` : 'Select a video and an audio clip' },
    { sep: true, label: '' },
    { label: 'Create compound clip (subproject)', key: c.key('Alt+KeyG'), disabled: true, title: `${NOT_IN_BUILD}: compound clips` },
    { label: 'Create multi-camera clip', disabled: true, title: multi ? 'Use Media › AI media › Multicam switch for angle files' : 'Select two or more clips' },
    { label: 'Save preset', disabled: !media, action: undefined, title: 'Save this clip\'s grade: inspector › Adjustment › Save as preset' },
    { label: 'Group', key: c.key('Mod+KeyG'), disabled: true, title: multi ? `${NOT_IN_BUILD}: clip groups` : 'Select two or more clips' },
    { label: 'Ungroup', key: c.key('Mod+Shift+KeyG'), disabled: true, title: 'Nothing is grouped' },
    { sep: true, label: '' },
    { label: off ? 'Activate clip' : 'Deactivate clip', key: 'V', action: c.do.deactivate, title: 'Mute this clip without removing it' },
    { label: 'Trim clip', action: c.do.trim, title: 'Trim this clip\'s start and end in the inspector' },
    { label: 'Replace clip', action: c.do.replace, disabled: !media, title: 'Swap this clip\'s footage for another file (keeps its timing)' },
    { label: 'Link to media', action: c.do.openFile, title: 'Show this clip\'s file in the Media tab' },
    { label: 'Open file location', disabled: true, title: 'Not available in a browser; the file is in the project folder' },
    { sep: true, label: '' },
    { label: 'Edit effects', action: c.do.editEffects, title: 'Open the Effects tab for this clip' },
    { label: 'Show variable speed animation', disabled: !curve, action: c.do.trim, title: curve ? 'Open the speed curve' : 'This clip has a constant speed' },
    { label: 'Range', submenu: true, action: c.do.trim, title: 'Trim this clip\'s start and end in the inspector' },
    { label: 'Render', submenu: true, action: c.do.freeze, title: 'Freeze the frame at the playhead' },
  ]
}

/** An audio clip detach_audio made records its picture in `linked_to`. */
function detachedSource(c: MenuContext): boolean {
  const link = (c.clip as unknown as { linked_to?: string | null }).linked_to
  if (!link) return false
  return c.edl.tracks.some((t) => t.type === 'video' && t.clips.some((x) => x.id === link))
}
