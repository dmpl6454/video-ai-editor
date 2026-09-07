# Third-party notices — Video AI Editor (macOS)

## FFmpeg (ffmpeg, ffprobe) — GPL-2.0-or-later

This application **bundles and redistributes** static `ffmpeg` and `ffprobe`
binaries (version 7.0, arm64), obtained via the `static-ffmpeg` PyPI package
(pinned in `packaging/ffmpeg-static.sha256`).

That build is configured with `--enable-gpl` and includes **libx264**,
**libx265** and **libvidstab**, each GPL-licensed. The resulting binary is
therefore a **GPL-2.0-or-later** work.

**Why this matters.** Earlier versions of this app did not ship ffmpeg; they
invoked whatever copy the user had installed, which carried no redistribution
obligation. Bundling changes that: distributing the DMG now distributes GPL
binaries, which requires

1. conveying this licence notice with the product (this file), and
2. making the **corresponding source** available to recipients.

FFmpeg source: <https://ffmpeg.org/download.html> (select the 7.0 release).
x264: <https://www.videolan.org/developers/x264.html> ·
x265: <https://bitbucket.org/multicoreware/x265_git> ·
vidstab: <https://github.com/georgmartius/vid.stab>

> **If shipping a GPL binary is not acceptable for your distribution,** rebuild
> with an LGPL-configured ffmpeg (drop `--enable-gpl`, `libx264`, `libx265`,
> `libvidstab`) and point `VAE_FFMPEG_DIR` at it. Cost of that choice: the
> `stabilize` tool stops working (it needs vidstab), and the software H.264
> fallback is lost — acceptable on Apple Silicon, where the encoder ladder
> resolves to `h264_videotoolbox` first anyway.

## Emoji artwork — Apple

Emoji artwork is fetched at runtime from a CDN and cached locally; it is not
redistributed inside this bundle. See `src/video_ai_editor/ai/emoji.py`, whose
module docstring records that trade-off deliberately.

## Piper (text-to-speech) and espeak-ng data

Bundled to make voiceover work offline. Piper is MIT-licensed; espeak-ng is
GPL-3.0-or-later — source: <https://github.com/espeak-ng/espeak-ng>.
