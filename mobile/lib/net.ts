/**
 * Addresses, and the one diagnosis this app absolutely has to get right.
 *
 * THE PROBLEM. On iOS every one of these produces the identical opaque
 * "Network request failed":
 *
 *   a) the Local Network permission prompt is on screen and unanswered;
 *   b) the user tapped Don't Allow, once, months ago;
 *   c) App Transport Security refused the cleartext load because the host is
 *      not on the local network at all;
 *   d) the Mac is asleep, the app is closed, or the phone is on cellular.
 *
 * There is no API that distinguishes them. Guessing wrong is expensive: it
 * sends the user to Settings when the Mac is simply asleep, or tells them to
 * wake a Mac that is already awake. And there is no simulator on the machine
 * this app is built from, so each wrong guess costs a full EAS build to find.
 *
 * THE APPROACH. Three facts we DO have, combined into one decision:
 *   - what kind of address the user is trying (`classifyHost`) — rules out (c);
 *   - whether this install has ever completed a request to a local host
 *     (`hasEverConnectedLocally`) — if it has, the permission was granted, so
 *     (a) and (b) are impossible and it must be (d);
 *   - how long we have been failing (`elapsedMs`) — the permission prompt is
 *     answered in seconds, so failing past the grace window means (b).
 *
 * Everything here is pure so it can be tested without a device.
 */

// ---------------------------------------------------------------------------
// Host classification
// ---------------------------------------------------------------------------

export type HostClass =
  /** 127.0.0.0/8 or ::1. Only reachable from the Mac itself. */
  | "loopback"
  /** RFC 1918 / RFC 4193 — a home or office LAN. */
  | "private"
  /** 169.254.0.0/16 or fe80:: — a direct cable/hotspot link with no DHCP. */
  | "link-local"
  /** A Bonjour name ("studio-mac.local"). */
  | "mdns"
  /**
   * 100.64.0.0/10 — carrier-grade NAT, which in practice on a developer's Mac
   * means Tailscale or another WireGuard mesh.
   *
   * Its own class because it is neither of the two things "public" implies. It
   * is not a routable internet address, so calling it hostile is wrong; and it
   * is not a Wi-Fi address, so iOS's Local Network permission does not apply
   * and the link only exists while the VPN is up. The Mac deliberately
   * advertises these — `api/pairing.py::host_candidates` appends them "so a
   * Tailscale-only setup still has something to show — the phone is the side
   * that decides whether to warn about them".
   *
   * What the phone decided, after a CFNetwork probe built with this app's own
   * ATS dictionary: cleartext to 100.64/10 is REFUSED BY ATS (see
   * `isAtsCleartextPermitted`), so the honest answer is a named refusal that
   * points at the Wi-Fi address, not a warning that implies the address works.
   * The class stays distinct from `public` precisely so that refusal can be
   * specific instead of "do not use it".
   */
  | "cgnat"
  /** A routable address or a real domain name. */
  | "public"
  /** Not a host at all. */
  | "invalid";

const IPV4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/;

// A hostname label: letters, digits, hyphens, not starting or ending with one.
const HOSTNAME = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$/i;

function classifyIPv4(octets: number[]): HostClass {
  const [a, b] = octets as [number, number, number, number];
  if (a === 127) return "loopback";
  if (a === 10) return "private";
  if (a === 192 && b === 168) return "private";
  if (a === 172 && b >= 16 && b <= 31) return "private";
  // 169.254/16 — what an interface self-assigns when there is no DHCP, e.g. a
  // Mac shared over a direct cable or a personal hotspot with nothing serving
  // leases. ATS's local-networking exception covers it, so it is reachable.
  if (a === 169 && b === 254) return "link-local";
  // 100.64/10. Matches `api/pairing.py::is_private_ipv4`, which accepts
  // 100.64-127 for exactly this reason.
  if (a === 100 && b >= 64 && b <= 127) return "cgnat";
  return "public";
}

/**
 * What sort of address this is. Case-insensitive; a trailing dot on an mDNS
 * name ("mac.local.") is accepted because Bonjour browsers hand it over that
 * way and the user should not have to know that is the same host.
 */
export function classifyHost(raw: string): HostClass {
  const host = raw.trim().toLowerCase().replace(/\.$/, "");
  if (!host) return "invalid";

  // Bracketed IPv6 as it appears in a URL authority.
  const bare = host.startsWith("[") && host.endsWith("]") ? host.slice(1, -1) : host;

  const v4 = IPV4.exec(bare);
  if (v4) {
    const octets = v4.slice(1, 5).map(Number);
    if (octets.some((n) => n > 255)) return "invalid";
    return classifyIPv4(octets);
  }

  if (bare.includes(":")) {
    // IPv6. We only need three answers, and getting them from prefixes is both
    // correct and far less code than a full parser.
    if (bare === "::1") return "loopback";
    if (/^fe[89ab][0-9a-f]:/.test(bare)) return "link-local";
    if (/^f[cd][0-9a-f]{2}:/.test(bare)) return "private";
    if (/^[0-9a-f:]+$/.test(bare)) return "public";
    return "invalid";
  }

  if (bare === "localhost") return "loopback";
  if (!HOSTNAME.test(bare)) return "invalid";
  if (bare.endsWith(".local")) return "mdns";
  return "public";
}

