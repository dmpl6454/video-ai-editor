// A tab's sub-segmented choice, remembered per tab for the session (so
// Video › Remove BG stays put when you re-select a clip).
import { useCallback, useState } from 'react'

const memory = new Map<string, string>()

export function useSubTab<T extends string>(tab: string, initial: T): [T, (v: T) => void] {
  const [v, setV] = useState<T>(() => (memory.get(tab) as T | undefined) ?? initial)
  const set = useCallback((n: T) => { memory.set(tab, n); setV(n) }, [tab])
  return [v, set]
}
