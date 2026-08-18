import React from 'react'
import { useStore } from '../store'
import { isMediaClip, clipEnd, type AnyClip } from '../types'
import { baseName } from '../lib/paths'
import { sampleKF, keyEps, type KFNum } from '../lib/overlay'
import { chordLabel } from '../keymap/engine'
import { setLivePipFraming } from '../lib/pipDraw'

/** Number input that re-seeds from the EDL but never stomps in-progress typing,
 *  and commits at most one dispatch per real change.
 *
 *  Replaces `defaultValue` inputs, which were actively dangerous here. React
 *  assigns `value` on MOUNT, and assigning the `value` IDL property sets the
 *  DOM's "dirty value flag" permanently. The media inspector's element tree is
 *  structurally identical for any two media clips, so selecting a different clip
 *  reconciles onto the SAME <input> node — which then kept displaying, and on
 *  blur COMMITTED, the previously selected clip's number onto the newly selected
 *  clip. Trimming clip A to In=0.50 and then clicking clip B showed B with A's
 *  0.50, and one keystroke wrote A's timing onto B. Silent data corruption.
 *
 *  Controlled-with-local-state (the same shape `Slider`/`ColorSlider` already
 *  use) re-seeds on undo / chat edits / timeline drags WITHOUT remounting, so
 *  the caret is never dropped mid-edit.
 */
function NumberField({ value, dp = 2, min, max, step = 0.1, width, onCommit, title }: {
  value: number
  dp?: number
  min?: number
  max?: number
  step?: number
  width?: number
  onCommit: (n: number) => void
  title?: string
}) {
  const seeded = value.toFixed(dp)
  const ref = React.useRef<HTMLInputElement>(null)
  const [local, setLocal] = React.useState(seeded)
  // Re-seed when the EDL moves underneath us — but not while this very field is
  // focused, or we'd overwrite what the user is typing.
  React.useEffect(() => {
    if (document.activeElement !== ref.current) setLocal(seeded)
  }, [seeded])

  const commit = () => {
    // An <input type=number> runs the HTML value-sanitization algorithm, so
    // "abc", "-", "." and "1e999" all arrive as "". Number("") === 0, which is
    // how clearing the In field used to silently commit in=0 and restore the
    // whole trimmed head of the clip.
    if (local.trim() === '' || !Number.isFinite(Number(local))) {
      setLocal(seeded)
      return
    }
    let v = Number(local)
    if (min != null) v = Math.max(min, v)
    if (max != null) v = Math.min(max, v)
    // Compare against the SEEDED display value, not the raw float: a bare
    // focus+blur must not append an op, clear the redo stack and force a
    // re-encode. (Same rule the video-fade block documents.)
    if (v.toFixed(dp) === seeded) {
      setLocal(seeded)
      return
    }
    onCommit(v)
  }

  return (
    <input ref={ref} type="number" step={step} min={min} max={max} title={title}
      style={width != null ? { width } : undefined}
      value={local}
      onChange={(e) => setLocal(e.target.value)}
      onBlur={commit}
      // Enter should commit; a bare number input otherwise only commits on blur,
      // so typing a value and hitting Enter looked like nothing happened.
      onKeyDown={(e) => { if (e.key === 'Enter') e.currentTarget.blur() }} />
  )
}

// Sub-half-second overlays are almost never intentional: at 30fps a 0.1s
// sticker is three frames. `set_clip_timing`'s Duration field has a floor of
// 0.1, so it is a permitted, silent outcome of a mis-typed number.
const SHORT_OVERLAY_S = 0.5

// Roles whose ROLE default is ALL CAPS, so the "Role default" option can say so
// instead of leaving the user to discover it by typing. Mirrors the `upper: true`
// rows in TextLayer's ROLE_STYLES and text_overlay.py's — a third list, but a
// purely cosmetic one: being wrong here mislabels a dropdown option, it does not
// change a pixel.
const ROLE_FORCES_CAPS = new Set(['super', 'hook'])

// The font each role actually renders in when `style.font` is unset. Needed
// because 'Inter-Black' is the schema default AND the "never touched" sentinel,
// so a hook clip whose style.font is unset renders in Bebas Neue, not Inter.
// Mirrors ROLE_STYLES in render/text_overlay.py; label-only, like the set above.
const ROLE_FONTS: Record<string, string> = {
  super: 'Anton-Regular', hook: 'BebasNeue-Regular', lower_third: 'Montserrat-Bold',
  caption: 'Inter-Black', label: 'Inter-Bold', watermark: 'Inter-Bold',
}

// Bundled faces with NO lowercase letterforms: their lowercase slots contain
// capitals, so "As typed" cannot show lowercase in them and no code change
// could make it. Measured, not assumed — in Bebas Neue at 170px, 'a' rasterises
// byte-identically to 'A' (4448 ink px each) and 'hello' to 'HELLO'; Anton, by
// contrast, differs (33176 vs 35043). This is the OTHER half of "Text layer only
// shows capital alphabets": the forced-caps rule was one cause, the Hook role's
// typeface is the other, and fixing only the first leaves Hook still all-caps.
const CAPS_ONLY_FONTS = new Set(['BebasNeue-Regular'])

/** Warning for an overlay too short to ever be seen.
 *
 *  This used to ALSO carry "Not visible at the playhead (6.25s) — this clip runs
 *  0.00–4.00s. Edits here still apply. [Jump to clip]". That has been removed on
 *  request: it fired constantly, most of all right after a split, where it
 *  reported an ordinary state as though something were wrong. The condition it
 *  described is normal — selecting a clip the playhead is not inside is how you
 *  edit one — and the two real problems behind it were fixed at the source
 *  instead: `splitTrackAt` now selects the piece UNDER the playhead (so the
 *  common case never arises), and the keyframe button clamps its time into the
 *  clip (so the one control the banner's "edits here still apply" was quietly
 *  wrong about now behaves).
 *
 *  The too-short warning stays because it reports something genuinely broken:
 *  round-5 finding M-02 was filed as "rotation sometimes doesn't work", and the
 *  truth was a 0.10s sticker — three frames at 30fps — that no amount of
 *  correct `set_clip_transform` commits could make visible.
 */
function ClipWindowNotice({ start, end }: { start: number; end: number }) {
  if (end - start >= SHORT_OVERLAY_S) return null
  return (
    <div style={{
      fontSize: 11, lineHeight: 1.5, marginBottom: 8, padding: '6px 8px',
      borderRadius: 4, border: '1px solid var(--line)',
      background: 'rgba(245,158,11,0.12)', color: 'var(--text)',
    }}>
      Only {(end - start).toFixed(2)}s long — raise Duration below to see it.
    </div>
  )
}

function isKeyframed(v: unknown): boolean {
  if (!v || typeof v !== 'object') return false
  const o = v as { keyframes?: unknown[] }
  return Array.isArray(o.keyframes) && o.keyframes.length > 0
}

/** Is there a keyframe on `v` at (approximately) clip-local time `t`?
 *  Tolerance is half a frame at 30fps — the playhead lands on frame
 *  boundaries and the stored time is a float, so exact equality never
 *  matches and the ◆ could never light up (or be removed). */
function keyAt(v: unknown, t: number, fps?: number): boolean {
  if (!isKeyframed(v)) return false
  const kfs = (v as { keyframes: [number, number][] }).keyframes
  const eps = keyEps(fps)
  return kfs.some((k) => Math.abs((k?.[0] ?? -1) - t) < eps)
}

// asScalar() used to live here and returned the LAST keyframe's value for an
// animated property. Every panel field read through it, so on a clip with keys
// at 0→1.0 and 4→3.0 the Scale slider showed 3.00 at every playhead position.
// Replaced by lib/overlay's sampleKF(v, localT, fallback) — the same
// interpolation the renderer (edl/keyframes.py) and the canvas layers use — so
// the inspector reports the pose actually on screen. Do not reintroduce a
// "just take a number out of it" helper: the value of an animated property is
// meaningless without a time.

