// The engine connection as a React value (lib/connection keeps it as a
// module-level state with a listener API).
import { useSyncExternalStore } from 'react'
import { engineState, onEngineState, type EngineState } from './connection'

export function useEngineState(): EngineState {
  return useSyncExternalStore(onEngineState, engineState, engineState)
}
