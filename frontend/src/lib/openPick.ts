// What the top bar's Open control was handed.
//
// "Open" is where anyone looks first to open a video, and it only knows saved
// .vae projects: picking IMG_0623.MOV there used to end in "Couldn't open that
// .vae project ... That file is not a Video AI Editor project" — a refusal for
// a request that has an obvious meaning (put this clip in my project). A media
// file is now imported like a drop; a project (or anything we cannot tell by
// name, e.g. the `<sid>.vae.txt` macOS makes of a text/plain download) still
// goes to the project loader, whose CONTENT probe is the real judge.
import { AUDIO_EXTS } from './paths'

export interface PickedFile { readonly name: string; readonly type: string }
export type OpenKind = 'project' | 'media' | 'unknown'

const PROJECT_EXT = /\.(vae|zip)$/i
// Video and picture containers a phone, camera or screen recorder writes. No
// `.ts`: that is TypeScript as often as MPEG-TS, and a browser that knows it
// is a transport stream says so in `type` (video/mp2t).
const MEDIA_EXT = /\.(mov|mp4|m4v|mkv|webm|avi|mts|m2ts|mxf|3gp|3g2|wmv|flv|mpg|mpeg|png|jpe?g|heic|heif|webp|gif|bmp|tiff?)$/i

export function classifyOpenedFile(f: PickedFile): OpenKind {
  if (PROJECT_EXT.test(f.name)) return 'project'
  if (MEDIA_EXT.test(f.name) || AUDIO_EXTS.test(f.name)) return 'media'
  if (/^(video|audio|image)\//.test(f.type)) return 'media'
  return 'unknown'
}

/** The one line the person reads when a media file was routed to the importer. */
export function mediaInsteadOfProject(f: PickedFile): string {
  const what = f.type.startsWith('audio/') || AUDIO_EXTS.test(f.name) ? 'an audio file'
    : f.type.startsWith('image/') || /\.(png|jpe?g|heic|heif|webp|gif|bmp|tiff?)$/i.test(f.name) ? 'a picture'
    : 'a video'
  return `\u201c${f.name}\u201d is ${what}, not a project \u2014 added to the Media panel.`
}
