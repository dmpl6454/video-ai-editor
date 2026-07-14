import { defineConfig } from 'vitest/config'

// Pure-logic unit tests only (no DOM/canvas). Kept deliberately minimal —
// the frontend otherwise has no test runner; CI's real gate stays tsc + build.
export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
})
