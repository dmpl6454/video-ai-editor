/**
 * The local-network diagnosis is the most consequential pure logic in the app
 * and the only part of it that cannot be exercised on this machine — there is
 * no simulator here, and each wrong guess would otherwise cost a full EAS
 * build to discover. So it is pinned hard.
 */

import {
  baseUrl,
  classifyHost,
  DEFAULT_PORT,
  diagnoseLocalNetwork,
  diagnosisMessage,
  isAtsCleartextPermitted,
  isPrivateHost,
  isValidPort,
  LOCAL_NETWORK_GRACE_MS,
  LOCAL_NETWORK_RETRY_MS,
  MAX_THUMBS_IN_FLIGHT,
} from "../../lib/net";

import appConfig from "../../app.json";

describe("classifyHost", () => {
  it("recognises loopback", () => {
    expect(classifyHost("127.0.0.1")).toBe("loopback");
    expect(classifyHost("127.1.2.3")).toBe("loopback");
    expect(classifyHost("localhost")).toBe("loopback");
    expect(classifyHost("::1")).toBe("loopback");
  });

  it("recognises every RFC 1918 range, and only those", () => {
    expect(classifyHost("10.120.2.82")).toBe("private");
    expect(classifyHost("192.168.1.20")).toBe("private");
    expect(classifyHost("172.16.0.1")).toBe("private");
    expect(classifyHost("172.31.255.254")).toBe("private");
    // The classic off-by-one: 172.15 and 172.32 are public.
    expect(classifyHost("172.15.0.1")).toBe("public");
    expect(classifyHost("172.32.0.1")).toBe("public");
    // 192.169 is not 192.168.
    expect(classifyHost("192.169.1.1")).toBe("public");
  });

  it("recognises CGNAT (Tailscale) as its own class, not as public", () => {
    // The Mac's `is_private_ipv4` accepts 100.64-127 and `host_candidates()`
    // appends those addresses on purpose. Classifying them "public" made the
    // phone refuse its own Mac's QR code with "Do not use it."
    expect(classifyHost("100.64.0.1")).toBe("cgnat");
    expect(classifyHost("100.87.139.4")).toBe("cgnat");
    expect(classifyHost("100.127.255.254")).toBe("cgnat");
    // The boundaries, which the Mac also draws at 64 and 127.
    expect(classifyHost("100.63.255.255")).toBe("public");
    expect(classifyHost("100.128.0.1")).toBe("public");
    // Private in the routing sense — a stolen payload pointed here is not
    // leaving the user's own mesh…
    expect(isPrivateHost("100.87.139.4")).toBe(true);
    // …and yet UNREACHABLE, which is the distinction this pair of functions
    // exists to keep. A CFNetwork probe built with this app's own ATS
    // dictionary showed NSAllowsLocalNetworking does not cover 100.64/10, so
    // iOS refuses the cleartext load inside the app. When these two agreed,
    // the app told users a Tailscale address worked and then failed forever.
    expect(isAtsCleartextPermitted("100.87.139.4")).toBe(false);
  });

  it("recognises link-local and unique-local", () => {
    expect(classifyHost("169.254.10.4")).toBe("link-local");
    expect(classifyHost("fe80::1")).toBe("link-local");
    expect(classifyHost("fd00::1")).toBe("private");
  });

  it("recognises Bonjour names, including a trailing dot", () => {
    expect(classifyHost("studio-mac.local")).toBe("mdns");
    expect(classifyHost("studio-mac.local.")).toBe("mdns");
    expect(classifyHost("Studio-Mac.LOCAL")).toBe("mdns");
  });

  it("treats routable addresses and real domains as public", () => {
    expect(classifyHost("8.8.8.8")).toBe("public");
    expect(classifyHost("example.com")).toBe("public");
    expect(classifyHost("2606:4700::1111")).toBe("public");
  });

  it("rejects what is not a host", () => {
    expect(classifyHost("")).toBe("invalid");
    expect(classifyHost("   ")).toBe("invalid");
    expect(classifyHost("999.1.1.1")).toBe("invalid");
    expect(classifyHost("-bad.example")).toBe("invalid");
    expect(classifyHost("has space")).toBe("invalid");
  });

  it("unwraps a bracketed IPv6 authority", () => {
    expect(classifyHost("[fe80::1]")).toBe("link-local");
  });
});

