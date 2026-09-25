// The AI tab's one-line feature status in editor language (QA-101 remainder).
// It used to print the backend's report line verbatim — "13/15 optional
// features available; missing: interpolate, gpu_transcribe" — feature KEYS a
// user never sees anywhere else. The line now says what it means; the human
// names of what is missing sit behind "Details".

import type { FeatureReport } from '../api'

export interface FeatureStatus { line: string; missing: string[] }

export function featureStatus(report: FeatureReport | null | undefined): FeatureStatus | null {
  if (!report) return null
  const missing = (report.unavailable ?? []).map((e) => e.feature || e.key)
  if (!missing.length) return { line: 'All AI features are ready', missing }
  return {
    line: missing.length === 1 ? 'One feature needs a download' : 'Some features need a download',
    missing,
  }
}