export function Properties() {
  const edl = useStore((s) => s.edl)
  const sel = useStore((s) => s.selection)
  const dispatch = useStore((s) => s.dispatch)
  // Hooks MUST be called in the same order on every render. Pull playhead
  // here at the top so the count stays stable across the early-return paths
  // below (selecting a clip would otherwise add a hook → React #310).
  const playhead = useStore((s) => s.playhead)
  // Same "keep the hook count stable" rule as `playhead` above — the video-fade
  // section's "Jump there" button needs it, and that section only renders for
  // some selections.
  const setPlayhead = useStore((s) => s.setPlayhead)
  const setLiveTransform = useStore((s) => s.setLiveTransform)
  const framing = useStore((s) => s.framing)
  const setFraming = useStore((s) => s.setFraming)

  if (!sel || !edl) return (
    <div className="props">
      <h2>Properties</h2>
      <div style={{ color: 'var(--text-dim)', fontSize: 12, lineHeight: 1.8 }}>
        Nothing selected.
        <br />• Click a clip on the timeline to edit it here
        <br />• Drag a selected clip's edges to trim it
        <br />• {chordLabel('Mod+KeyB')} splits the clip at the playhead
      </div>
    </div>
  )

  const clip = findClip(edl, sel)
  if (!clip) return (
    <div className="props"><h2>Properties</h2><div style={{ color: 'var(--text-dim)' }}>Clip not found.</div></div>
  )

  const c = clip.c
  if (clip.t.type === 'sticker') {
    // Is this sticker already strictly above / below every sibling on its
    // track? set_clip_z 'front' assigns max(sibling z)+1 over a list that
    // INCLUDES the target, so clicking when already top-most still bumps z:
    // a commit, a cleared redo stack and a full preview re-encode for a
    // pixel-identical timeline. Disable the button instead (it also shows
    // the user the current state).
    const sibZ = clip.t.clips
      .filter((s) => s.id !== c.id)
      .map((s) => (s as unknown as { z?: number }).z ?? 0)
    const myZ = (c as unknown as { z?: number }).z ?? 0
    const canRaise = sibZ.some((z) => z >= myZ)
    const canLower = sibZ.some((z) => z <= myZ)
    return (
      <StickerProps
        c={c as unknown as StickerLike}
        trackLabel={clip.t.label ?? clip.t.id}
        canRaise={canRaise}
        canLower={canLower}
        playhead={playhead}
        dispatch={dispatch}
      />
    )
  }
  if (!isMediaClip(c)) {
    // Text clip (sticker tracks were handled above) — full editable inspector.
    return (
      <TextProps
        c={c as unknown as TextClipLike}
        trackLabel={clip.t.label ?? clip.t.id}
        canvas={edl.canvas}
        playhead={playhead}
        dispatch={dispatch}
      />
    )
  }

  // Media clip — full inspector.
  // Audio-lane clips (music/vo/audio tracks) hide Speed/Color/Transform:
  // the audio render path (audio_mix._audio_clip_filter) applies only
  // resample + delay + gain + fades + mute — effects/transform are ignored
  // and speed (atempo) isn't applied on audio lanes at all, so those
  // sections' commits were silent no-ops (tester issue 10). The backend
  // rejects them too (dispatch.py _reject_audio_lane_clip).
  const isAudioLane = ['audio', 'music', 'vo'].includes(clip.t.type)
  const speedRaw = (c as unknown as { speed?: number | null }).speed
  const speed = typeof speedRaw === 'number' ? speedRaw : 1.0
  const audio = (c as unknown as { audio?: { gain_db?: number; fade_in?: number; fade_out?: number; mute?: boolean } }).audio
  const tx = (c as unknown as { transform?: { x?: unknown; y?: unknown; rotation?: unknown; scale?: unknown; opacity?: unknown } }).transform
  const gain = audio?.gain_db ?? 0
  const fadeIn = audio?.fade_in ?? 0
  const fadeOut = audio?.fade_out ?? 0
  const muted = !!audio?.mute
  // Another top-level Clip field types.ts doesn't declare (see the note below).
  const fitCover = (c as unknown as { fit?: string }).fit === 'cover'
  // Framing mode is per-clip: selecting a different clip must not leave the
  // crop view open over one it does not belong to.
  const isFramingThis = framing?.clipId === c.id
  // Visual fade-from/to-black — top-level Clip fields (NOT audio.*), rendered
  // by compositor._build_clip_video_chain. types.ts omits them, hence the cast.
  const vf = c as unknown as { video_fade_in?: number; video_fade_out?: number }
  const videoFadeIn = vf.video_fade_in ?? 0
  const videoFadeOut = vf.video_fade_out ?? 0

  const effects = (c as unknown as { effects?: { type: string; params?: Record<string, number> }[] }).effects
  const colorEffect = effects?.find((e) => e.type === 'color' || e.type === 'color_grade')
  const colorParams = colorEffect?.params ?? {}

  const clipStart = (c as unknown as { start?: number }).start ?? 0
  // CLAMPED to the clip's own span, not just floored at 0.
  //
  // A keyframe time is clip-local, and the timeline draws each one at
  // `clip.start + t` and skips any that falls outside the clip's rect. So a
  // playhead sitting PAST the selected clip produced a keyframe at a time the
  // clip does not contain: stored, counted in the panel's "N keys" readout,
  // and impossible to see or reach on the timeline. Reported as "it didn't
  // show up in the video layer, yet the keyframe was marked".
  //
  // Very easy to hit right after a split, where the selection is the LEFT half
  // and the playhead is in the right. The banner above says "Not visible at
  // the playhead … Edits here still apply", which is true of every other field
  // and was quietly false of this one.
  const clipSpan = Math.max(0, (clipEnd(c as AnyClip) ?? clipStart) - clipStart)
  const localT = Math.min(Math.max(0, playhead - clipStart), clipSpan)

  // Transform values are read AT THE PLAYHEAD, which is why they are derived
  // here rather than beside the other fields above — they depend on localT.
  //
  // These used to come from asScalar(), which returns the LAST keyframe's value
  // on an animated property. On a clip with keys at 0→1.0 and 4→3.0 the Scale
  // slider therefore read 3.00 everywhere along the timeline, including where
  // the picture plainly showed 1.5. That was merely wrong to look at while a
  // slider drag overwrote the whole animation; now that a drag writes a key AT
  // the playhead, it would also mean nudging the slider snapped the clip from
  // its real value to the last key's. Sampling is the same math the renderer
  // and the canvas layers use (lib/overlay.sampleKF mirrors edl/keyframes.py).
  const rotation = sampleKF(tx?.rotation as KFNum | undefined, localT, 0)
  const scale = sampleKF(tx?.scale as KFNum | undefined, localT, 1)
  const opacity = sampleKF(tx?.opacity as KFNum | undefined, localT, 1)
  const xVal = sampleKF(tx?.x as KFNum | undefined, localT, 0)
  const yVal = sampleKF(tx?.y as KFNum | undefined, localT, 0)
  // ONE keyframe button for the whole transform.
  //
  // There used to be five — one per animatable property (scale, rotation,
  // opacity, X, Y) — which is how pro NLEs do it, and it read as "why are there
  // 5 buttons to add a keyframe" followed by "I can't see any keyframe added".
  // A keyframe here now means what it means in a consumer editor: a snapshot of
  // how the clip LOOKS at this instant. One click pins all five, so moving the
  // playhead and changing anything produces a move between two poses instead of
  // an animation on one property and static values on the others.
  //
  // Three states, kept from the per-property version — a flat "add" button gave
  // no way to tell whether a click had done anything and NO way to undo it
  // ("I was also unable to unmark the keyframe"; remove_keyframe existed in the
  // backend with no UI surface at all):
  //   • dim    — nothing animated yet; click keys the current pose
  //   • hollow — animated, but no key at the playhead; click adds one here
  //   • solid  — a key sits at the playhead; click REMOVES it (toggle off)
  //
  // Both directions go out as ONE dispatch carrying all five props (the
  // handlers take a `props` list), so one click is one undo step. Five separate
  // dispatches would let an Undo leave the clip keyed on some properties and
  // not others.
  const KF_PROPS = ['scale', 'rotation', 'opacity', 'x', 'y'] as const
  const kfValues: Record<string, unknown> = {
    scale: tx?.scale, rotation: tx?.rotation, opacity: tx?.opacity, x: tx?.x, y: tx?.y,
  }
  // Every distinct keyframe time on the clip, so the panel can SHOW them.
  const kfTimes = [...new Set(KF_PROPS.flatMap((p) => {
    const v = kfValues[p]
    return isKeyframed(v) ? (v as { keyframes: [number, number][] }).keyframes.map((k) => k[0]) : []
  }))].sort((a, b) => a - b)
  const kfAnimated = kfTimes.length > 0
  const kfHere = KF_PROPS.some((p) => keyAt(kfValues[p], localT, edl.canvas.fps))

  const KeyframeButton = () => (
    <button
      aria-label={`${kfHere ? 'Remove' : 'Add'} keyframe`}
      title={kfHere
        ? `Remove the keyframe at the playhead (${localT.toFixed(2)}s into the clip)`
        : `Add a keyframe at the playhead (${localT.toFixed(2)}s into the clip) — `
          + 'pins scale, rotation, opacity and position as they are now'}
      // Deliberately sends NO `values`. add_keyframe pins whatever each
      // property reads as AT `time` when it is left out — including the
      // interpolated value mid-animation — which is precisely "key the current
      // pose". The panel used to compute them itself with asScalar(), which
      // returns the LAST key's value, not the value at the playhead: with keys
      // at 0→1.0 and 4→3.0, pressing this at t=2 stored 3.0 where the clip
      // actually showed 2.0. So the one button whose contract is "changes
      // nothing, just pins it" visibly altered the animation. The backend
      // samples correctly for scalars too, so there is nothing left to pass.
      onClick={() => dispatch(kfHere ? 'remove_keyframe' : 'add_keyframe', {
        clip_id: c.id, props: [...KF_PROPS], time: localT,
      })}
      style={{
        background: kfHere ? 'var(--accent)' : 'var(--bg-3)',
        border: `1px solid ${kfAnimated ? 'var(--accent)' : 'var(--line)'}`,
        padding: '1px 8px', fontSize: 11, borderRadius: 3, cursor: 'pointer',
        color: kfAnimated ? 'inherit' : 'var(--text-dim)',
      }}
    >{kfHere ? '◆' : '◇'} Keyframe</button>
  )

  return (
    // key={c.id} is the belt to NumberField's braces: ANY selection change
    // unmounts this whole subtree, so no field — present or future — can carry
    // one clip's value across to another clip.
    <div className="props" key={c.id}>
      <h2>Properties</h2>
      <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 8 }} title={c.src}>
        {isAudioLane ? 'Audio clip · ' : ''}{clip.t.label} · {baseName(c.src)}
      </div>
      {/* Timeline footprint = source duration / speed (effective_duration
          server-side); a 2x clip ends halfway through its source length. */}
      <ClipWindowNotice
        start={clipStart}
        end={clipStart + (Math.max(0, ((c as unknown as { out?: number }).out ?? 0)
          - ((c as unknown as { in?: number }).in ?? 0)) / (speed > 0 ? speed : 1))}
      />

      <Section label="Timing">
        <div className="row two">
          <div className="field">
            <label>In (s)</label>
            <NumberField value={c.in} min={0}
              onCommit={(n) => dispatch('trim_clip', { clip_id: c.id, in: n })} />
          </div>
          <div className="field">
            <label>Out (s)</label>
            <NumberField value={c.out} min={0}
              onCommit={(n) => dispatch('trim_clip', { clip_id: c.id, out: n })} />
          </div>
        </div>
        <div className="field">
          <label>Start on timeline (s)</label>
          <NumberField value={c.start} min={0}
            onCommit={(n) => dispatch('move_clip', { clip_id: c.id, new_start: n })} />
        </div>
      </Section>

      {!isAudioLane && (
        <Section label="Speed" onReset={() => dispatch('set_speed', { clip_id: c.id, factor: 1 })}>
          <Slider min={0.25} max={4} step={0.05} value={speed}
            format={(v) => `${v.toFixed(2)}×`}
            onChange={(v) => dispatch('set_speed', { clip_id: c.id, factor: v })} />
        </Section>
      )}

      {!isAudioLane && (
        <Section label="Color" onReset={() => dispatch('color_grade', {
          clip_id: c.id, brightness: 0, contrast: 1, saturation: 1, temp: 0, tint: 0,
        })}>
          <ColorPanel clipId={c.id} dispatch={dispatch} current={colorParams} />
        </Section>
      )}

      {clip.t.id === 'v1' && (
        // v1-only, mirroring the backend: set_video_fade rejects audio lanes
        // AND v2/PIP clips (the pip overlay chain has no setpts shift and
        // would need an alpha-fade, not fade-to-black — see dispatch.py).
        // Showing the section on a v2 clip would just 400 with a toast.
        <Section label="Video fade" onReset={() => dispatch('set_video_fade', { clip_id: c.id, in_s: 0, out_s: 0 })}>
          {/* The visual fade the tester expected from the (audio-only) fade
              fields below. Key-seeded so undo/chat edits re-seed the inputs.
              The same-value guard compares against the SEEDED (2-dp) display
              value, not the raw EDL float: seeded from a chat-set 0.333 the
              field shows "0.33", so comparing to 0.333 would treat a
              focus-then-blur as an edit — committing an op, clearing redo,
              re-encoding the preview and silently rounding the stored value.
              Same rule as TextProps.commitNumber below. */}
          <div className="row two">
            <div className="field">
              <label>Video fade in (s)</label>
              <input type="number" step="0.05" min={0} max={5}
                key={`vfi${videoFadeIn.toFixed(2)}`} defaultValue={videoFadeIn.toFixed(2)}
                onBlur={(e) => {
                  const n = Number(e.target.value)
                  if (Number.isFinite(n) && Math.max(0, n) !== Number(videoFadeIn.toFixed(2)))
                    void dispatch('set_video_fade', { clip_id: c.id, in_s: Math.max(0, n) })
                }} />
            </div>
            <div className="field">
              <label>Video fade out (s)</label>
              <input type="number" step="0.05" min={0} max={5}
                key={`vfo${videoFadeOut.toFixed(2)}`} defaultValue={videoFadeOut.toFixed(2)}
                onBlur={(e) => {
                  const n = Number(e.target.value)
                  if (Number.isFinite(n) && Math.max(0, n) !== Number(videoFadeOut.toFixed(2)))
                    void dispatch('set_video_fade', { clip_id: c.id, out_s: Math.max(0, n) })
                }} />
            </div>
          </div>
          {/* Where the fade actually lands on the timeline. A fade is a
              property of THIS CLIP, not of the video: set 5s of fade-out on
              the first of two clips and it dips to black in the middle, not
              at the end — and looking anywhere else, nothing appears to have
              happened ("Fade out isn't working"). Spelling out the seconds
              turns that into something checkable. */}
          {(videoFadeIn > 0 || videoFadeOut > 0) && (() => {
            const cEnd = clipStart + Math.max(0, ((c as unknown as { out?: number }).out ?? 0)
              - ((c as unknown as { in?: number }).in ?? 0)) / (speed > 0 ? speed : 1)
            return (
              <div style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 6, lineHeight: 1.6 }}>
                {videoFadeIn > 0 && (
                  <div>Fades in {clipStart.toFixed(2)}–{(clipStart + videoFadeIn).toFixed(2)}s</div>
                )}
                {videoFadeOut > 0 && (
                  <div>Fades out {(cEnd - videoFadeOut).toFixed(2)}–{cEnd.toFixed(2)}s
                    {' '}<button
                      onClick={() => setPlayhead(Math.max(0, cEnd - videoFadeOut / 2))}
                      style={{
                        background: 'var(--bg-3)', border: '1px solid var(--line)',
                        borderRadius: 3, padding: '0 6px', fontSize: 11, cursor: 'pointer',
                        color: 'inherit',
                      }}>Jump there</button>
                  </div>
                )}
              </div>
            )
          })()}
        </Section>
      )}

      <Section label="Audio" onReset={() => {
        dispatch('set_volume', { target: c.id, db: 0 })
        dispatch('add_fade', { clip_id: c.id, in_s: 0, out_s: 0 })
      }}>
        <Slider min={-30} max={6} step={0.5} value={gain}
          format={(v) => `${v.toFixed(1)} dB`}
          onChange={(v) => dispatch('set_volume', { target: c.id, db: v })} />
        {/* "Audio" prefix is load-bearing: the section header scrolls out of
            small panels and a bare "Fade in" reads as a VIDEO fade (tester
            issue: "fade in and out is not working video" — the audio fade
            worked fine; the label promised more than add_fade delivers). */}
        <div className="row two">
          <div className="field">
            <label>Audio fade in (s)</label>
            <NumberField value={fadeIn} step={0.05} min={0} max={5}
              onCommit={(n) => dispatch('add_fade', { clip_id: c.id, in_s: n })} />
          </div>
          <div className="field">
            <label>Audio fade out (s)</label>
            <NumberField value={fadeOut} step={0.05} min={0} max={5}
              onCommit={(n) => dispatch('add_fade', { clip_id: c.id, out_s: n })} />
          </div>
        </div>
        <label style={{ fontSize: 11, color: 'var(--text-dim)' }}>
          <input type="checkbox" checked={muted} onChange={() => {
            // Real clip-level mute: flips audio.mute (which the checkbox
            // renders from, via the EDL refresh) and preserves gain_db.
            // The old set_volume{db:-60} proxy never set audio.mute — the
            // box never showed checked and unmute was impossible (issue 9).
            dispatch('set_clip_muted', { clip_id: c.id, muted: !muted })
          }} style={{ marginRight: 4 }} />
          Mute clip
        </label>
      </Section>

      {!isAudioLane && (
      <Section label="Framing">
        {/* The only framing control used to be the toolbar's aspect buttons,
            which just resize the canvas — so a landscape clip on a vertical
            canvas could only ever gain black bars ("there is no crop option for
            the video, only the aspect ratio gets changed"). 'Fill frame' is
            scale-up-and-crop; combine it with Transform scale/X/Y below to pick
            which part of the frame is visible. */}
        {/* V1 gets an explicit MODE, not a checkbox. Framing is a gesture with a
            beginning and an end, and the checkbox conflated it with the render
            property it needs: ticking it both set fit:'cover' AND opened the
            crop view, so there was no way to say "done" without also changing
            how the clip renders. Requested as a button to start framing and an
            Apply button to finish.

            The drags themselves still commit live, exactly as before — Apply
            closes the view, it does not defer the edit. Cancel is the one real
            behaviour change, and it is a fix: it restores the fit AND the
            transform captured on entry, where unticking used to reset x/y/scale
            to IDENTITY and silently discard a zoom set under `contain`.

            A PIP keeps the checkbox: `fit` there is only a render property (the
            crop view is v1-only), so there is no mode to enter or leave. */}
        {clip.t.id === 'v1' ? (
          isFramingThis ? (
            <div className="row" style={{ gap: 6, alignItems: 'center' }}>
              <button
                onClick={() => setFraming(null)}
                title="Finish framing and close the crop view — your changes are already applied"
                style={{ background: 'var(--accent)', fontWeight: 600 }}
              >Apply</button>
              <button
                onClick={() => {
                  // Put back exactly what was there on entry: the fit first,
                  // because set_clip_fit resets the transform on a cover→contain
                  // change and would otherwise undo the restore that follows it.
                  const b = framing?.before ?? {}
                  const wasCover = b.fit === 'cover'
                  if (!wasCover) {
                    void dispatch('set_clip_fit', { clip_id: c.id, fit: 'contain' })
                  }
                  void dispatch('set_clip_transform', {
                    clip_id: c.id,
                    x: b.x ?? 0, y: b.y ?? 0, scale: b.scale ?? 1,
                  })
                  setFraming(null)
                }}
                title="Discard this framing session and restore the clip as it was"
              >Cancel</button>
              <span style={{ fontSize: 10, color: 'var(--text-dim)' }}>
                Drag the picture in the preview; scroll to zoom.
              </span>
            </div>
          ) : (
            <div className="row" style={{ gap: 6, alignItems: 'center' }}>
              <button
                onClick={() => {
                  // Snapshot BEFORE the fit change, or the snapshot records the
                  // state this very click created and Cancel becomes a no-op.
                  setFraming({ clipId: c.id,
                               before: { fit: fitCover ? 'cover' : 'contain',
                                         x: xVal, y: yVal, scale } })
                  if (!fitCover) void dispatch('set_clip_fit', { clip_id: c.id, fit: 'cover' })
                }}
                title="Open the crop view over the preview to choose which part of the frame is visible"
              >Adjust framing…</button>
              {fitCover && (
                <button
                  onClick={() => dispatch('set_clip_fit', { clip_id: c.id, fit: 'contain' })}
                  title="Stop filling the frame — letterbox the whole picture instead"
                >Letterbox</button>
              )}
              <span style={{ fontSize: 10, color: 'var(--text-dim)' }}>
                {fitCover ? 'Filling the frame (cropped).' : 'Letterboxed — bars top/bottom.'}
              </span>
            </div>
          )
        ) : (
          <>
            <label style={{ fontSize: 11, color: 'var(--text-dim)' }}>
              <input type="checkbox" checked={fitCover}
                onChange={() => dispatch('set_clip_fit', {
                  clip_id: c.id, fit: fitCover ? 'contain' : 'cover',
                })}
                style={{ marginRight: 4 }} />
              Fill frame (crop the PIP to the canvas shape)
            </label>
            <div style={{ fontSize: 10, color: 'var(--text-dim)', marginTop: 4 }}>
              Off: the PIP keeps its source shape. On: it is centre-cropped to the
              canvas aspect. Choosing the Circle shape below crops to a square.
            </div>
          </>
        )}
      </Section>
      )}

      {/* PIP shape. Offered only on a v2+ video lane, because render/pip.py is
          the only path that cuts these: it applies the mask to the SCALED
          element, so the shape fits the picture-in-picture itself. On v1 a mask
          goes through effects.render_mask_png against the whole canvas, which
          is a different feature with different geometry — showing these buttons
          there would promise a circular clip and deliver a circle in the middle
          of a full-frame shot.

          Only the shapes that actually render appear. Mask.type also permits
          heart/star/mirror, which render_mask_png falls through to "fully
          visible" — a pre-existing no-op, and not something to surface as a
          button until it draws something. */}
      {!isAudioLane && clip.t.type === 'video' && clip.t.id !== 'v1' && (
      <Section label="PIP shape">
        <div className="row" style={{ gap: 6 }}>
          {([
            ['Rectangle', null],
            ['Circle', 'circle'],
            ['Rounded', 'rounded'],
          ] as const).map(([label, shape]) => {
            const cur = (c as unknown as { mask?: { type?: string } | null }).mask?.type ?? null
            const active = cur === shape
            return (
              <button
                key={label}
                onClick={() => dispatch(
                  shape ? 'add_mask' : 'remove_mask',
                  shape ? { clip_id: c.id, type: shape, feather: 0 } : { clip_id: c.id },
                )}
                style={{
                  flex: 1, fontSize: 11, padding: '3px 6px', borderRadius: 3,
                  cursor: 'pointer', color: 'inherit',
                  background: active ? 'var(--accent)' : 'var(--bg-3)',
                  border: `1px solid ${active ? 'var(--accent)' : 'var(--line)'}`,
                }}
              >{label}</button>
            )
          })}
        </div>
        {/* Framing WITHIN the shape. Separate from Transform below, which moves
            and sizes the PIP on the canvas: these choose which part of the
            source lands inside the circle/cropped box. Only meaningful when the
            element is actually cropped, so say so rather than offering three
            sliders that do nothing on a source-shaped PIP. */}
        {(() => {
          const pc = c as unknown as {
            mask?: { type?: string } | null; fit?: string
            framing?: { x?: number; y?: number; zoom?: number; rotation?: number } | null
          }
          const cropped = pc.mask?.type === 'circle' || pc.fit === 'cover'
          const fr = pc.framing ?? {}
          const set = (p: Record<string, number>) =>
            dispatch('set_pip_framing', { clip_id: c.id, ...p })
          if (!cropped) {
            return (
              <div style={{ fontSize: 10, color: 'var(--text-dim)', marginTop: 6 }}>
                Pick Circle, or turn on Fill frame above, to reframe the picture
                inside the shape.
              </div>
            )
          }
          return (
            <div style={{ marginTop: 8 }}>
              <div style={{ fontSize: 10, color: 'var(--text-dim)', marginBottom: 4 }}>
                Framing inside the shape
              </div>
              <Slider min={1} max={4} step={0.05} value={fr.zoom ?? 1}
                format={(v) => `zoom ${v.toFixed(2)}`}
                onChange={(v) => set({ zoom: v })} />
              <Slider min={-1} max={1} step={0.02} value={fr.x ?? 0}
                format={(v) => `pan X ${v.toFixed(2)}`}
                onChange={(v) => set({ x: v })} />
              <Slider min={-1} max={1} step={0.02} value={fr.y ?? 0}
                format={(v) => `pan Y ${v.toFixed(2)}`}
                onChange={(v) => set({ y: v })} />
              {/* Turns the PICTURE inside the shape; the shape stays put. The
                  element's own rotation (Transform below, or the handle above
                  the box in the preview) turns shape and picture together, and
                  the two compose — a PIP can sit at 20° with its footage
                  levelled at -20° inside. */}
              <Slider min={-180} max={180} step={1} value={fr.rotation ?? 0}
                format={(v) => `rotate inside ${v.toFixed(0)}°`}
                onLive={(v) => setLivePipFraming({ id: c.id,
                                                   x: fr.x ?? 0, y: fr.y ?? 0,
                                                   rotation: v })}
                onChange={(v) => { setLivePipFraming(null); set({ rotation: v }) }} />
              <div style={{ fontSize: 10, color: 'var(--text-dim)', marginTop: 2 }}>
                Or <strong>Alt-drag the PIP</strong> in the preview to pan the
                picture inside the shape. Pan only moves where there is margin —
                zoom in first if nothing happens. To turn the whole PIP instead,
                drag the <strong>handle above its box</strong> in the preview.
              </div>
            </div>
          )
        })()}
        <div style={{ fontSize: 10, color: 'var(--text-dim)', marginTop: 6 }}>
          Drag the PIP in the preview to move it; drag a corner to resize.
        </div>
      </Section>
      )}

      {!isAudioLane && (
      <Section label="Transform" onReset={() => dispatch('set_clip_transform', {
        clip_id: c.id, x: 0, y: 0, scale: 1, rotation: 0, opacity: 1,
      })}>
        {/* One button, and a readout of where the keys actually are — the
            timeline draws them on the clip too, but the panel is where you are
            looking when you press it ("I can't see any keyframe added"). */}
        <div className="row" style={{ alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <KeyframeButton />
          {kfAnimated ? (
            <span style={{ fontSize: 10, color: 'var(--text-dim)' }}>
              {kfTimes.length} key{kfTimes.length === 1 ? '' : 's'} ·{' '}
              {kfTimes.map((t, i) => (
                <span key={t}>
                  {i ? ', ' : ''}
                  <button
                    onClick={() => setPlayhead(clipStart + t)}
                    title={`Jump to this keyframe (${(clipStart + t).toFixed(2)}s)`}
                    style={{
                      background: 'none', border: 'none', padding: 0, cursor: 'pointer',
                      fontSize: 10, textDecoration: 'underline',
                      color: Math.abs(t - localT) < keyEps(edl.canvas.fps) ? 'var(--accent)' : 'var(--text-dim)',
                    }}
                  >{t.toFixed(2)}s</button>
                </span>
              ))}
            </span>
          ) : (
            <span style={{ fontSize: 10, color: 'var(--text-dim)' }}>
              no keyframes — move the playhead, press this, then change scale/position
            </span>
          )}
        </div>
        {/* Every transform edit carries the playhead as `time`. On a property
            with no keyframes the backend just sets the scalar, exactly as
            before; on an animated one it writes a key AT the playhead instead
            of replacing the whole animation with a number. Without this, the
            sequence this very panel recommends below — key it, move the
            playhead, change a value — deleted the key on the last step and left
            the clip static. */}
        <div className="row" style={{ alignItems: 'center', gap: 6 }}>
          <Slider min={0.1} max={4} step={0.05} value={scale}
            format={(v) => `scale ${v.toFixed(2)}`}
            onLive={(v) => setLiveTransform({ clipId: c.id, scale: v })}
            onChange={(v) => dispatch('set_clip_transform', { clip_id: c.id, scale: v, time: localT })} />
        </div>
        <div className="row" style={{ alignItems: 'center', gap: 6 }}>
          <Slider min={-180} max={180} step={1} value={rotation}
            format={(v) => `rotation ${v.toFixed(0)}°`}
            onLive={(v) => setLiveTransform({ clipId: c.id, rotation: v })}
            onChange={(v) => dispatch('set_clip_transform', { clip_id: c.id, rotation: v, time: localT })} />
        </div>
        <div className="row" style={{ alignItems: 'center', gap: 6 }}>
          <Slider min={0} max={1} step={0.05} value={opacity}
            format={(v) => `opacity ${v.toFixed(2)}`}
            onLive={(v) => setLiveTransform({ clipId: c.id, opacity: v })}
            onChange={(v) => dispatch('set_clip_transform', { clip_id: c.id, opacity: v, time: localT })} />
        </div>
        <div className="row" style={{ alignItems: 'center', gap: 6 }}>
          <label style={{ fontSize: 10, color: 'var(--text-dim)', minWidth: 80, display: 'flex', alignItems: 'center', gap: 4 }}>
            x:
            <NumberField value={xVal} dp={0} step={1} width={56}
              onCommit={(n) => dispatch('set_clip_transform', { clip_id: c.id, x: n, time: localT })} />
            {isKeyframed(tx?.x) ? '· animated' : ''}
          </label>
          <label style={{ fontSize: 10, color: 'var(--text-dim)', display: 'flex', alignItems: 'center', gap: 4 }}>
            y:
            <NumberField value={yVal} dp={0} step={1} width={56}
              onCommit={(n) => dispatch('set_clip_transform', { clip_id: c.id, y: n, time: localT })} />
            {isKeyframed(tx?.y) ? '· animated' : ''}
          </label>
        </div>
      </Section>
      )}

      <div className="row" style={{ marginTop: 8 }}>
        <button
          title={`Add a copy of this clip right after it (${chordLabel('Mod+KeyD')})`}
          onClick={() => dispatch('duplicate_clip', { clip_id: c.id })}
        >Duplicate</button>
        <button
          title="Remove this clip and close the gap (⌫)"
          onClick={() => dispatch('ripple_delete', { clip_id: c.id })}
        >Delete</button>
      </div>
    </div>
  )
}