describe("isAtsCleartextPermitted", () => {
  it("matches exactly what the ATS exception in app.json permits", () => {
    // NSAllowsLocalNetworking exempts loopback, link-local, RFC-1918 and
    // `.local` — that list, and nothing else.
    expect(isAtsCleartextPermitted("10.0.0.5")).toBe(true);
    expect(isAtsCleartextPermitted("192.168.1.20")).toBe(true);
    expect(isAtsCleartextPermitted("169.254.1.1")).toBe(true);
    expect(isAtsCleartextPermitted("mac.local")).toBe(true);
    expect(isAtsCleartextPermitted("127.0.0.1")).toBe(true);
    expect(isAtsCleartextPermitted("example.com")).toBe(false);
    expect(isAtsCleartextPermitted("8.8.8.8")).toBe(false);
  });

  it("excludes CGNAT across the whole 100.64/10 block", () => {
    // The regression this file exists to prevent: the function's own docstring
    // said ATS does not cover 100.64/10 while the function returned true for
    // it. Every boundary of the block, so a future edit cannot let one end
    // back in.
    for (const host of ["100.64.0.1", "100.87.139.4", "100.127.255.254"]) {
      expect(isAtsCleartextPermitted(host)).toBe(false);
      expect(isPrivateHost(host)).toBe(true);
    }
  });

  it("refuses to be satisfied by widening ATS in app.json", () => {
    // The tempting "fix" is NSAllowsArbitraryLoads, or an exception domain for
    // the Tailscale range. Both trade one clear message for cleartext loads to
    // anything that resolves into that range, so the manifest is pinned here
    // next to the predicate that depends on it.
    const ats = (
      appConfig as unknown as {
        expo: { ios: { infoPlist: { NSAppTransportSecurity: Record<string, unknown> } } };
      }
    ).expo.ios.infoPlist.NSAppTransportSecurity;
    expect(ats.NSAllowsArbitraryLoads).toBe(false);
    expect(ats.NSAllowsLocalNetworking).toBe(true);
    expect(ats.NSExceptionDomains).toBeUndefined();
  });
});

describe("isPrivateHost", () => {
  it("answers the routing question, not the ATS one", () => {
    expect(isPrivateHost("10.0.0.5")).toBe(true);
    expect(isPrivateHost("100.87.139.4")).toBe(true);
    expect(isPrivateHost("8.8.8.8")).toBe(false);
    expect(isPrivateHost("attacker.example")).toBe(false);
  });
});

describe("baseUrl", () => {
  it("builds a cleartext origin", () => {
    expect(baseUrl("10.0.0.5", DEFAULT_PORT)).toBe("http://10.0.0.5:8765");
  });

  it("brackets a bare IPv6 literal so the URL resolves to the right host", () => {
    expect(baseUrl("fe80::1", 8765)).toBe("http://[fe80::1]:8765");
    expect(baseUrl("[fe80::1]", 8765)).toBe("http://[fe80::1]:8765");
  });

  it("drops an mDNS trailing dot so two spellings are one origin", () => {
    expect(baseUrl("mac.local.", 8765)).toBe("http://mac.local:8765");
  });
});

describe("isValidPort", () => {
  it("accepts the usable range only", () => {
    expect(isValidPort(8765)).toBe(true);
    expect(isValidPort(1)).toBe(true);
    expect(isValidPort(65535)).toBe(true);
    expect(isValidPort(0)).toBe(false);
    expect(isValidPort(65536)).toBe(false);
    expect(isValidPort(80.5)).toBe(false);
    expect(isValidPort(Number.NaN)).toBe(false);
  });
});

