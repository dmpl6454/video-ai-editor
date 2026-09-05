/**
 * Cold-start smoke: require every screen and library module the way a launch
 * does. A module-scope exception is what kills a release build before the
 * ErrorBoundary can mount — the failure mode that produced "opens and closes
 * immediately". jest-expo mocks most native modules; the three below are
 * SDK-57 classes its preset does not provide (their real native objects are
 * only present in a device build), so they are stubbed just enough to import.
 */
import * as fs from "fs";
import * as path from "path";

jest.mock("expo-video", () => {
  const React = require("react");
  const { View } = require("react-native");
  return {
    VideoView: (props: Record<string, unknown>) => React.createElement(View, props),
    useVideoPlayer: () => ({ play() {}, pause() {}, replace() {}, currentTime: 0, duration: 0, playing: false,
      addListener() { return { remove() {} }; } }),
  };
});
jest.mock("expo-file-system", () => ({
  File: class { uri = "file:///stub"; constructor(..._a: unknown[]) {} },
  Directory: class { uri = "file:///stub/"; constructor(..._a: unknown[]) {} },
  Paths: { cache: { uri: "file:///cache/" }, document: { uri: "file:///doc/" }, availableDiskSpace: 1e12 },
}));
jest.mock("expo-media-library", () => ({
  requestPermissionsAsync: async () => ({ granted: true, accessPrivileges: "all" }),
  saveToLibraryAsync: async () => undefined,
  createAssetAsync: async () => ({ id: "stub" }),
}));

const root = path.resolve(__dirname, "..", "..");
const files: string[] = [];
const walk = (d: string) => {
  for (const f of fs.readdirSync(d)) {
    const p = path.join(d, f);
    if (fs.statSync(p).isDirectory()) walk(p);
    else if (/\.(ts|tsx)$/.test(f) && !/\.test\./.test(f) && !/\.d\.ts$/.test(f)) files.push(p);
  }
};
for (const dir of ["lib", "constants", "components", "app"]) if (fs.existsSync(path.join(root, dir))) walk(path.join(root, dir));

describe("every module survives a cold-start require", () => {
  test("found the app's modules", () => expect(files.length).toBeGreaterThan(20));
  for (const f of files) {
    test(path.relative(root, f), () => {
      jest.isolateModules(() => { require(f); });
    });
  }
});
