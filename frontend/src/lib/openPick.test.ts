import { describe, expect, it } from 'vitest'
import { classifyOpenedFile, mediaInsteadOfProject } from './openPick'

const f = (name: string, type = '') => ({ name, type })

describe('what the Open control was handed', () => {
  it('sends an iPhone video (the file that started this) to the importer', () => {
    expect(classifyOpenedFile(f('IMG_0623.MOV', 'video/quicktime'))).toBe('media')
    expect(classifyOpenedFile(f('IMG_0623.MOV'))).toBe('media')       // no MIME from the picker
  })

  it.each([
    'clip.mp4', 'CLIP.MP4', 'reel.m4v', 'take.mkv', 'screen.webm', 'old.avi', 'cam.MTS', 'cam.m2ts', 'a.mxf', 'a.3gp',
    'song.mp3', 'VOICE.WAV', 'note.m4a', 'x.flac', 'x.opus', 'x.aiff',
    'photo.HEIC', 'photo.heif', 'shot.png', 'shot.JPG', 'shot.jpeg', 'pic.webp', 'anim.gif',
  ])('treats %s as media', (name) => {
    expect(classifyOpenedFile(f(name))).toBe('media')
  })

  it('knows media by MIME when the name says nothing', () => {
    expect(classifyOpenedFile(f('IMG_0623', 'video/quicktime'))).toBe('media')
    expect(classifyOpenedFile(f('recording', 'audio/mpeg'))).toBe('media')
    expect(classifyOpenedFile(f('stream.ts', 'video/mp2t'))).toBe('media')
  })

  it('leaves projects to the project loader, whatever the case', () => {
    for (const n of ['My project.vae', 'P.VAE', 'backup.zip', 'BACKUP.ZIP']) {
      expect(classifyOpenedFile(f(n))).toBe('project')
    }
  })

  it('leaves what it cannot tell to the project loader, whose content probe decides', () => {
    // macOS appends .txt to a text/plain download: the app's own saved
    // project arrives as `<sid>.vae.txt` (v0.7.2). It must still reach the loader.
    expect(classifyOpenedFile(f('s_1234abcd.vae.txt', 'text/plain'))).toBe('unknown')
    expect(classifyOpenedFile(f('notes.txt', 'text/plain'))).toBe('unknown')
    expect(classifyOpenedFile(f('project', ''))).toBe('unknown')
    expect(classifyOpenedFile(f('script.ts', ''))).toBe('unknown')     // TypeScript as often as MPEG-TS
    expect(classifyOpenedFile(f('', ''))).toBe('unknown')
  })

  it('a .vae wins over a media-looking name', () => {
    expect(classifyOpenedFile(f('holiday.mov.vae', 'application/octet-stream'))).toBe('project')
  })
})

describe('the sentence the person reads', () => {
  it('names the file and what it is', () => {
    expect(mediaInsteadOfProject(f('IMG_0623.MOV', 'video/quicktime')))
      .toBe('\u201cIMG_0623.MOV\u201d is a video, not a project \u2014 added to the Media panel.')
    expect(mediaInsteadOfProject(f('song.mp3'))).toContain('is an audio file')
    expect(mediaInsteadOfProject(f('shot.HEIC'))).toContain('is a picture')
  })
})