/**
 * Whether the address is PRIVATE — not routable on the public internet, and so
 * not a place a stolen pairing payload could send this phone's bearer token.
 *
 * This is a question about the address itself, and CGNAT counts: 100.64/10 is
 * carrier-grade NAT space, it is what a Tailscale interface hands out, and
 * `api/pairing.py::is_private_ipv4` accepts 100.64-127 for exactly that reason.
 *
 * It deliberately does NOT answer "can this phone actually load
 * `http://<host>`" — see `isAtsCleartextPermitted`. Conflating the two is the
 * bug this pair of functions exists to keep apart.
 */
export function isPrivateHost(host: string): boolean {
  const c = classifyHost(host);
  return (
    c === "private" || c === "link-local" || c === "mdns" || c === "loopback" || c === "cgnat"
  );
}

/**
 * Whether iOS will let us reach this host over cleartext HTTP under the ATS
 * exception in app.json (`NSAllowsLocalNetworking: true` with
 * `NSAllowsArbitraryLoads: false`). That exception covers link-local, RFC-1918
 * and `.local` names — and nothing else. A public host over http:// is blocked
 * by ATS before any packet leaves, which is a completely different failure
 * from "the Mac is asleep" and is why `classifyHost` exists.
 *
 * WHY CGNAT IS NOT IN THIS SET, THOUGH IT USED TO BE
 * --------------------------------------------------
 * This function once returned true for 100.64/10, on the reasoning that a
 * Tailscale interface is a VPN tunnel rather than a cleartext LAN hop and so
 * "Apple's exception is not what governs it". That reasoning is wrong, and a
 * native CFNetwork probe compiled with this app's verbatim ATS dictionary
 * proved it: NSAllowsLocalNetworking exempts loopback, link-local, RFC-1918
 * and `.local` ONLY. A `http://100.87.x.x:8765` load is refused by ATS inside
 * the app, before any packet reaches the tunnel — the VPN being up changes
 * nothing, because the request never gets that far. The docstring on the
 * `cgnat` class six lines up said as much all along while this function
 * contradicted it, so the phone told users a Tailscale address worked and
 * then failed on it forever.
 *
 * The honest fix is to keep it out of this set — NOT to widen ATS. Adding
 * NSAllowsArbitraryLoads, or an exception domain covering 100.64/10, would
 * trade one clear "use the Wi-Fi address instead" message for cleartext loads
 * permitted to any host that resolves into that range, and App Review asks for
 * a justification we do not have. `pair.ts` refuses these addresses at pairing
 * time and says which address to use instead.
 *
 * TWO SEPARATE CONSTRAINTS, OFTEN CONFUSED
 * ----------------------------------------
 * ATS is one of them and this function answers it. The Mac's `Host` allowlist
 * is the other, and it is stricter: `api/auth.py::host_header_allowed` accepts
 * loopback names, `testserver` and bare IPv4 literals, and refuses every other
 * NAME — its own comment says "a consequence worth stating:
 * `http://my-mac.local:8765` is refused". So `.local` passes ATS and is then
 * turned away with a 421 by the server. `pair.ts` is where that second
 * constraint is enforced, before a round trip is spent discovering it.
 */
export function isAtsCleartextPermitted(host: string): boolean {
  const c = classifyHost(host);
  return c === "private" || c === "link-local" || c === "mdns" || c === "loopback";
}

// ---------------------------------------------------------------------------
// Base URLs
// ---------------------------------------------------------------------------

/** desktop.py's default. `VAE_PORT` can move it, which is why pairing carries
 *  the port rather than assuming this. */
export const DEFAULT_PORT = 8765;

export function isValidPort(port: number): boolean {
  return Number.isInteger(port) && port >= 1 && port <= 65535;
}

/**
 * The origin to send requests to. Always cleartext http: the Mac serves plain
 * uvicorn on the LAN and there is no certificate to trust; the ATS exception
 * above is what permits it, and the pairing token is what secures it.
 *
 * IPv6 literals are bracketed here rather than at every call site, because
 * forgetting to do that produces a URL that parses and then resolves to the
 * wrong thing.
 */
export function baseUrl(host: string, port: number): string {
  const h = host.trim().replace(/\.$/, "");
  const needsBrackets = h.includes(":") && !h.startsWith("[");
  return `http://${needsBrackets ? `[${h}]` : h}:${port}`;
}


// ---------------------------------------------------------------------------
// Diagnosis
// ---------------------------------------------------------------------------

/**
 * How long to keep quietly retrying before concluding the Local Network
 * permission was denied rather than merely unanswered.
 *
 * WHY 12 s: the prompt appears within a second of the first request and a user
 * who is looking at their phone answers it in two or three. Twelve seconds
 * leaves room for someone who glanced away, while still being short enough
 * that a genuine denial does not leave the app spinning. Below about 6 s a
 * slow reader gets told to open Settings while the prompt is still on screen,
 * which is the worst outcome of all — the advice is wrong AND it dismisses the
 * prompt they were about to accept.
 */