interface StickerLike {
  id: string
  label?: string | null
  start: number
  end: number
  transform?: { x?: unknown; y?: unknown; scale?: unknown; rotation?: unknown; opacity?: unknown }
}

function StickerProps({ c, trackLabel, canRaise, canLower, playhead, dispatch }: {
  c: StickerLike
  trackLabel: string
  canRaise: boolean
  canLower: boolean
  playhead: number
  dispatch: ReturnType<typeof useStore.getState>['dispatch']
}) {
  const tx = c.transform ?? {}
  const start = c.start ?? 0
  const duration = Math.max(0.1, (c.end ?? start + 3) - start)
  // Read and write AT the playhead, for the same reasons as the media-clip
  // inspector above: a keyframed sticker's fields showed the last key's value
  // rather than the one on screen, and every edit here replaced the animation
  // with a scalar. Stickers are keyframable (add_keyframe takes Clip, TextClip
  // and Sticker alike), so leaving this panel alone would have fixed the bug
  // only for media clips.
  const localT = Math.min(Math.max(0, playhead - start), duration)
  const x = sampleKF(tx.x as KFNum | undefined, localT, 0)
  const y = sampleKF(tx.y as KFNum | undefined, localT, 0)
  const scale = sampleKF(tx.scale as KFNum | undefined, localT, 1)
  const rotation = sampleKF(tx.rotation as KFNum | undefined, localT, 0)
  const opacity = sampleKF(tx.opacity as KFNum | undefined, localT, 1)
  const setTx = (p: Record<string, number>) =>
    dispatch('set_clip_transform', { clip_id: c.id, ...p, time: localT })
  const setTiming = (p: { start?: number; end?: number }) =>
    dispatch('set_clip_timing', { clip_id: c.id, ...p })

  return (
    // See the media panel: key={c.id} guarantees a fresh field subtree per clip.
    <div className="props" key={c.id}>
      <h2>Properties</h2>
      <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 8 }}>
        {trackLabel} · {c.label ? `${c.label} ` : ''}Sticker · {c.id}
      </div>
      <ClipWindowNotice start={start} end={start + duration} />

      <Section label="Position">
        {/* NumberField re-seeds from the EDL when a canvas drag changes x/y, and
            rejects an emptied field instead of committing 0 (which would jump
            the sticker to the canvas origin). */}
        <div className="row two">
          <div className="field">
            <label>X</label>
            <NumberField value={x} dp={0} step={1}
              onCommit={(n) => setTx({ x: n })} />
          </div>
          <div className="field">
            <label>Y</label>
            <NumberField value={y} dp={0} step={1}
              onCommit={(n) => setTx({ y: n })} />
          </div>
        </div>
      </Section>

      <Section label="Transform">
        <Slider min={0.1} max={4} step={0.05} value={scale}
          format={(v) => `scale ${v.toFixed(2)}`} onChange={(v) => setTx({ scale: v })} />
        <Slider min={-180} max={180} step={1} value={rotation}
          format={(v) => `rotation ${v.toFixed(0)}°`} onChange={(v) => setTx({ rotation: v })} />
        <Slider min={0} max={1} step={0.05} value={opacity}
          format={(v) => `opacity ${v.toFixed(2)}`} onChange={(v) => setTx({ opacity: v })} />
      </Section>

      <Section label="Timing">
        <div className="row two">
          <div className="field">
            <label>Start (s)</label>
            <NumberField value={start} min={0}
              onCommit={(ns) => setTiming({ start: ns, end: ns + duration })} />
          </div>
          <div className="field">
            <label>Duration (s)</label>
            <NumberField value={duration} min={0.1}
              onCommit={(nd) => setTiming({ end: start + nd })} />
          </div>
        </div>
      </Section>

      <div className="row" style={{ marginTop: 8 }}>
        {/* Stacking is decided server-side by (track_z, clip_z, start) — with
            every sticker at the default z=0 the LATEST-added always composites
            on top and dragging (which only writes x/y) can't change it (tester
            issue: "latest emoji always overlaps"). set_clip_z is the existing,
            tested backend primitive; these buttons are its first UI surface. */}
        <button
          disabled={!canRaise}
          title={canRaise
            ? 'Composite this sticker above all overlapping stickers'
            : 'Already in front of every other sticker on this track'}
          onClick={() => dispatch('set_clip_z', { clip_id: c.id, z: 'front' })}
        >Bring to front</button>
        <button
          disabled={!canLower}
          title={canLower
            ? 'Composite this sticker below all overlapping stickers'
            : 'Already behind every other sticker on this track'}
          onClick={() => dispatch('set_clip_z', { clip_id: c.id, z: 'back' })}
        >Send to back</button>
        <button
          title="Remove this overlay from the timeline (⌫)"
          onClick={() => dispatch('ripple_delete', { clip_id: c.id })}
        >Delete</button>
      </div>
    </div>
  )
}

