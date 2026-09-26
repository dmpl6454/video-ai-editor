// What an AI tool card SAYS when its feature is missing (QA-101 sweep; the
// feature half of lib/brainCopy). The backend's feature report is written for
// developers — "`uv sync --all-extras --group dev` (installs rembg)" — and the
// card printed it in a <pre> with a "Copy fix" button, inside a packaged app
// where no terminal command can be acted on. This maps a gate to editor
// language; the raw fix stays available only where it can be used (a source
// checkout), behind a disclosure.

export interface FeatureGate { feature: string; fix: string; packagedExcluded: boolean; packagedApp: boolean }

export interface FeatureCopy {
  /** The card's badge. */
  badge: string
  /** The sentence under the description. */
  line: string
  /** Why Run is off (the form's hint). */
  reason: string
  /** The setup command, only when it can be run here (not the packaged app). */
  fix: string | null
}

export function featureCopy(g: FeatureGate): FeatureCopy {
  if (g.packagedExcluded) {
    return { badge: 'Not included', line: `${g.feature} is not included in this version of the app.`,
             reason: 'Not included in this version of the app', fix: null }
  }
  if (g.packagedApp) {
    return { badge: 'Not set up', line: `${g.feature} isn’t set up on this Mac.`,
             reason: `${g.feature} isn’t set up on this Mac`, fix: null }
  }
  const fix = g.fix.replace(/`/g, '').trim()
  return { badge: 'Not installed', line: `${g.feature} isn’t installed.`,
           reason: `${g.feature} isn’t installed`, fix: fix || null }
}
