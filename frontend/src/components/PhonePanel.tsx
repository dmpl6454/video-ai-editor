// "Connect a phone" — the desktop half of pairing.
//
// This panel is the only place in the product where a user changes the
// machine's security posture, so it is written to be read rather than clicked
// through:
//
//   - LAN mode is OFF until someone turns it on here, and the switch says in
//     plain words what turning it on does (puts this Mac's editor on the local
//     network). It is a switch, not a button, because it is a state you can
//     leave armed by accident.
//   - The pairing code is a live credential for ten minutes. It is not shown
//     until the user asks for it, and the panel says so before showing it —
//     anyone who photographs the screen inside that window can drive this Mac.
//   - The sentence about which folders a connected phone can reach comes from
//     the backend (`allowed_roots_hint`) and is printed VERBATIM. Paraphrasing
//     a security promise is how a paraphrase and the code drift apart.
//   - The link is cleartext HTTP and the panel says so, right next to the
//     switch that creates it. That is the one exposure arming LAN mode adds
//     which the user cannot see for themselves, and until 0.6.0 neither half
//     of the product mentioned it — the phone's own Connect screen now carries
//     the same sentence.
//   - `restart_required` is stated rather than hidden. The bind address is
//     chosen once, before uvicorn starts (desktop.py), so arming LAN mode does
//     not move a socket that is already listening on 127.0.0.1. Showing a QR
//     code for an unreachable socket would waste the user's ten minutes and
//     make them blame the phone.
//
// The QR is generated in-process by `lib/pairQr.ts`; nothing here fetches
// anything from the network, which matters for a local-first editor that has
// to work with the router unplugged from the internet.

import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { api, type PairCode, type PairDevice, type PairInfo } from '../api'
import { errorMessage } from '../store'
import { encodeQr } from '../lib/pairQr'
import './phonePanel.css'

interface Props {
  onClose: () => void
}