// The backend TextClip schema (edl/schema.py) always carries style/transform
// (pydantic default factories), so the dotted set_property paths below always
// resolve — including on auto_caption's caption cues. types.ts's TextClip
// interface deliberately omits transform ("M1 frontend ignores transform"),
// hence the local cast shape.
interface TextClipLike {
  id: string
  text: string
  start: number
  end: number
  role?: string | null
  // Mirrors TextStyle in edl/schema.py. `stroke`/`stroke_w`/`anim_in`/`anim_out`
  // were missing here (though types.ts declared them), which is why the outline
  // and animation fields looked unavailable to the inspector even though BOTH
  // renderers already honoured them.
  //
  // It happened AGAIN with `upper`: types.ts had it and this did not, so the
  // panel could not read the field it was written to set. This local copy exists
  // only because the props type is inlined here; anything added to TextStyle has
  // to land in three places (schema.py, types.ts, and this), and tsc only catches
  // the third once something reads it.
  style?: {
    font?: string; size?: number; color?: string
    stroke?: string; stroke_w?: number; upper?: boolean | null
  }
  anim_in?: string | null
  anim_out?: string | null
  transform?: { x?: unknown; y?: unknown; opacity?: unknown }
}

function TextProps({ c, trackLabel, canvas, playhead, dispatch }: {
  c: TextClipLike
  trackLabel: string
  canvas: { w: number; h: number }
  playhead: number
  dispatch: ReturnType<typeof useStore.getState>['dispatch']
}) {
  // Sampled at the playhead, and transform writes go through set_clip_transform
  // with a `time` — see the media-clip inspector. A TextClip does carry a
  // Transform (schema.py defaults it to x=540,y=1700), so it is keyframable and
  // was subject to the same clobbering; the stale comment on set_clip_transform
  // claiming "text clips don't" have one is what makes that easy to miss.
  const txLocalT = Math.min(Math.max(0, playhead - (c.start ?? 0)),
                            Math.max(0, (c.end ?? 0) - (c.start ?? 0)))
  const x = sampleKF(c.transform?.x as KFNum | undefined, txLocalT, canvas.w / 2)
  const y = sampleKF(c.transform?.y as KFNum | undefined, txLocalT, canvas.h * 0.85)
  const opacity = sampleKF(c.transform?.opacity as KFNum | undefined, txLocalT, 1)
  const setTx = (p: Record<string, number>) =>
    dispatch('set_clip_transform', { clip_id: c.id, ...p, time: txLocalT })
  const isCaption = c.role === 'caption'
  const size = c.style?.size ?? 96
  const rawColor = c.style?.color ?? '#FFFFFF'
  // <input type=color> only speaks #rrggbb — drop an alpha suffix if present.
  const color = /^#[0-9a-fA-F]{6}/.test(rawColor) ? rawColor.slice(0, 7) : '#ffffff'
  // Defaults mirror TextStyle in edl/schema.py so the control shows what the
  // renderer will actually use when the field is unset.
  const font = c.style?.font ?? 'Inter-Black'
  const rawStroke = c.style?.stroke ?? '#000000'
  const stroke = /^#[0-9a-fA-F]{6}/.test(rawStroke) ? rawStroke.slice(0, 7) : '#000000'
  const strokeW = c.style?.stroke_w ?? 4
  // Tri-state, so '' (role default) is a real, distinct choice — not the same as
  // an explicit false. A checkbox could not express it, and defaulting it to
  // false would silently un-capitalise every existing hook and super.
  const upperSel = typeof c.style?.upper === 'boolean' ? (c.style.upper ? 'on' : 'off') : ''
  // The font this clip RENDERS in: an explicit style.font, else the role's.
  // 'Inter-Black' doubles as the schema default and the "unset" sentinel, so it
  // cannot be taken at face value for a role clip.
  const effectiveFont = c.style?.font && c.style.font !== 'Inter-Black'
    ? c.style.font
    : (ROLE_FONTS[c.role ?? ''] ?? 'Inter-Black')
  // "As typed" is being asked for, in a face that has no lowercase to show.
  const capsOnlyFont = upperSel === 'off' && CAPS_ONLY_FONTS.has(effectiveFont)
  const animIn = c.anim_in ?? ''
  const animOut = c.anim_out ?? ''
  const start = c.start
  const end = c.end

  // Editing an existing TextClip = `set_property` (dispatch.py's generic
  // dotted-path mutator): paths `text`, `style.size`, `style.color`,
  // `transform.x`, `transform.y`. Timing goes through `set_clip_timing`
  // instead — it enforces end > start (clamps to a 0.1s minimum span) and
  // re-sorts the track, which a raw set_property on start/end would skip.
  const setProp = (path: string, value: unknown) =>
    dispatch('set_property', { clip_id: c.id, path, value })
  const setTiming = (p: { start?: number; end?: number }) =>
    dispatch('set_clip_timing', { clip_id: c.id, ...p })

  const commitText = (v: string) => {
    // Clearing the box CLEARS THE TEXT. Blank commits used to be skipped here,
    // on the theory that an empty TextClip renders nothing and is "only
    // recoverable through this same (now-empty-looking) inspector".
    //
    // That reasoning does not hold and the guard caused the reported bug:
    // "when I applied the text, and delete the text by backspace or delete, the
    // previous was still showing up, it should be empty and no text should be
    // there." Selecting all and deleting left the box empty while the preview
    // and the export kept the OLD string — the panel and the render disagreeing
    // about what the clip says, which is strictly worse than an empty overlay.
    //
    // Nor is it unrecoverable: the clip keeps its bar on the timeline and stays
    // selectable, so typing again is one click away — and the bar now reads
    // "(empty)" rather than looking like a broken blank clip. The renderer has
    // always handled this correctly: collect_text_clips skips a clip whose text
    // is blank, so no PNG is baked and no overlay is composited. Verified —
    // setting text='' caches 0 PNGs and emits no overlay.
    //
    // Deleting the CLIP is deliberately not done here: emptying a text box and
    // removing an element from the timeline are different intentions, and Delete
    // (right there in this panel) already does the second one.
    if (v !== c.text) void setProp('text', v)
  }
  // (The former local `commitNumber` guard now lives in the shared NumberField
  // component at the top of this file, which every numeric input routes
  // through — including the five media-inspector fields that had no guard and
  // no React key at all.)

  return (
    // See the media panel: key={c.id} guarantees a fresh field subtree per clip.
    <div className="props" key={c.id}>
      <h2>Properties</h2>
      <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 8 }}>
        {trackLabel} · {c.role ?? 'default'} · {c.id}
      </div>
      <ClipWindowNotice start={start} end={end} />

      <Section label="Text">
        {/* Uncontrolled + key-seeded like the sticker inputs: typing stays
            local; commit fires once on blur (or Cmd/Ctrl+Enter, routed
            through blur so there's a single commit path); an external change
            (chat edit, undo) re-seeds via the key. */}
        <textarea
          key={`t${c.id}:${c.text}`}
          defaultValue={c.text}
          rows={3}
          onBlur={(e) => commitText(e.target.value)}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
              e.preventDefault()
              ;(e.target as HTMLTextAreaElement).blur()
            }
          }}
          style={{ width: '100%', resize: 'vertical', fontSize: 12,
                   fontFamily: 'inherit', boxSizing: 'border-box' }}
        />
      </Section>

      <Section label="Style">
        <div className="row two">
          <div className="field">
            <label>Size (px)</label>
            <NumberField value={size} dp={0} step={1} min={8}
              onCommit={(n) => void setProp('style.size', n)} />
          </div>
          <div className="field">
            <label>Color</label>
            <input type="color" key={`c${color}`} defaultValue={color}
              title="Text fill color (#ffffff means: use the role's preset style)"
              onBlur={(e) => { const v = e.target.value; if (v !== color) void setProp('style.color', v) }}
              style={{ width: '100%', padding: 0, height: 24 }} />
          </div>
        </div>
        {/* Everything below was already honoured END TO END by both renderers
            and had no control at all — the "more controls for the text" gap.
            Each option list is deliberately limited to what BOTH sides support,
            so the browser preview matches the export:
              · fonts → the families declared in fonts.css, which is exactly what
                TextLayer's cssFont() can map (Anton, Bebas Neue, Montserrat,
                Inter Bold/Black). Offering a font the client can't map would
                silently preview in the role default and export differently.
              · anim → server ANIM_PRESETS ("pop","fade","slide_up","slide_down");
                TextLayer mirrors the same curves. */}
        <div className="row two">
          <div className="field">
            <label>Font</label>
            <select key={`f${font}`} defaultValue={font}
              title="Bundled font. Preview and export use the same file."
              onChange={(e) => { const v = e.target.value; if (v !== font) void setProp('style.font', v) }}
              style={{ fontSize: 12, padding: '3px 4px', width: '100%' }}>
              <option value="Inter-Black">Inter Black (default)</option>
              <option value="Inter-Bold">Inter Bold</option>
              <option value="Anton-Regular">Anton</option>
              <option value="BebasNeue-Regular">Bebas Neue (capitals only)</option>
              <option value="Montserrat-Bold">Montserrat Bold</option>
            </select>
          </div>
          <div className="field">
            <label>Outline width</label>
            <NumberField value={strokeW} dp={0} step={1} min={0} max={40}
              onCommit={(n) => void setProp('style.stroke_w', n)} />
          </div>
        </div>
        {/* Letter case. The caps rule used to be hardcoded in BOTH renderers'
            role tables with nothing in the schema to override it, so a lowercase
            hook or super simply could not be made — "Text layer only shows
            capital alphabets and doesn't support the small alphabets".
            Three options, not a checkbox, because the field is tri-state: the
            role default has to stay expressible, or picking it would write an
            explicit value and freeze the clip against future role changes. */}
        <div className="row two">
          <div className="field">
            <label>Letter case</label>
            <select key={`u${upperSel}`} defaultValue={upperSel}
              title="ALL CAPS is the house style for Hook and Super. Choose 'As typed' to keep lowercase."
              onChange={(e) => {
                const v = e.target.value
                void setProp('style.upper', v === '' ? null : v === 'on')
              }}
              style={{ fontSize: 12, padding: '3px 4px', width: '100%' }}>
              <option value="">Role default{ROLE_FORCES_CAPS.has(c.role ?? '') ? ' (ALL CAPS)' : ''}</option>
              <option value="on">ALL CAPS</option>
              <option value="off">As typed</option>
            </select>
          </div>
          <div className="field" />
        </div>
        {capsOnlyFont && (
          // Without this the control looks broken: you pick "As typed", nothing
          // changes, and the reason is in the typeface rather than the setting.
          <div style={{ fontSize: 10, color: 'var(--warn, #d8a657)', marginTop: -2 }}>
            Bebas Neue has no lowercase letters — its lowercase slots are drawn as
            capitals, so this stays uppercase. Pick another font to see lowercase.
          </div>
        )}
        <div className="row two">
          <div className="field">
            <label>Outline color</label>
            <input type="color" key={`sk${stroke}`} defaultValue={stroke}
              onBlur={(e) => { const v = e.target.value; if (v !== stroke) void setProp('style.stroke', v) }}
              style={{ width: '100%', padding: 0, height: 24 }} />
          </div>
          <div className="field">
            <label>Animate in / out</label>
            <div className="row" style={{ gap: 4 }}>
              <select key={`ai${animIn}`} defaultValue={animIn}
                onChange={(e) => void setProp('anim_in', e.target.value || null)}
                style={{ fontSize: 11, padding: '3px 2px', flex: 1 }}>
                <option value="">none</option>
                <option value="pop">pop</option>
                <option value="fade">fade</option>
                <option value="slide_up">slide up</option>
                <option value="slide_down">slide down</option>
              </select>
              <select key={`ao${animOut}`} defaultValue={animOut}
                onChange={(e) => void setProp('anim_out', e.target.value || null)}
                style={{ fontSize: 11, padding: '3px 2px', flex: 1 }}>
                <option value="">none</option>
                <option value="pop">pop</option>
                <option value="fade">fade</option>
                <option value="slide_up">slide up</option>
                <option value="slide_down">slide down</option>
              </select>
            </div>
          </div>
        </div>
        {/* transform.opacity — rendered by BOTH paths (server render_text_png
            alpha-multiplies the baked PNG; TextLayer multiplies globalAlpha),
            so this is a live control, not another dead field (tester issue 4:
            "more controls for the text like opacity"). */}
        <Slider min={0} max={1} step={0.05} value={opacity}
          format={(v) => `opacity ${v.toFixed(2)}`}
          onChange={(v) => setTx({ opacity: v })} />
      </Section>

      <Section label={`Position (canvas px, ${canvas.w}×${canvas.h})`}>
        {/* TextClip transform x/y are ABSOLUTE CANVAS PIXELS (clip centre),
            not relative units — 540/1700 is bottom-centre on a 1080×1920.
            Caption cues are the exception: both renderers deliberately ignore
            caption transforms (resolve_anchor_overrides returns (None, None)
            for role='caption'; TextLayer's resolveAnchor mirrors it), so live
            inputs here would be dead controls — show why instead. */}
        {isCaption ? (
          <div style={{ fontSize: 11, color: 'var(--text-dim)' }}>
            Caption position is controlled by the captions style, not per-cue
            coordinates.
          </div>
        ) : (
          <div className="row two">
            <div className="field">
              <label>X</label>
              <NumberField value={x} dp={0} step={1}
                onCommit={(n) => void setTx({ x: n })} />
            </div>
            <div className="field">
              <label>Y</label>
              <NumberField value={y} dp={0} step={1}
                onCommit={(n) => void setTx({ y: n })} />
            </div>
          </div>
        )}
      </Section>

      <Section label="Timing">
        <div className="row two">
          <div className="field">
            {/* These two DID guard for finiteness, but compared against the raw
                EDL float while displaying a 2-dp rounding of it — so re-blurring
                an unchanged field whose true value was e.g. 1.004 dispatched a
                no-op op, cleared the redo stack and forced a re-encode.
                NumberField compares against the SEEDED display value instead. */}
            <label>Start (s)</label>
            <NumberField value={start} min={0}
              onCommit={(n) => void setTiming({ start: n })} />
          </div>
          <div className="field">
            <label>End (s)</label>
            <NumberField value={end} min={0}
              onCommit={(n) => void setTiming({ end: n })} />
          </div>
        </div>
      </Section>

      <div className="row" style={{ marginTop: 8 }}>
        <button
          title="Remove this overlay from the timeline (⌫)"
          onClick={() => dispatch('ripple_delete', { clip_id: c.id })}
        >Delete</button>
      </div>
    </div>
  )
}

