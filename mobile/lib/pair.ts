/**
 * The pairing payload: what the Mac puts in the QR code, and what this app is
 * willing to accept back out of a camera.
 *
 * GRAMMAR. Defined by `api/pairing.py::pair_payload` on the Mac, which is the
 * single source of truth; this is its reader.
 *
 *   vaepair:v=1&h=<host>&p=<port>&c=<code>
 *
 * Everything after the colon is a standard urlencoded query string, so new
 * keys are additive and this parser ignores the ones it does not know.
 *
 * `c` IS NOT A BEARER TOKEN. It is a 128-bit hex CLAIM CODE: single-use, valid
 * for ten minutes, and worthless once redeemed. The phone exchanges it at
 * `POST /api/pair/claim` for the one and only copy of an actual bearer token,
 * which is what goes into the keychain. That indirection is why the code can
 * be short enough to read off a screen and type in by hand, and why a
 * photograph of a stale QR is not a live credential.
 *
 * WHY NO URL CAN DELIVER ONE OF THESE.
 * `vaepair:` is not a registered scheme and the app installs no deep-link
 * handler. If a URL could hand the app a pairing payload, any web page the
 * user visited could silently re-point their phone at an attacker's host, or
 * at a host of the attacker's choosing on the user's own network. A pairing
 * payload is a bearer credential plus a destination; it may only enter this
 * app through a deliberate physical act (pointing the camera at the Mac's own
 * screen) or through the user typing it. `vaepair:` here is just a
 * self-describing string that this module parses; the OS never sees it.
 *
 * The app DOES declare `scheme: "videoaieditor"` — it has to, or expo-linking
 * throws at launch in a release build (see `app/_layout.tsx`). That scheme is
 * a way in, so the deep-link surface behind it is kept empty on purpose:
 * `extra.router.sitemap: false` in app.json removes expo-router's built-in
 * `_sitemap` route listing, and `app/+not-found.tsx` replaces expo-router's
 * `Unmatched` screen so an unmatched `videoaieditor://…` lands on a dead end
 * that echoes nothing back. `__tests__/lib/pair.test.ts` pins that no code
 * feeds a URL to this parser; `__tests__/release/linking.test.ts` pins the
 * production linking table.
 *
 * WHY THE CODE IS STILL TREATED AS A SECRET.
 * Anyone who photographs it inside the ten-minute window can claim a token and
 * drive the Mac. The Mac limits the blast radius (short window, claiming burns
 * the code, per-device revocation, LAN mode as the master switch); this side
 * never logs it, and the bearer it becomes lives only in the iOS keychain —
 * see `lib/vault.ts`.
 */

import {
  CGNAT_UNREACHABLE_ADVICE,
  classifyHost,
  DEFAULT_PORT,
  isPrivateHost,
  isValidPort,
} from "./net";

/** Bumped only if the payload's meaning changes; unknown versions are refused
 *  rather than best-effort parsed, because a misread host is a wrong Mac. */
export const PAIR_PAYLOAD_VERSION = 1;

export interface PairPayload {
  host: string;
  port: number;
  /** The single-use claim code. Exchanged for a bearer at /api/pair/claim. */
  code: string;
}

export type PairParseFailure =
  | "empty"
  | "not_a_pair_payload"
  | "unsupported_version"
  | "missing_host"
  | "bad_host"
  | "host_not_local"
  | "host_is_loopback"
  | "host_is_mdns"
  | "host_is_vpn"
  | "bad_port"
  | "missing_code"
  | "bad_code";

export type PairParseResult =
  | { ok: true; payload: PairPayload }
  | { ok: false; reason: PairParseFailure };

/** Exactly `api/pairing.py::_CODE_RE` — `secrets.token_hex(16)`. Anchored at
 *  both ends so a truncated scan is refused rather than half-accepted. */
const CODE = /^[0-9a-f]{32}$/;