export function PhonePanel({ onClose }: Props) {
  const [info, setInfo] = useState<PairInfo | null>(null)
  const [code, setCode] = useState<PairCode | null>(null)
  const [secondsLeft, setSecondsLeft] = useState(0)
  const [rootsHint, setRootsHint] = useState<string | null>(null)
  const [restartRequired, setRestartRequired] = useState(false)
  const [busy, setBusy] = useState<'lan' | 'code' | 'revoke' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const closeRef = useRef<HTMLButtonElement>(null)

  const load = useCallback(async () => {
    try {
      setInfo(await api.pairInfo())
      setError(null)
    } catch (e) {
      setError(errorMessage(e))
    }
  }, [])

  useEffect(() => {
    void load()
    // Focus goes to Close so the panel is dismissible from the keyboard the
    // instant it opens, without tabbing past a switch that arms LAN mode.
    closeRef.current?.focus()
  }, [load])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.code === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  // The countdown runs off a deadline captured when the code was minted, not
  // off a decrementing counter: a tab that is backgrounded stops firing
  // intervals, and a counter would come back claiming the code is still fresh.
  useEffect(() => {
    if (!code) return
    const deadline = Date.now() + code.expires_in_s * 1000
    const tick = () => setSecondsLeft(Math.max(0, Math.round((deadline - Date.now()) / 1000)))
    tick()
    const id = window.setInterval(tick, 1000)
    return () => window.clearInterval(id)
  }, [code])

  const toggleLan = useCallback(async () => {
    if (!info) return
    setBusy('lan')
    setError(null)
    try {
      const res = await api.setPairLan(!info.lan_enabled)
      setRootsHint(res.allowed_roots_hint)
      setRestartRequired(res.restart_required)
      // A code minted under the old posture is meaningless now; drop it rather
      // than leave a QR on screen that points at a socket about to change.
      setCode(null)
      await load()
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setBusy(null)
    }
  }, [info, load])

  const mint = useCallback(async () => {
    setBusy('code')
    setError(null)
    try {
      setCode(await api.newPairCode())
      await load()
    } catch (e) {
      setError(errorMessage(e))
      setCode(null)
    } finally {
      setBusy(null)
    }
  }, [load])

  const revoke = useCallback(async (device: PairDevice) => {
    setBusy('revoke')
    setError(null)
    try {
      await api.revokeDevice(device.id)
      await load()
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setBusy(null)
    }
  }, [load])

  const lanOn = info?.lan_enabled === true
  const expired = code !== null && secondsLeft <= 0

  return createPortal(
    <div className="phone-scrim" onClick={onClose}>
      <div
        className="phone-panel"
        role="dialog"
        aria-modal="true"
        aria-label="Connect a phone"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="phone-head">
          <div>
            <p className="phone-kicker">Companion</p>
            <h2>Connect a phone</h2>
            <p>
              The iPhone app is a remote control for this Mac. Every render, every AI tool and
              every file stays here — the phone shows the timeline and asks this Mac to do the work.
            </p>
          </div>
          <button ref={closeRef} onClick={onClose}>Close</button>
        </div>

        {error !== null && (
          <div className="phone-foot">
            <div className="phone-callout is-danger" role="alert">
              <strong>That did not work</strong>
              {error}
            </div>
          </div>
        )}

        <div className="phone-body">
          <div>
            <CodePlate code={code} lanOn={lanOn} expired={expired} />
            {code !== null && (
              <>
                <p className={`phone-countdown${expired ? ' is-expired' : ''}`}>
                  {expired
                    ? 'This code has expired. Show a new one.'
                    : `Expires in ${formatCountdown(secondsLeft)} · works once`}
                </p>
                <dl className="phone-kv">
                  <dt>Address</dt>
                  <dd>{`${code.host}:${code.port}`}</dd>
                  <dt>Code</dt>
                  <dd>{code.code}</dd>
                </dl>
                {code.hosts.length > 1 && (
                  <p className="phone-note">
                    This Mac also answers on {code.hosts.slice(1).join(', ')}. If the address above
                    does not work, type one of those into the phone with the same code.
                  </p>
                )}
              </>
            )}
          </div>

          <div>
            <section className="phone-section">
              <h3>1 · Let the phone in</h3>
              <button
                type="button"
                role="switch"
                aria-checked={lanOn}
                className="phone-switch"
                disabled={info === null || busy !== null}
                onClick={() => void toggleLan()}
              >
                <span className="track"><span className="knob" /></span>
                <span>
                  <span className="label">Allow my iPhone to connect</span>
                  <span className="sub">
                    {lanOn
                      ? 'On — this Mac accepts paired phones on your local network.'
                      : 'Off — this Mac only listens to itself. No phone can reach it.'}
                  </span>
                </span>
              </button>

              {restartRequired && (
                <div className="phone-callout" role="status">
                  <strong>Quit and reopen Video AI Editor</strong>
                  The setting is saved, but the app decides which network to listen on when it
                  starts. Until you restart it, this Mac is still on the old setting.
                </div>
              )}

              {lanOn && info !== null && info.hosts.length === 0 && (
                <div className="phone-callout">
                  <strong>This Mac has no local network address right now</strong>
                  Connect it to the same Wi-Fi as your iPhone, then show a code.
                </div>
              )}

              {rootsHint !== null && (
                <p className="phone-note">{rootsHint}</p>
              )}

              {/* The exposure the user is being asked to consent to, stated
                  next to the switch that creates it. NSAllowsLocalNetworking
                  permits plain HTTP to the LAN, so the bearer token, every
                  frame of preview video and every signed media URL cross the
                  Wi-Fi in cleartext — and the token has no expiry and is not
                  bound to a peer address, so a passive listener on a hostile
                  network can replay it for the life of the pairing. Nothing
                  else in the product said this. The fix for 0.6.0 is telling
                  the truth about the transport, not changing it. */}
              <p className="phone-note">
                This connection is not encrypted. Anyone who can watch your network can see the
                video the phone previews and could reuse its access to this Mac — so pair on a
                network you trust, and revoke the phone below if you ever pair somewhere you
                would rather not have.
              </p>
            </section>

            <section className="phone-section">
              <h3>2 · Show a pairing code</h3>
              <p className="phone-note">
                The code below is a key to this Mac for the next ten minutes. Anyone who photographs
                it in that time can pair their own phone, so show it only when the phone you want to
                pair is in front of you. It stops working the moment it is used.
              </p>
              <div className="phone-actions">
                <button
                  className="primary"
                  disabled={!lanOn || busy !== null}
                  onClick={() => void mint()}
                  title={lanOn
                    ? 'Mint a single-use pairing code and show its QR'
                    : 'Turn on “Allow my iPhone to connect” first'}
                >
                  {busy === 'code' ? 'Working…' : code === null ? 'Show a pairing code' : 'Show a new code'}
                </button>
                {code !== null && (
                  <button onClick={() => setCode(null)} title="Take the code off the screen">
                    Hide the code
                  </button>
                )}
              </div>
              {info !== null && info.pending_codes > 0 && (
                <p className="phone-note">
                  {info.pending_codes === 1
                    ? '1 code is still unclaimed. It expires on its own.'
                    : `${info.pending_codes} codes are still unclaimed. They expire on their own.`}
                </p>
              )}
            </section>

            <section className="phone-section">
              <h3>3 · Phones paired with this Mac</h3>
              {info === null ? (
                <p className="phone-note">Reading…</p>
              ) : info.devices.length === 0 ? (
                <p className="phone-note">
                  None yet. A phone appears here the moment it uses a code.
                </p>
              ) : (
                <ul className="phone-devices">
                  {info.devices.map((d) => (
                    <li key={d.id}>
                      <span className="who">
                        <span className="name">{d.name}</span>
                        <span className="meta">
                          {`Paired ${formatWhen(d.created_at)} · ${d.last_seen > 0 ? `last seen ${formatWhen(d.last_seen)}` : 'not seen yet'}`}
                        </span>
                      </span>
                      <button
                        disabled={busy !== null}
                        onClick={() => void revoke(d)}
                        title={`Stop ${d.name} from reaching this Mac. It takes effect on its next request.`}
                      >
                        Revoke
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              <p className="phone-note">
                Revoking is immediate: the phone's token and any live media link stop working on the
                next request. Nothing on this Mac is deleted.
              </p>
            </section>
          </div>
        </div>

        {info !== null && (
          <div className="phone-foot">
            A connected phone can queue {info.job_workers} render {info.job_workers === 1 ? 'job' : 'jobs'} at
            a time — the same slots you use — and upload files up to {formatBytes(info.max_upload_bytes)}.
            This setting lives in <code>{info.settings_path}</code>.
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}

// --- the plate -------------------------------------------------------------

function CodePlate({ code, lanOn, expired }: { code: PairCode | null; lanOn: boolean; expired: boolean }) {
  if (code === null) {
    return (
      <div className="phone-qr-empty">
        {lanOn
          ? 'The pairing code appears here. Open Video AI Editor on the iPhone and point its camera at it.'
          : 'Turn on “Allow my iPhone to connect” to show a pairing code.'}
      </div>
    )
  }

  let qr
  try {
    qr = encodeQr(code.payload)
  } catch {
    // A payload we cannot encode is a backend bug, not a user error. Falling
    // back to the typed values is better than an empty square, because the
    // address and code below the plate still work.
    return (
      <div className="phone-qr-empty">
        This code could not be drawn. Type the address and code into the phone instead.
      </div>
    )
  }

  return (
    <svg
      className="phone-qr"
      viewBox={`0 0 ${qr.size} ${qr.size}`}
      role="img"
      aria-label="Pairing QR code. The address and code are written underneath for typing in."
      style={expired ? { opacity: 0.35 } : undefined}
      shapeRendering="crispEdges"
    >
      <rect width={qr.size} height={qr.size} fill="var(--qr-plate)" />
      <path d={qr.path} fill="var(--qr-ink)" />
    </svg>
  )
}

// --- formatting ------------------------------------------------------------

function formatCountdown(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  return `${m}:${String(s).padStart(2, '0')}`
}

/** `created_at` / `last_seen` are UNIX floats in SECONDS (api/pairing.py). */
function formatWhen(epochSeconds: number): string {
  if (!Number.isFinite(epochSeconds) || epochSeconds <= 0) return 'unknown'
  const ms = Date.now() - epochSeconds * 1000
  if (ms < 60_000) return 'just now'
  const min = Math.floor(ms / 60_000)
  if (min < 60) return `${min} min ago`
  const h = Math.floor(min / 60)
  if (h < 24) return `${h} hr ago`
  const d = Math.floor(h / 24)
  return `${d} day${d === 1 ? '' : 's'} ago`
}

function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return 'an unknown size'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i += 1
  }
  return `${i === 0 ? Math.round(v) : v.toFixed(v >= 100 ? 0 : 1)} ${units[i]}`
}