function Section({ label, children, onReset }: {
  label: string; children: React.ReactNode; onReset?: () => void;
}) {
  return (
    <div style={{ marginTop: 10 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                    margin: '8px 0 4px' }}>
        <div style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: 0.08 * 10 + 'em',
                      color: 'var(--text-dim)' }}>{label}</div>
        {onReset && (
          <button
            onClick={onReset}
            title={`Reset ${label.toLowerCase()} to default`}
            style={{
              fontSize: 10, padding: '1px 6px', background: 'transparent',
              border: '1px solid var(--line)', borderRadius: 3, color: 'var(--text-dim)',
              cursor: 'pointer',
            }}
          >Reset</button>
        )}
      </div>
      {children}
    </div>
  )
}

function Slider({ label, min, max, step, value, onChange, onLive, format }: {
  label?: string; min: number; max: number; step: number; value: number;
  onChange: (v: number) => void;        // committed value — dispatched to server
  onLive?: (v: number) => void;         // live value during drag — client-side only
  format?: (v: number) => string;       // optional live label formatter
}) {
  // Commit-on-release: the thumb + label track every drag tick locally (0ms),
  // but the server `onChange` fires ONCE on pointer-up / blur / key-release.
  // Dragging used to fire a dispatch + full preview render per tick — dozens of
  // HTTP round-trips and render jobs for one gesture. Now it's exactly one.
  const [local, setLocal] = React.useState(value)
  const dragging = React.useRef(false)
  // Keep local in sync when the prop changes from outside (undo, chat, etc.)
  // but never stomp the value mid-drag.
  React.useEffect(() => { if (!dragging.current) setLocal(value) }, [value])

  const commit = (v: number) => { if (v !== value) onChange(v) }
  return (
    <div className="row" style={{ alignItems: 'center', gap: 6 }}>
      <input type="range" min={min} max={max} step={step} value={local}
        onChange={(e) => {
          const v = Number(e.target.value)
          dragging.current = true
          setLocal(v)
          onLive?.(v)
        }}
        onPointerUp={(e) => { dragging.current = false; commit(Number((e.target as HTMLInputElement).value)) }}
        onPointerCancel={() => { dragging.current = false }}
        onKeyUp={(e) => { dragging.current = false; commit(Number((e.target as HTMLInputElement).value)) }}
        onBlur={(e) => { dragging.current = false; commit(Number((e.target as HTMLInputElement).value)) }}
        style={{ flex: 1 }} />
      <span style={{ fontSize: 10, color: 'var(--text-dim)', minWidth: 70, textAlign: 'right' }}>
        {format ? format(local) : label}
      </span>
    </div>
  )
}