/** The prefix `api/pairing.py::_PAYLOAD_PREFIX` emits. */
const PREFIX = "vaepair:";

const ALIASES: Record<string, keyof PairPayload | "v"> = {
  h: "host",
  host: "host",
  p: "port",
  port: "port",
  c: "code",
  code: "code",
  v: "v",
  version: "v",
};

/** Human sentence for each refusal. Shown under the scanner, so each one says
 *  what to do rather than what went wrong. */
export function pairFailureMessage(reason: PairParseFailure): string {
  switch (reason) {
    case "empty":
      return "Nothing to read there.";
    case "not_a_pair_payload":
      return "That is not a Video AI Editor pairing code. On your Mac, open Video AI Editor and choose Phone.";
    case "unsupported_version":
      return "That pairing code is from a newer version of the Mac app. Update this app to match.";
    case "missing_host":
    case "bad_host":
      return "That pairing code does not contain a usable address for your Mac.";
    case "host_not_local":
      return "That pairing code points somewhere outside your local network. Do not use it.";
    case "host_is_loopback":
      return "That code has your Mac's private loopback address in it, which no other device can reach. Turn on local network mode in the Mac app's Phone panel and show the code again.";
    case "host_is_mdns":
      return "Your Mac only answers to the numeric address shown in its Phone panel, not to a .local name. Use the address the Mac is showing.";
    case "host_is_vpn":
      return `That code has your Mac's Tailscale (VPN) address in it. ${CGNAT_UNREACHABLE_ADVICE}`;
    case "bad_port":
      return "That pairing code has an invalid port.";
    case "missing_code":
    case "bad_code":
      return "That pairing code is incomplete. Show a fresh one on your Mac.";
  }
}

/**
 * Read a scanned or pasted payload. Every refusal is named, because the whole
 * point of a scanner is that the user cannot see what it read.
 */
