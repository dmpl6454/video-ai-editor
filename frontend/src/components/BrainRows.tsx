// "Who answers your prompts" — ONE rendering of the brain list, shared by the
// Prompt bar's brain popover and Settings (COHERENCE, wave C review). They
// used to be two visual languages for one fact: the popover's "BRAINS — WHICH
// ONE ANSWERS, AND WHY" with a green ring / struck plug and a "Recheck"
// button, Settings' "Who answers your prompts" with filled/hollow dots and
// "Check again", and the popover's add-key hint in a code-style box. Now one
// heading, one dot (--good filled = can answer, hollow = can't), one verb,
// and the hint as plain help text. Each surface adds only what is its own
// (the popover's download row, "answered" for the last run).

import type { ReactNode } from 'react'
import { brainCopy, brainStatus } from '../lib/brainCopy'
import { shortModel, type BrainRow } from '../lib/promptEvents'
import './brainRows.css'

export const BRAINS_HEADING = 'Who answers your prompts'
export const BRAINS_HELP = 'The first one that can answer does, in this order.'
export const CHECK_AGAIN = 'Check again'

interface Props {
  rows: readonly BrainRow[]
  /** The brain that answered the last run, if any. */
  answered?: string | null
  /** Replace the next step a row suggests (Settings: the key field is right there). */
  fixFor?: (row: BrainRow, fix: string | null) => string | null
  /** Surface-specific controls under a row (the popover's Download). */
  extra?: (row: BrainRow) => ReactNode
}

export function BrainRows({ rows, answered = null, fixFor, extra }: Props) {
  return (
    <ul className="brain-list">
      {rows.map((row) => {
        const copy = brainCopy(row)
        const fix = fixFor ? fixFor(row, copy.fix) : copy.fix
        const more = extra?.(row)
        return (
          <li className="brain-list-row" key={row.id} data-brain={row.id}>
            <span className="brain-list-name">
              <span className="brain-list-dot" data-on={row.available ? 'true' : 'false'} aria-hidden="true" />
              {row.label}{row.id === 'local_model' && row.model ? ` · ${shortModel(row.model)}` : ''}
            </span>
            <span className="brain-list-side" data-answered={answered === row.id ? 'true' : undefined}>
              {brainStatus(row, answered)}
            </span>
            {/* Editor language; the raw report stays the hover title for bug reports. */}
            <span className="brain-list-meta" title={[row.detail, row.fix].filter(Boolean).join(' · ')}>
              {copy.detail}{fix ? ` ${fix}` : ''}
            </span>
            {more ? <div className="brain-list-extra">{more}</div> : null}
          </li>
        )
      })}
    </ul>
  )
}