export const LOCAL_NETWORK_GRACE_MS = 12_000;

/** Gap between silent retries during that window. */
export const LOCAL_NETWORK_RETRY_MS = 1_500;

/**
 * The rate limiter in api/hardening.py is keyed on the path WITHOUT its query
 * string, so every `/thumb?src=…&t=…` in a filmstrip shares one 60-requests-
 * per-second bucket. Twenty-four in flight is comfortably under that while
 * still filling a screen of thumbnails in one pass, and it is the same number
 * `lib/timeline.ts` caps a filmstrip at so the two cannot disagree.
 */
export const MAX_THUMBS_IN_FLIGHT = 24;

export interface DiagnosisInput {
  /** The host being dialled. */
  host: string;
  /** True once ANY request to a local host has succeeded on this install. */
  hasEverConnectedLocally: boolean;
  /** How long this run of consecutive failures has lasted. */
  elapsedMs: number;
}

export type LocalNetworkDiagnosis =
  /** Keep trying quietly; iOS is probably showing the permission prompt. */
  | { kind: "retry"; afterMs: number }
  /** Long enough. Offer the Settings deep link. */
  | { kind: "permission_denied" }
  /** ATS will never allow this address; the user typed a public host. */
  | { kind: "not_local" }
  /** The address is fine and permission is granted — so it is the Mac. */
  | { kind: "mac_unreachable" };

/**
 * Decide what a run of network failures actually means. See the file header
 * for the reasoning; the branch order below is the reasoning in code.
 */
export function diagnoseLocalNetwork(input: DiagnosisInput): LocalNetworkDiagnosis {
  const { host, hasEverConnectedLocally, elapsedMs } = input;

  // (c) — ATS refuses cleartext to anything outside the local-network
  // exception, so no amount of waiting or permission-granting will help. This
  // is checked first because it is the only branch that is certain. A CGNAT
  // (Tailscale) address lands here too, and `diagnosisMessage` says so in its
  // own words rather than with the generic "that is not a local address".
  if (!isAtsCleartextPermitted(host)) return { kind: "not_local" };

  // (d) — permission has demonstrably been granted at least once. iOS does not
  // silently revoke it, so the phone is fine and the Mac is not answering.
  if (hasEverConnectedLocally) return { kind: "mac_unreachable" };

  // (a) — first run, still inside the window where a prompt is plausibly on
  // screen. Retry without saying anything alarming.
  if (elapsedMs < LOCAL_NETWORK_GRACE_MS) return { kind: "retry", afterMs: LOCAL_NETWORK_RETRY_MS };

  // (b) — first run, past the window. Either denied, or the Mac was never
  // reachable to begin with; the Settings link is the only advice that can
  // unstick the former and it does no harm to the latter.
  return { kind: "permission_denied" };
}

/**
 * The one sentence every screen uses for a Tailscale/CGNAT address, so the
 * pairing refusal, the advisory and the connection diagnosis cannot drift into
 * telling the user three different stories about the same address.
 *
 * It names the fix, not the mechanism: "use the Wi-Fi address" is the only
 * thing the user can act on. The reason it is unfixable on this side is in
 * `isAtsCleartextPermitted` — ATS refuses the load before it leaves the app,
 * so having the VPN up does not help and there is nothing to retry.
 */
export const CGNAT_UNREACHABLE_ADVICE =
  "iPhone blocks plain HTTP to a VPN address like this one, so this app can never reach your Mac there — turning the VPN on does not change it. Use the Wi-Fi address your Mac's Phone panel shows instead, which looks like 192.168.1.20.";

/** One sentence per diagnosis. Written to be shown as-is. */
export function diagnosisMessage(d: LocalNetworkDiagnosis, host: string): string {
  switch (d.kind) {
    case "retry":
      return `Looking for your Mac at ${host}…`;
    case "permission_denied":
      return "iPhone is blocking this app from reaching your local network. Turn on Local Network for Video AI Editor in Settings, then try again.";
    case "not_local":
      // Two very different addresses arrive here and they need different
      // advice. A Tailscale address is one the MAC ITSELF advertises
      // (`api/pairing.py::host_candidates`), so "that is not your local
      // network, check the pairing panel" sends the user back to the panel
      // that just gave them the address — the loop this branch exists to break.
      if (classifyHost(host) === "cgnat") {
        return `${host} is a Tailscale (VPN) address. ${CGNAT_UNREACHABLE_ADVICE}`;
      }
      return `${host} is not an address on your local network. The Mac app is reached over Wi-Fi at an address like 192.168.1.20 — check the pairing panel on your Mac.`;
    case "mac_unreachable":
      return `No answer from ${host}. Check that your Mac is awake, on the same Wi-Fi, and that Video AI Editor is open on it.`;
  }
}