describe("diagnoseLocalNetwork", () => {
  const LOCAL = "192.168.1.20";

  it("blames the address first, before anything else, when it cannot work", () => {
    // Even on a first run inside the grace window, a public host is hopeless —
    // ATS refuses it and no permission grant changes that.
    expect(
      diagnoseLocalNetwork({ host: "example.com", hasEverConnectedLocally: false, elapsedMs: 0 }),
    ).toEqual({ kind: "not_local" });
    expect(
      diagnoseLocalNetwork({ host: "example.com", hasEverConnectedLocally: true, elapsedMs: 999_999 }),
    ).toEqual({ kind: "not_local" });
  });

  it("calls a Tailscale address hopeless, and says why in its own words", () => {
    // Before the CFNetwork probe this returned `retry` and then
    // `permission_denied`, i.e. the app spent twelve seconds and then sent the
    // user to a Settings toggle that has no effect on an ATS refusal.
    const vpn = "100.87.139.4";
    expect(
      diagnoseLocalNetwork({ host: vpn, hasEverConnectedLocally: false, elapsedMs: 0 }),
    ).toEqual({ kind: "not_local" });
    expect(
      diagnoseLocalNetwork({ host: vpn, hasEverConnectedLocally: true, elapsedMs: 999_999 }),
    ).toEqual({ kind: "not_local" });

    const message = diagnosisMessage({ kind: "not_local" }, vpn);
    expect(message).toMatch(/VPN/i);
    // The user's next action, spelled out. "That is not a local address" is
    // useless here: the Mac's own pairing panel is where they got this one.
    expect(message).toMatch(/Wi-Fi address/i);
    expect(message).toMatch(/192\.168/);
    // And it is NOT the generic public-host sentence.
    expect(message).not.toBe(diagnosisMessage({ kind: "not_local" }, "example.com"));
  });

  it("retries quietly on a first run while the permission prompt may be up", () => {
    expect(diagnoseLocalNetwork({ host: LOCAL, hasEverConnectedLocally: false, elapsedMs: 0 })).toEqual({
      kind: "retry",
      afterMs: LOCAL_NETWORK_RETRY_MS,
    });
    expect(
      diagnoseLocalNetwork({ host: LOCAL, hasEverConnectedLocally: false, elapsedMs: LOCAL_NETWORK_GRACE_MS - 1 }),
    ).toEqual({ kind: "retry", afterMs: LOCAL_NETWORK_RETRY_MS });
  });

  it("concludes the permission was denied once the window closes", () => {
    expect(
      diagnoseLocalNetwork({ host: LOCAL, hasEverConnectedLocally: false, elapsedMs: LOCAL_NETWORK_GRACE_MS }),
    ).toEqual({ kind: "permission_denied" });
  });

  it("never blames permission once this install has reached a local host", () => {
    // The single most damaging wrong answer: sending someone to Settings to
    // fix a permission that is already granted, when the Mac is just asleep.
    for (const elapsed of [0, 5_000, LOCAL_NETWORK_GRACE_MS, 600_000]) {
      expect(
        diagnoseLocalNetwork({ host: LOCAL, hasEverConnectedLocally: true, elapsedMs: elapsed }),
      ).toEqual({ kind: "mac_unreachable" });
    }
  });

  it("leaves the grace window long enough for a person to read a prompt", () => {
    // Below ~6 s a slow reader is told to open Settings while the prompt they
    // were about to accept is still on screen.
    expect(LOCAL_NETWORK_GRACE_MS).toBeGreaterThanOrEqual(6_000);
  });
});

describe("diagnosisMessage", () => {
  it("says something specific and actionable for every branch", () => {
    const host = "192.168.1.20";
    const messages = [
      diagnosisMessage({ kind: "retry", afterMs: 1_500 }, host),
      diagnosisMessage({ kind: "permission_denied" }, host),
      diagnosisMessage({ kind: "not_local" }, host),
      diagnosisMessage({ kind: "mac_unreachable" }, host),
    ];
    for (const m of messages) expect(m.length).toBeGreaterThan(20);
    expect(new Set(messages).size).toBe(messages.length);
    expect(diagnosisMessage({ kind: "permission_denied" }, host)).toMatch(/Settings/);
    expect(diagnosisMessage({ kind: "mac_unreachable" }, host)).toMatch(/awake/);
  });
});

describe("MAX_THUMBS_IN_FLIGHT", () => {
  it("stays under the Mac's 60-per-second-per-path rate limit", () => {
    // api/hardening.py keys its sliding window on the path WITHOUT the query,
    // so every /thumb request in a filmstrip shares one bucket.
    expect(MAX_THUMBS_IN_FLIGHT).toBeLessThan(60);
    expect(MAX_THUMBS_IN_FLIGHT).toBe(24);
  });
});