function findClip(edl: ReturnType<typeof useStore.getState>['edl'], id: string) {
  if (!edl) return null
  for (const t of edl.tracks) {
    for (const c of t.clips) {
      if (c.id === id) return { t, c }
    }
  }
  return null
}

function ColorPanel({ clipId, dispatch, current }: {
  clipId: string;
  dispatch: ReturnType<typeof useStore.getState>['dispatch'];
  current: Record<string, number>;
}) {
  // Local sliders for shadows / mids / highlights gain + temp/tint. The
  // commit-on-release pattern (debounced via onPointerUp) avoids dispatching
  // a new effect for every pixel of slider drag. The backend merges each
  // commit into the clip's single "color" effect (dispatch.py color_grade),
  // so repeated adjustments settle on a final value instead of stacking.
  const setLiveFilter = useStore((s) => s.setLiveFilter)
  const commit = (params: Record<string, number>) => {
    dispatch('color_grade', { clip_id: clipId, ...params })
  }
  // Live CSS preview during a drag (the Color mirror of liveTransform). The
  // filter always carries all three mappable params seeded from the clip's
  // CURRENT grade — with the dragged one overriding — so dragging one slider
  // doesn't visually drop another's just-committed value while that value's
  // re-render is still in flight. Values stay in eq-param space; Preview.tsx
  // converts to CSS.
  const live = (p: { brightness?: number; contrast?: number; saturation?: number }) =>
    setLiveFilter({
      clipId,
      brightness: current.brightness ?? 0,
      contrast: current.contrast ?? 1,
      saturation: current.saturation ?? current.sat ?? 1,
      ...p,
    })
  return (
    <>
      <ColorSlider label="Brightness" min={-0.5} max={0.5} step={0.02} commit={(v) => commit({ brightness: v })}
        onLive={(v) => live({ brightness: v })}
        value={current.brightness} init={0}
        format={(v) => `${v >= 0 ? '+' : ''}${v.toFixed(2)}`} />
      <ColorSlider label="Contrast"   min={0.5}  max={2.0} step={0.02} commit={(v) => commit({ contrast: v })}
        onLive={(v) => live({ contrast: v })}
        value={current.contrast} init={1}
        format={(v) => `${v.toFixed(2)}×`} />
      <ColorSlider label="Saturation" min={0}    max={3.0} step={0.02} commit={(v) => commit({ saturation: v })}
        onLive={(v) => live({ saturation: v })}
        value={current.saturation ?? current.sat} init={1}
        format={(v) => `${v.toFixed(2)}×`} />
      {/* Temp/Tint stay commit-only (no onLive): the backend maps them to
          band-weighted colorbalance on midtones (render/effects.py), which
          CSS filter() has no faithful equivalent for — a wrong live preview
          would be worse than none. */}
      <ColorSlider label="Temp"       min={-1}   max={1}   step={0.02} commit={(v) => commit({ temp: v })}
        value={current.temp} init={0}
        format={(v) => `${v >= 0 ? '+' : ''}${Math.round(v * 100)}`} />
      <ColorSlider label="Tint"       min={-1}   max={1}   step={0.02} commit={(v) => commit({ tint: v })}
        value={current.tint} init={0}
        format={(v) => `${v >= 0 ? '+' : ''}${v.toFixed(2)}`} />
    </>
  )
}

