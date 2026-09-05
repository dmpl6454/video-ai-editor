/**
 * The deep-link surface of a SHIPPED build, executed rather than read.
 *
 * WHY THIS FILE EXISTS. app.json declares `scheme: "videoaieditor"` because a
 * release build without one throws in expo-linking at launch — the crash that
 * shipped once already (`__tests__/release/scheme.test.ts`). A scheme is a way
 * in, though, and expo-router puts two routes of its own into the LINKING
 * config with no __DEV__ or NODE_ENV gate
 * (`expo-router/build/getLinkingConfig.js::getNavigationConfig`):
 * `_sitemap`, which renders a listing of every route in the app, and
 * `*not-found`, whose built-in view prints the incoming URL back and links to
 * `/_sitemap`. So `videoaieditor://_sitemap` opened the route listing in a
 * production binary. This app has no deep-link surface on purpose — a pairing
 * payload is a bearer credential, see `lib/pair.ts` — and that was an
 * unintended one.
 *
 * HOW IT IS TESTED. By building the same linking table the app builds at
 * launch: expo-router's own `getRoutes` + `getLinkingConfig`, over the real
 * `app/` directory, with the options `expo-router/build/global-state/
 * useStore.js` passes, and with expo-constants mocked as a STANDALONE build's
 * manifest (`expoConfig` = the `expo` object out of our app.json, no dev-server
 * extras) under `__DEV__ === false`. Reading the config knob out of app.json
 * would only pin that we wrote a line; this pins that expo-router acts on it,
 * which is the part a patch bump could change.
 */

// Both must be set before expo-router's modules are required: its route
// scanning and its warnings branch on them at import time, and a test that
// runs with the dev-time shape is exactly how the original defect stayed
// invisible.
(globalThis as { __DEV__?: boolean }).__DEV__ = false;
process.env.NODE_ENV = "production";

import { readdirSync, statSync } from "node:fs";
import { join, relative, resolve, sep } from "node:path";

import appConfig from "../../app.json";

/**
 * A standalone build's Constants: `expoConfig` is app.json's `expo` object and
 * nothing else. expo-router reads `expoConfig.extra.router` in three places
 * (useStore, getLinkingConfig, global-state/utils), so this mock is what makes
 * the assertions below about OUR configuration rather than about a default.
 */
jest.mock("expo-constants", () => ({
  __esModule: true,
  default: {
    expoConfig: jest.requireActual("../../app.json").expo,
    executionEnvironment: "standalone",
    appOwnership: null,
  },
}));

// jest-expo's preset does not provide these SDK-57 native classes, and
// `app/_layout.tsx` is required for real below (expo-router loads every layout
// while it scans routes, to read `unstable_settings`). Same three stubs as
// `__tests__/release/coldStart.test.ts`, for the same reason.
jest.mock("expo-video", () => {
  const React = require("react");
  const { View } = require("react-native");
  return {
    VideoView: (props: Record<string, unknown>) => React.createElement(View, props),
    useVideoPlayer: () => ({
      play() {},
      pause() {},
      replace() {},
      currentTime: 0,
      duration: 0,
      playing: false,
      addListener() {
        return { remove() {} };
      },
    }),
  };
});
jest.mock("expo-file-system", () => ({
  File: class {
    uri = "file:///stub";
    constructor(..._a: unknown[]) {}
  },
  Directory: class {
    uri = "file:///stub/";
    constructor(..._a: unknown[]) {}
  },
  Paths: { cache: { uri: "file:///cache/" }, document: { uri: "file:///doc/" }, availableDiskSpace: 1e12 },
}));
jest.mock("expo-media-library", () => ({
  requestPermissionsAsync: async () => ({ granted: true, accessPrivileges: "all" }),
  saveToLibraryAsync: async () => undefined,
  createAssetAsync: async () => ({ id: "stub" }),
}));

const APP_DIR = resolve(__dirname, "..", "..", "app");

/**
 * The Metro `require.context` expo-router is handed at runtime, built from the
 * real files on disk so the route table under test cannot drift from the app.
 */
function appRequireContext() {
  const keys: string[] = [];
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir)) {
      const full = join(dir, entry);
      if (statSync(full).isDirectory()) walk(full);
      else if (/\.(ts|tsx|js|jsx)$/.test(entry)) {
        keys.push(`./${relative(APP_DIR, full).split(sep).join("/")}`);
      }
    }
  };
  walk(APP_DIR);

  const context = (key: string) => require(join(APP_DIR, key));
  context.keys = () => keys;
  context.resolve = (key: string) => key;
  context.id = "app";
  return context as unknown as Parameters<typeof import("expo-router/build/getRoutes").getRoutes>[0];
}