export function parsePairPayload(raw: string): PairParseResult {
  const text = raw.trim();
  if (!text) return { ok: false, reason: "empty" };

  if (!text.toLowerCase().startsWith(PREFIX)) return { ok: false, reason: "not_a_pair_payload" };
  const query = text.slice(PREFIX.length);
  if (!query) return { ok: false, reason: "missing_host" };

  const fields: Partial<Record<keyof PairPayload | "v", string>> = {};
  for (const part of query.split("&")) {
    if (!part) continue;
    const eq = part.indexOf("=");
    if (eq === -1) continue;
    const key = ALIASES[decodeURIComponent(part.slice(0, eq)).toLowerCase()];
    if (!key) continue; // forward compatibility: ignore what we do not know
    let value: string;
    try {
      value = decodeURIComponent(part.slice(eq + 1).replace(/\+/g, " "));
    } catch {
      // A malformed percent-escape is not worth failing the whole scan over
      // unless it lands on a field we need, which the checks below will catch.
      continue;
    }
    fields[key] = value;
  }

  if (fields.v !== undefined && fields.v !== String(PAIR_PAYLOAD_VERSION)) {
    return { ok: false, reason: "unsupported_version" };
  }

  const host = fields.host?.trim() ?? "";
  if (!host) return { ok: false, reason: "missing_host" };
  const hostClass = classifyHost(host);
  if (hostClass === "invalid") return { ok: false, reason: "bad_host" };
  // A loopback address in a QR means the Mac generated the code without
  // knowing its own LAN address. It is a Mac-side bug with a specific fix, so
  // it gets its own refusal instead of the generic "not local" one.
  if (hostClass === "loopback") return { ok: false, reason: "host_is_loopback" };
  // A `.local` name passes ATS but the Mac will NEVER accept it:
  // `api/auth.py::host_header_allowed` takes loopback names, `testserver` and
  // bare IPv4 literals and refuses every other name — its own comment says
  // "a consequence worth stating: `http://my-mac.local:8765` is refused". The
  // phone has every fact needed to say so instantly and in better words, so
  // spending a round trip to land in `refused` is a worse version of the same
  // answer. Nothing the Mac produces carries an mDNS name; `host_candidates()`
  // only ever emits IPv4 literals.
  if (hostClass === "mdns") return { ok: false, reason: "host_is_mdns" };
  // Stated as "must be private" rather than "must not be `public`", because
  // this is the line that keeps a scanned bearer credential off the internet:
  // a new HostClass added later should have to opt IN to being pairable.
  if (!isPrivateHost(host)) return { ok: false, reason: "host_not_local" };
  // `cgnat` (100.64/10 — Tailscale) is refused, and this refusal has been
  // rewritten twice, so the history matters.
  //
  //   1. Originally it fell into `host_not_local`: "That pairing code points
  //      somewhere outside your local network. Do not use it." The Mac appends
  //      these addresses ON PURPOSE (`host_candidates()`: a Tailscale-only
  //      setup should "still have something to show"), so the app was calling
  //      the user's own QR code an attack, with no way forward.
  //   2. So it was ACCEPTED with a warning from `hostAdvisory` saying it works
  //      while the VPN is up. That is worse in a different way: it is false. A
  //      CFNetwork probe compiled with this app's verbatim ATS dictionary
  //      showed NSAllowsLocalNetworking does not cover 100.64/10, so iOS
  //      refuses the cleartext load inside the app — every request fails, VPN
  //      or no VPN, and the app had just promised the address was fine.
  //   3. Hence its own named refusal, which neither accuses the user nor lies
  //      to them: it says the address cannot work over plain HTTP and names
  //      the one that can. Widening ATS to make (2) true is not on the table;
  //      see `isAtsCleartextPermitted`.
  if (hostClass === "cgnat") return { ok: false, reason: "host_is_vpn" };

  const port = fields.port === undefined ? DEFAULT_PORT : Number(fields.port);
  if (!isValidPort(port)) return { ok: false, reason: "bad_port" };

  const code = fields.code?.trim().toLowerCase() ?? "";
  if (!code) return { ok: false, reason: "missing_code" };
  if (!CODE.test(code)) return { ok: false, reason: "bad_code" };

  return { ok: true, payload: { host, port, code } };
}

/**
 * A sentence to show ALONGSIDE a host the app is already pointed at, or null
 * when there is nothing worth saying.
 *
 * Separate from `pairFailureMessage` because it is shown next to an address
 * that is in play rather than one being rejected at the scanner — `connect.tsx`
 * renders it for `conn.host`, which can be a Mac saved long before this build.
 * The honesty rule for this app is that a screen never implies more than it
 * can do; an address the phone cannot legally send a cleartext byte to is
 * exactly that kind of fact, and staying silent about a saved Mac that never
 * connects is the failure this exists to prevent.
 */
export function hostAdvisory(host: string): string | null {
  if (classifyHost(host) !== "cgnat") return null;
  // This used to read "it works while the VPN is up". It does not work at all:
  // ATS refuses cleartext to 100.64/10 (see `isAtsCleartextPermitted`), which
  // is why `parsePairPayload` now refuses such a host outright. The advisory
  // survives for the one address that can still reach this function — a
  // Tailscale host saved by a build that accepted it — because a saved Mac
  // that silently never connects needs the same sentence, not silence.
  return `That is a VPN address, not a Wi-Fi one. ${CGNAT_UNREACHABLE_ADVICE}`;
}

/**
 * The canonical string for a payload — the same one `pair_payload()` builds on
 * the Mac. Exists so the parser can be tested against real input rather than
 * against hand-typed strings that drift from the producer.
 */
export function serialisePairPayload(p: PairPayload): string {
  const parts = [
    `v=${PAIR_PAYLOAD_VERSION}`,
    `h=${encodeURIComponent(p.host)}`,
    `p=${p.port}`,
    `c=${encodeURIComponent(p.code)}`,
  ];
  return `${PREFIX}${parts.join("&")}`;
}