function ColorSlider({ label, min, max, step, commit, onLive, value, init = 0, format }: {
  label: string; min: number; max: number; step: number;
  commit: (v: number) => void;          // committed value — dispatched to server
  onLive?: (v: number) => void;         // live value during drag — client-side only
  value?: number; init?: number; format?: (v: number) => string;
}) {
  // Controlled so the value readout tracks the thumb live; commit only fires
  // on release. `onPointerUp` (not `onMouseUp`) is the reliable cross-input
  // release event — mouse-only handlers can silently miss a release on some
  // touch/pen/trackpad interactions, which used to mean the color change
  // never got dispatched at all ("brightness works only sometimes").
  const seeded = value ?? init
  const [local, setLocal] = React.useState(seeded)
  const dragging = React.useRef(false)
  // Re-seed from the stored value when it changes from outside (switching
  // clips, undo/redo, chat edits) — but never mid-drag.
  React.useEffect(() => { if (!dragging.current) setLocal(seeded) }, [seeded])

  const release = (e: { target: EventTarget | null }) => {
    dragging.current = false
    // Same-value guard (mirrors Slider's `commit`): release fires from
    // pointerup AND the later blur — without the guard the blur re-commits
    // the identical value, appending a junk op to undo history.
    const v = Number((e.target as HTMLInputElement).value)
    if (v !== seeded) commit(v)
  }
  return (
    <div className="row" style={{ alignItems: 'center', gap: 6 }}>
      <span style={{ fontSize: 10, color: 'var(--text-dim)', minWidth: 64 }}>{label}</span>
      <input type="range" min={min} max={max} step={step} value={local}
        onChange={(e) => {
          const v = Number(e.target.value)
          dragging.current = true
          setLocal(v)
          onLive?.(v)
        }}
        onPointerUp={release}
        onKeyUp={release}
        onBlur={release}
        style={{ flex: 1 }} />
      <span style={{ fontSize: 10, color: 'var(--text)', minWidth: 46, textAlign: 'right',
                     fontVariantNumeric: 'tabular-nums' }}>
        {format ? format(local) : local.toFixed(2)}
      </span>
    </div>
  )
}