/** The linking config the app hands React Navigation, built the way it is at
 *  launch. Mirrors `expo-router/build/global-state/useStore.js`. */
function productionLinkingConfig() {
  const { getRoutes } = require("expo-router/build/getRoutes");
  const { getLinkingConfig } = require("expo-router/build/getLinkingConfig");
  const Constants = require("expo-constants").default;

  const config = Constants.expoConfig?.extra?.router;
  const context = appRequireContext();
  const routeNode = getRoutes(context, {
    ...config,
    skipGenerated: true,
    ignoreEntryPoints: true,
    platform: "ios",
    preserveRedirectAndRewrites: true,
  });
  expect(routeNode).not.toBeNull();

  return getLinkingConfig(routeNode, context, () => ({ segments: [] }), {
    metaOnly: true,
    redirects: [],
    skipGenerated: config?.skipGenerated ?? false,
    sitemap: config?.sitemap ?? true,
    notFound: config?.notFound ?? true,
  });
}

describe("the production deep-link table", () => {
  it("runs with the production flags the defect needed to be visible", () => {
    expect((globalThis as { __DEV__?: boolean }).__DEV__).toBe(false);
    expect(process.env.NODE_ENV).toBe("production");
  });

  it("exposes no _sitemap route", () => {
    const linking = productionLinkingConfig();
    const screens = linking.config?.screens ?? {};

    expect(Object.keys(screens)).not.toContain("_sitemap");
    // Nested too: the route listing is reachable by PATH, so the whole table
    // is what has to be clean, not just its top level.
    expect(JSON.stringify(linking.config)).not.toContain("_sitemap");
  });

  it("is expo-router itself that agrees the sitemap is off, not just app.json", () => {
    // `shouldAppendSitemap` is what ExpoRoot uses to decide whether to mount
    // the Sitemap screen in the root stack. It reads the same manifest value,
    // so if a patch bump renames the knob this fails here rather than on a
    // phone.
    const { shouldAppendSitemap } = require("expo-router/build/global-state/utils");
    expect(shouldAppendSitemap()).toBe(false);
    expect(appConfig.expo.extra.router.sitemap).toBe(false);
  });

  it("answers an unmatched link with this app's own screen, not expo-router's", () => {
    // expo-router injects `views/Unmatched` when the app ships no +not-found
    // (`getRoutesCore.js::appendNotFoundRoute`). That view renders the incoming
    // URL and a hardcoded link to /_sitemap, in production as much as in dev.
    // Shipping `app/+not-found.tsx` is what makes it skip the injection.
    const linking = productionLinkingConfig();
    const serialised = JSON.stringify(linking.config);
    expect(serialised).toContain("*not-found");
    expect(serialised).not.toContain("expo-router/build/views/Unmatched");

    const { getRoutes } = require("expo-router/build/getRoutes");
    const Constants = require("expo-constants").default;
    type Child = { route: string; contextKey: string; generated?: boolean };
    // Generated routes ALLOWED this time (no `skipGenerated`), which is where
    // expo-router would inject its own `_sitemap` and `+not-found` if the
    // manifest let it. Everything else is what the app passes at launch.
    const routeNode = getRoutes(appRequireContext(), {
      ...Constants.expoConfig?.extra?.router,
      ignoreEntryPoints: true,
      platform: "ios",
    }) as { children: Child[] };

    const notFound = routeNode.children.find((c) => c.route === "+not-found");
    expect(notFound?.contextKey).toBe("./+not-found.tsx");
    expect(notFound?.generated).toBeFalsy();
    expect(routeNode.children.map((c) => c.route)).not.toContain("_sitemap");
  });

  it("would have shipped the sitemap without the manifest knob", () => {
    // The control. Without it, all of the above passes just as well if
    // expo-router stopped generating a sitemap for some unrelated reason, and
    // this file would go on reporting a defect as fixed after the fix stopped
    // being the thing doing the work.
    const { getRoutes } = require("expo-router/build/getRoutes");
    const routeNode = getRoutes(appRequireContext(), {
      ignoreEntryPoints: true,
      platform: "ios",
    }) as { children: { route: string }[] };
    expect(routeNode.children.map((c) => c.route)).toContain("_sitemap");
  });
});
