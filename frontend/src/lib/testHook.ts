/**
 * Test hook: lets the Playwright suites drive the app's own store in the BUILT
 * bundle.
 *
 * WHY. Several UI suites select clips, move the playhead or set the zoom
 * through `useStore`. They reached it with `import('/src/store.ts')`, which
 * exists only on a Vite dev server, so in the gate's full pytest run (which
 * serves frontend/dist) 14 tests failed with "Failed to fetch dynamically
 * imported module" (wave D3 gate). With the page opened as `/?vae-test`, the
 * store is published as `window.__vaeTest.useStore`, and the tests use
 * `await (window.__vaeTest ?? import('/src/store.ts'))`, which works against
 * both. An ordinary launch never sets the flag, and the store adds no
 * capability a local page does not already have through the API.
 */

/** The query parameter that turns the hook on. */
export const TEST_HOOK_PARAM = 'vae-test'

/** Publish `store` on `target` when `search` carries the flag. */
export function installTestHook(search: string, store: unknown, target: object): boolean {
  if (!new URLSearchParams(search).has(TEST_HOOK_PARAM)) return false
  ;(target as { __vaeTest?: { useStore: unknown } }).__vaeTest = { useStore: store }
  return true
}
