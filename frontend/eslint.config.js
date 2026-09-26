import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
    },
    rules: {
      // ONE icon approach (QA-125, wave C review): lucide-react is reached only
      // through the name map in lib/icons.ts, rendered by components/Icon —
      // a direct import skips the app's stroke width, size and data-icon.
      'no-restricted-imports': ['error', { paths: [{ name: 'lucide-react',
        message: 'Use <Icon name=…/> (components/Icon) and add the glyph to lib/icons.ts.' }] }],
    },
  },
  {
    files: ['src/lib/icons.ts', 'src/lib/icons.test.ts'],
    rules: { 'no-restricted-imports': 'off' },
  },
])
