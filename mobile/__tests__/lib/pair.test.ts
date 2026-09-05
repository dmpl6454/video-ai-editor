/**
 * The pairing payload, and the security invariant that goes with it.
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import { basename, join } from "node:path";

import appConfig from "../../app.json";
import {
  hostAdvisory,
  PAIR_PAYLOAD_VERSION,
  pairFailureMessage,
  parsePairPayload,
  serialisePairPayload,
  type PairParseFailure,
} from "../../lib/pair";

/** `secrets.token_hex(16)` shaped — exactly what api/pairing.py mints. */
const CODE = "0123456789abcdef0123456789abcdef";

/** Every .ts/.tsx file under the given roots, with its text. */
function sourceFiles(roots: string[]): { path: string; text: string }[] {
  const out: { path: string; text: string }[] = [];
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir)) {
      const full = join(dir, entry);
      if (statSync(full).isDirectory()) walk(full);
      else if (/\.tsx?$/.test(entry)) out.push({ path: full, text: readFileSync(full, "utf8") });
    }
  };
  for (const r of roots) walk(r);
  return out;
}

describe("no pairing payload can arrive from a URL", () => {
  /**
   * THE INVARIANT. A pairing payload is a bearer credential plus a
   * destination. If a URL could deliver one, any web page the user visited
   * could silently re-point their phone at a host of the attacker's choosing.
   * So a payload may only enter this app through a deliberate physical act:
   * the camera, or the user typing it.
   *
   * Two halves, and both are checked, because the first alone is not enough.
   * `expo prebuild` ALWAYS registers the bundle identifier as a URL scheme —
   * `com.videoaieditor.mobile://` exists in the built app whatever app.json
   * says. What makes the invariant hold is not the absence of a scheme but the
   * absence of any code that feeds a URL to the parser, so that is asserted
   * directly against the source.
   */
  it("declares no URL scheme of its own in app.json", () => {
    // A `vae://` scheme would make the payload grammar itself addressable from
    // the web, which is a different and worse thing than an opaque bundle id.
    expect("scheme" in appConfig.expo).toBe(false);
    const plist = appConfig.expo.ios.infoPlist as Record<string, unknown>;
    expect(plist.CFBundleURLTypes).toBeUndefined();
  });

  it("parses a payload in exactly one file, and that file is the scanner", () => {
    const roots = [join(__dirname, "..", "..", "app"), join(__dirname, "..", "..", "lib")];
    const callers = sourceFiles(roots).filter(
      (f) => f.path !== join(__dirname, "..", "..", "lib", "pair.ts") && f.text.includes("parsePairPayload"),
    );
    expect(callers.map((f) => basename(f.path))).toEqual(["connect.tsx"]);
  });

  it("subscribes to no incoming URL anywhere in the app", () => {
    // `Linking.openSettings` is fine and is used. These two are the APIs that
    // let the outside world hand this app a URL in the first place.
    const roots = [join(__dirname, "..", "..", "app"), join(__dirname, "..", "..", "lib")];
    const offenders = sourceFiles(roots)
      .filter((f) => /getInitialURL|addEventListener\(\s*["']url["']/.test(f.text))
      .map((f) => basename(f.path));
    expect(offenders).toEqual([]);
  });

  it("keeps route parameters out of the one file that mints a credential", () => {
    /**
     * Deliberately narrower than "no screen may read route params": passing a
     * session id between screens is ordinary and other screens do it. What
     * must never happen is a route param reaching the pairing screen, because
     * prebuild registers the bundle id as a URL scheme and a route param is
     * therefore reachable from outside the app.
     */
    const connect = readFileSync(join(__dirname, "..", "..", "app", "connect.tsx"), "utf8");
    expect(connect).not.toMatch(/useLocalSearchParams|useGlobalSearchParams|getInitialURL/);
  });
});

describe("app transport security", () => {
  // Pinned here because pairing is the first thing that exercises it, and a
  // silent revert of either flag makes every request fail identically.
  it("permits local networking and nothing else", () => {
    const ats = appConfig.expo.ios.infoPlist.NSAppTransportSecurity;
    expect(ats.NSAllowsLocalNetworking).toBe(true);
    expect(ats.NSAllowsArbitraryLoads).toBe(false);
  });

  it("explains the local-network prompt to the user", () => {
    expect(appConfig.expo.ios.infoPlist.NSLocalNetworkUsageDescription).toMatch(/local network/i);
  });
});

describe("parsePairPayload", () => {
  it("round-trips a serialised payload", () => {
    const payload = { host: "10.120.2.82", port: 8765, code: CODE };
    const parsed = parsePairPayload(serialisePairPayload(payload));
    expect(parsed).toEqual({ ok: true, payload });
  });

  it("round-trips a payload on a non-default port", () => {
    const payload = { host: "192.168.1.20", port: 9000, code: CODE };
    const parsed = parsePairPayload(serialisePairPayload(payload));
    expect(parsed).toEqual({ ok: true, payload });
  });

  it("refuses a Bonjour host, because the Mac will 421 it", () => {
    // This assertion used to read `{ ok: true }`. A `.local` name passes ATS,
    // so accepting it looked reasonable — but `api/auth.py::host_header_allowed`
    // takes loopback names, `testserver` and bare IPv4 literals and refuses
    // every other NAME, with its own comment saying so. Accepting it spent a
    // round trip to land the user in `refused` for an address the app had just
    // told them was fine. Nothing the Mac produces is affected:
    // `host_candidates()` only ever emits IPv4 literals.
    const payload = { host: "mac.local", port: 8765, code: CODE };
    expect(parsePairPayload(serialisePairPayload(payload))).toEqual({
      ok: false,
      reason: "host_is_mdns",
    });
    expect(pairFailureMessage("host_is_mdns")).toMatch(/numeric address/i);
  });

  it("accepts a Tailscale (CGNAT) host and warns instead of refusing", () => {
    // `api/pairing.py::host_candidates` appends 100.64/10 deliberately, "so a
    // Tailscale-only setup still has something to show — the phone is the side
    // that decides whether to warn about them". Refusing told the user their
    // own Mac's QR code was an attack, with no way forward.
    const payload = { host: "100.87.139.4", port: 8765, code: CODE };
    expect(parsePairPayload(serialisePairPayload(payload))).toEqual({ ok: true, payload });
    expect(hostAdvisory("100.87.139.4")).toMatch(/VPN/i);
    // And an ordinary LAN address gets no advisory at all.
    expect(hostAdvisory("192.168.1.20")).toBeNull();
  });

  it("accepts the long parameter spellings the Mac may emit", () => {
    const parsed = parsePairPayload(`vaepair:host=10.0.0.5&port=8765&code=${CODE}`);
    expect(parsed).toEqual({ ok: true, payload: { host: "10.0.0.5", port: 8765, code: CODE } });
  });

  it("ignores parameters it does not know, so the Mac can add one", () => {
    const parsed = parsePairPayload(`vaepair:h=10.0.0.5&p=8765&c=${CODE}&fingerprint=abc&x=1`);
    expect(parsed.ok).toBe(true);
  });

  it("defaults the port to the desktop's own default", () => {
    const parsed = parsePairPayload(`vaepair:h=10.0.0.5&c=${CODE}`);
    expect(parsed.ok && parsed.payload.port).toBe(8765);
  });

  it("is case-insensitive about the prefix and the code's hex", () => {
    expect(parsePairPayload(`VAEPAIR:h=10.0.0.5&c=${CODE.toUpperCase()}`)).toEqual({
      ok: true,
      payload: { host: "10.0.0.5", port: 8765, code: CODE },
    });
  });

  const refusals: [string, PairParseFailure][] = [
    ["", "empty"],
    ["   ", "empty"],
    ["https://example.com/pair?c=x", "not_a_pair_payload"],
    // The provisional grammar this app was written against before
    // api/pairing.py landed. It must read as "not ours", not as a payload.
    [`vae://pair?h=10.0.0.5&t=${CODE}`, "not_a_pair_payload"],
    ["totally unrelated text", "not_a_pair_payload"],
    ["vaepair:", "missing_host"],
    [`vaepair:v=99&h=10.0.0.5&c=${CODE}`, "unsupported_version"],
    [`vaepair:h=&c=${CODE}`, "missing_host"],
    [`vaepair:h=999.1.1.1&c=${CODE}`, "bad_host"],
    [`vaepair:h=evil.example.com&c=${CODE}`, "host_not_local"],
    [`vaepair:h=8.8.8.8&c=${CODE}`, "host_not_local"],
    [`vaepair:h=127.0.0.1&c=${CODE}`, "host_is_loopback"],
    [`vaepair:h=localhost&c=${CODE}`, "host_is_loopback"],
    [`vaepair:h=10.0.0.5&p=0&c=${CODE}`, "bad_port"],
    [`vaepair:h=10.0.0.5&p=99999&c=${CODE}`, "bad_port"],
    ["vaepair:h=10.0.0.5&p=8765", "missing_code"],
    ["vaepair:h=10.0.0.5&c=short", "bad_code"],
    ["vaepair:h=10.0.0.5&c=zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz", "bad_code"],
  ];

  it.each(refusals)("refuses %p with reason %s", (input, reason) => {
    expect(parsePairPayload(input)).toEqual({ ok: false, reason });
  });

  it("refuses a code that is not exactly 32 hex characters", () => {
    const huge = "a".repeat(5_000);
    expect(parsePairPayload(`vaepair:h=10.0.0.5&c=${huge}`)).toEqual({ ok: false, reason: "bad_code" });
  });

  it("refuses a payload pointed at a public host even when it is well-formed", () => {
    // Worth stating on its own: this is the scan that would send a working
    // bearer token to somebody else's server.
    const parsed = parsePairPayload(serialisePairPayload({ host: "attacker.example", port: 443, code: CODE }));
    expect(parsed).toEqual({ ok: false, reason: "host_not_local" });
  });
});

describe("pairFailureMessage", () => {
  it("says what to do for every reason, and never repeats itself", () => {
    const reasons: PairParseFailure[] = [
      "empty", "not_a_pair_payload", "unsupported_version", "missing_host", "bad_host",
      "host_not_local", "host_is_loopback", "bad_port", "missing_code", "bad_code",
    ];
    const messages = reasons.map(pairFailureMessage);
    for (const m of messages) expect(m.length).toBeGreaterThan(10);
    // "missing"/"bad" pairs share copy on purpose; the rest must be distinct.
    expect(new Set(messages).size).toBeGreaterThanOrEqual(reasons.length - 2);
  });
});

describe("serialisePairPayload", () => {
  it("always stamps the current version", () => {
    expect(serialisePairPayload({ host: "10.0.0.5", port: 8765, code: CODE })).toContain(
      `v=${PAIR_PAYLOAD_VERSION}`,
    );
  });
});
