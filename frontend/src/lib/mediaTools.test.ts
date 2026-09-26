import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { isGatewayFailure } from './connection'
import { contrastRatio, resolveToken, rootTokens, ruleDeclarations } from './contrast'
import { bannerSentence, mediaToolsProblem } from './mediaTools'

// QA-108: GET /api/health reports whether ffmpeg/ffprobe are installed, and
// every route that needs them answers 503 `ffmpeg_missing`.

const MAC_HEALTH = {
  ok: true, version: '0.7.2', max_upload_bytes: 1, upload_limit: 'free_space',
  media_tools: {
    ok: false, missing: ['ffmpeg', 'ffprobe'], install_command: 'brew install ffmpeg',
    message: "Video AI Editor can't find ffmpeg and ffprobe, the video engine it uses to import, " +
      'preview and export. Your files are fine. Open Terminal, run  brew install ffmpeg  ' +
      "(Homebrew is at brew.sh if you don't have it yet), then quit and reopen Video AI Editor.",
  },
}

describe('mediaToolsProblem', () => {
  it('reads a missing toolchain from /api/health', () => {
    const p = mediaToolsProblem(MAC_HEALTH)
    expect(p).not.toBeNull()
    expect(p!.missing).toEqual(['ffmpeg', 'ffprobe'])
    expect(p!.command).toBe('brew install ffmpeg')
  })
  it('is quiet when the tools are there, or the backend is older', () => {
    expect(mediaToolsProblem({ ...MAC_HEALTH, media_tools: { ok: true, missing: [], install_command: 'x', message: null } })).toBeNull()
    expect(mediaToolsProblem({ ok: true })).toBeNull()
    expect(mediaToolsProblem(null)).toBeNull()
    expect(mediaToolsProblem('nonsense')).toBeNull()
  })
  it('the banner text points at the command box instead of repeating it', () => {
    const s = bannerSentence(mediaToolsProblem(MAC_HEALTH)!)
    expect(s).not.toContain('brew install ffmpeg')
    expect(s).toContain('run the command below')
    expect(s).not.toMatch(/\s{2}/)
  })
})

describe('a 503 from the engine itself is not an outage (QA-108)', () => {
  const envelope = JSON.stringify({ error: { code: 'HTTP_503', message: 'request failed', request_id: 'r',
    details: { error: 'ffmpeg_missing', message: MAC_HEALTH.media_tools.message } } })
  it('engine envelope 503 -> the engine answered', () => {
    expect(isGatewayFailure(503, envelope)).toBe(false)
  })
  it('a proxy 503/502/504 or an empty dev-proxy 500 is still an outage', () => {
    expect(isGatewayFailure(503, '')).toBe(true)
    expect(isGatewayFailure(503, '<html>Service Unavailable</html>')).toBe(true)
    expect(isGatewayFailure(502, envelope)).toBe(true)
    expect(isGatewayFailure(504, '')).toBe(true)
    expect(isGatewayFailure(500, '')).toBe(true)
    expect(isGatewayFailure(500, envelope)).toBe(false)
  })
})

describe('the install banner reaches AA on its own surfaces', () => {
  const STYLES = readFileSync(new URL('../styles.css', import.meta.url), 'utf8')
  const TOKENS = rootTokens(STYLES)
  const tok = (v: string) => resolveToken(v, TOKENS)
  const pairs: [string, string, string][] = [
    // [text selector, background selector, label]
    ['.tools-banner', '.tools-banner', 'banner text'],
    ['.tools-banner-title', '.tools-banner', 'title'],
    ['.tools-banner-text', '.tools-banner', 'explanation'],
    ['.tools-banner-command code', '.tools-banner-command', 'the command'],
  ]
  for (const [fgSel, bgSel, label] of pairs) {
    it(`${label} (${fgSel} on ${bgSel}) >= 4.5:1`, () => {
      const fg = ruleDeclarations(STYLES, fgSel).color
      const bg = ruleDeclarations(STYLES, bgSel).background
      expect(fg, `${fgSel} has no color`).toBeTruthy()
      expect(bg, `${bgSel} has no background`).toBeTruthy()
      expect(contrastRatio(tok(fg), tok(bg))).toBeGreaterThanOrEqual(4.5)
    })
  }
  it('the warning icon is a >= 3:1 non-text graphic', () => {
    const fg = ruleDeclarations(STYLES, '.tools-banner-icon').color
    const bg = ruleDeclarations(STYLES, '.tools-banner').background
    expect(contrastRatio(tok(fg), tok(bg))).toBeGreaterThanOrEqual(3)
  })
})
