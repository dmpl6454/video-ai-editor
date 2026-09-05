/**
 * A release build with no `scheme` opens and closes instantly: expo-router
 * resolves the root URL via expo-linking at startup, and expo-linking THROWS in
 * production when the manifest has no scheme (node_modules/expo-linking/build/
 * Schemes.js, resolveScheme — a warning in __DEV__, an exception otherwise).
 * Nothing in a dev client or the unit suite exercises that branch, so this test
 * is the only thing standing between "all green" and a dead app on the phone.
 */
import appJson from "../../app.json";

test("app.json declares a non-empty URL scheme (release builds crash without one)", () => {
  const scheme = (appJson as { expo: { scheme?: unknown } }).expo.scheme;
  expect(typeof scheme).toBe("string");
  expect((scheme as string).length).toBeGreaterThan(0);
  expect(scheme).toMatch(/^[a-z][a-z0-9+.-]*$/);
});
