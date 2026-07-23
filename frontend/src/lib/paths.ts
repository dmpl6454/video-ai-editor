// Path display helpers shared across panels.
//
// baseName: leaf filename of a path on EITHER separator. A naive
// `p.split('/').pop()` returns the whole `D:\...` path on Windows (and for
// .vae projects moved across OSes the stored separator can differ from the
// host's), so split on both. The `|| p` keeps trailing-separator inputs from
// degrading to '' — they fall back to the raw input instead (mirrors
// EffectsPanel's proven helper).
export const baseName = (p: string): string => {
  const parts = p.split(/[\\/]/)
  return parts[parts.length - 1] || p
}
