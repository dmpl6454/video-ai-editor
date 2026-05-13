import { Component, type ReactNode } from 'react'

/**
 * Boundary that contains a render-time throw to its child subtree instead of
 * blanking the whole editor. Used around the WebCodecs scrubber + anywhere
 * else a third-party library could synchronously raise.
 *
 * `fallback` receives the error so callers can decide whether to render
 * nothing, an inline error chip, or simply re-render their non-fancy path.
 */
interface Props {
  children: ReactNode
  fallback?: (err: Error) => ReactNode
  /** Optional callback for telemetry / console logging. */
  onError?: (err: Error, info: { componentStack?: string | null }) => void
  /** Reset key — when it changes the boundary tries to render its children
   * again. Useful when the upstream reason for the error has been resolved
   * (e.g. preview URL changed). */
  resetKey?: unknown
}

interface State {
  err: Error | null
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { err: null }

  static getDerivedStateFromError(err: Error): State {
    return { err }
  }

  componentDidCatch(err: Error, info: { componentStack?: string | null }) {
    this.props.onError?.(err, info)
    // Always log so the bug is debuggable from the devtools console.
    console.warn('[ErrorBoundary] caught:', err, info)
  }

  componentDidUpdate(prev: Props) {
    if (this.state.err && prev.resetKey !== this.props.resetKey) {
      this.setState({ err: null })
    }
  }

  render() {
    if (this.state.err) {
      return this.props.fallback ? this.props.fallback(this.state.err) : null
    }
    return this.props.children
  }
}
