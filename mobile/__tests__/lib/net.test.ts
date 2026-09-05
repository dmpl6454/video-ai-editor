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
  isLocalNetworkHost,
  isValidPort,
  LOCAL_NETWORK_GRACE_MS,
  LOCAL_NETWORK_RETRY_MS,
  MAX_THUMBS_IN_FLIGHT,
} from "../../lib/net";

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
    // Reachable, but not via the ATS local-networking exception — see the
    // note on isLocalNetworkHost.
    expect(isLocalNetworkHost("100.87.139.4")).toBe(true);
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

describe("isLocalNetworkHost", () => {
  it("matches exactly what the ATS exception in app.json permits", () => {
    expect(isLocalNetworkHost("10.0.0.5")).toBe(true);
    expect(isLocalNetworkHost("169.254.1.1")).toBe(true);
    expect(isLocalNetworkHost("mac.local")).toBe(true);
    expect(isLocalNetworkHost("127.0.0.1")).toBe(true);
    expect(isLocalNetworkHost("example.com")).toBe(false);
    expect(isLocalNetworkHost("8.8.8.8")).toBe(false);
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
