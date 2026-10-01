// Editor Brain (EB1-F, FX-E wave, UX-07): the footage is being read.
//
// The Prompt bar used to say only "planning · a moment estimated" for the
// whole read (the reducer dropped the `analysis` frames). This is the read's
// own line under the input: which layer, how far, about how long — and, when
// the backend names them, every layer with its fraction. A progressbar with a
// text value, not a live region: a screen reader hears "Reading the footage"
// once (the bar announces it), never a percentage every 200 ms.

import { analysisLine, layerWords, type AnalysisProgress } from '../../lib/promptEvents'
import './brain.css'

export function AnalysisProgressLine({ analysis }: { analysis: AnalysisProgress }) {
  const line = analysisLine(analysis)
  return (
    <div className="prompt-analysis" role="progressbar" aria-label="Reading the footage" aria-valuemin={0}
         aria-valuemax={100} aria-valuenow={Math.round(analysis.pct)} aria-valuetext={line}
         data-testid="analysis-progress">
      <span className="prompt-analysis-line">{line}</span>
      {analysis.layers.length > 0 && (
        <ul className="prompt-analysis-layers" aria-hidden="true">
          {analysis.layers.map((l) => (
            <li key={l.name} className={`is-${l.state}`}>
              {layerWords(l.name)}{l.state === 'done' ? ' ✓' : l.pct !== null && l.state === 'running' ? ` ${Math.round(l.pct)}%` : ''}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
