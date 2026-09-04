import { describe, expect, it } from 'vitest'
import { errorMessage, messageFromBody, responseError } from './errorMessage'

// The envelope shapes api/hardening.py actually puts on the wire. Each one was
// a message a user never saw: api.ts read `detail.error`, a key none of these
// emit, so every failure below rendered as a bare "422 Unprocessable Entity".
describe('messageFromBody', () => {
  it('reads a dict detail out of error.details, not error.message', () => {
    // HTTPException(422, {"error": "render_failed", "message": …}) — hardening
    // hardcodes error.message to the "request failed" sentinel for these.
    expect(messageFromBody({
      error: {
        code: 'UNPROCESSABLE',
        message: 'request failed',
        request_id: 'abc123',
        details: {
          error: 'render_failed',
          message: 'ffmpeg is not installed — install it and reopen the app.',
          ffmpeg: 'sh: ffmpeg: command not found',
        },
      },
    })).toBe('ffmpeg is not installed — install it and reopen the app.')
  })

  it('falls back to details.detail, then to the legacy details.error sentence', () => {
    expect(messageFromBody({ error: { message: 'request failed', details: { detail: 'ffprobe exited 127' } } }))
      .toBe('ffprobe exited 127')
    // vo_record / audio_upload still raise {"error": "<sentence>"} with no
    // `message` key — there `error` IS the human text, not a slug.
    expect(messageFromBody({ error: { message: 'request failed', details: { error: 'vo transcode failed: no such file' } } }))
      .toBe('vo transcode failed: no such file')
  })

  it('uses error.message when the detail was a plain string', () => {
    // HTTPException(404, "session s_x not found") / a bare ValueError → 400.
    expect(messageFromBody({ error: { code: 'NOT_FOUND', message: 'session s_x not found', request_id: 'r' } }))
      .toBe('session s_x not found')
  })

  it('names the field for a request-validation body', () => {
    expect(messageFromBody({
      error: {
        code: 'VALIDATION_ERROR', message: 'invalid request',
        details: [{ loc: ['body', 'height'], msg: 'Input should be a valid integer', type: 'int_parsing' }],
      },
    })).toBe('height: Input should be a valid integer')
  })

  it('still reads the plain FastAPI shape, enveloped or not', () => {
    expect(messageFromBody({ detail: 'expected a .srt, .vtt or .ass file' }))
      .toBe('expected a .srt, .vtt or .ass file')
    expect(messageFromBody({ detail: { message: 'no video stream in this file' } }))
      .toBe('no video stream in this file')
  })

  it('returns undefined when the body holds no sentence, so the caller can fall back', () => {
    expect(messageFromBody({})).toBeUndefined()
    expect(messageFromBody({ error: { code: 'INTERNAL', message: '   ' } })).toBeUndefined()
    expect(messageFromBody(null)).toBeUndefined()
    expect(messageFromBody(42)).toBeUndefined()
  })
})

describe('errorMessage', () => {
  it('unwraps an envelope appended to an Error message', () => {
    const body = JSON.stringify({
      error: { code: 'UNPROCESSABLE', message: 'request failed', details: { message: 'a cached overlay image was corrupted; your media is fine' } },
    })
    expect(errorMessage(new Error(`422 Unprocessable Entity: ${body}`)))
      .toBe('a cached overlay image was corrupted; your media is fine')
  })

  it('drops the class name a failed job records', () => {
    expect(errorMessage(new Error('RuntimeError: upscale only supports media clips')))
      .toBe('upscale only supports media clips')
  })

  it('passes a non-JSON message through untouched', () => {
    expect(errorMessage(new Error('Failed to fetch'))).toBe('Failed to fetch')
    expect(errorMessage('plain string')).toBe('plain string')
  })
})

describe('responseError', () => {
  const fake = (status: number, statusText: string, body: string): Response =>
    ({ status, statusText, text: async () => body }) as unknown as Response

  it('prefers the backend sentence over the status line', async () => {
    const body = JSON.stringify({ error: { message: 'request failed', details: { message: "Couldn't read this file" } } })
    expect((await responseError(fake(422, 'Unprocessable Entity', body))).message)
      .toBe("Couldn't read this file")
  })

  it('falls back to the status line when the body says nothing', async () => {
    expect((await responseError(fake(500, 'Internal Server Error', ''))).message)
      .toBe('500 Internal Server Error')
    expect((await responseError(fake(404, 'Not Found', '{}'))).message).toBe('404 Not Found')
  })

  it('keeps the status line and a bounded tail for a non-JSON body', async () => {
    const msg = (await responseError(fake(502, 'Bad Gateway', '<html>' + 'x'.repeat(400) + '</html>'))).message
    expect(msg.startsWith('502 Bad Gateway: <html>')).toBe(true)
    expect(msg.length).toBeLessThan(340)
  })
})

// Bodies RECORDED from api/hardening.py itself (a FastAPI TestClient raising the
// same HTTPExceptions main.py raises), pasted verbatim — the point is that this
// reader is checked against the real wire format rather than a paraphrase of it.
const RECORDED: { what: string; body: string; expect: string }[] = [
  {
    what: 'a render failure (the missing-ffmpeg case, the one that matters most)',
    body: '{"error":{"code":"UNPROCESSABLE","message":"request failed","request_id":"1a42d3dd7ea6","details":{"error":"render_failed","message":"ffmpeg isn\'t available, so the preview couldn\'t be rendered.","ffmpeg":"sh: ffmpeg: command not found"}}}',
    expect: "ffmpeg isn't available, so the preview couldn't be rendered.",
  },
  {
    what: 'an upload that could not be ingested',
    body: '{"error":{"code":"UNPROCESSABLE","message":"request failed","request_id":"4eb57a963b44","details":{"file":"a.mp4","error":"couldn\'t_import","message":"Couldn\'t import this file — it may not be a valid video…","detail":"ffprobe exited 127"}}}',
    expect: "Couldn't import this file — it may not be a valid video…",
  },
  {
    what: 'a voiceover transcode failure (the legacy {error: sentence} detail)',
    body: '{"error":{"code":"UNPROCESSABLE","message":"request failed","request_id":"beade3c66b4a","details":{"error":"vo transcode failed: no such file"}}}',
    expect: 'vo transcode failed: no such file',
  },
  {
    what: 'a string detail (no details key at all)',
    body: '{"error":{"code":"NOT_FOUND","message":"session s_x not found","request_id":"4bfb6dc820a4"}}',
    expect: 'session s_x not found',
  },
  {
    what: 'a request-validation error',
    body: '{"error":{"code":"VALIDATION_ERROR","message":"invalid request","request_id":"068824ccde04","details":[{"type":"int_parsing","loc":["body","height"],"msg":"Input should be a valid integer, unable to parse string as an integer","input":"tall"}]}}',
    expect: 'height: Input should be a valid integer, unable to parse string as an integer',
  },
]

describe('recorded hardening envelopes', () => {
  for (const r of RECORDED) {
    it(`reads ${r.what}`, () => {
      expect(messageFromBody(JSON.parse(r.body))).toBe(r.expect)
    })
  }

  it('proves the key api.ts used to read is absent from every one of them', () => {
    // `body.detail.error` — the old parse — finds nothing in ANY real envelope,
    // which is why every one of these collapsed to "422 Unprocessable Entity".
    for (const r of RECORDED) {
      const body = JSON.parse(r.body) as { detail?: { error?: unknown } }
      expect(body.detail?.error).toBeUndefined()
    }
  })
})
