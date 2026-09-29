// "Ask before applying Prompt bar edits" (0.8.0, Settings › Prompt bar).
// Backend: prompt_setting.py, GET/PUT /api/settings/prompt. ON by default:
// the key-free Prompt bar shows a preview card and changes nothing until
// Apply. OFF: the plan runs at once (the safety net still checks every run).
// The chat assistant with an Anthropic key is never previewed.

import type { PromptSettingsWire } from '../api'

export const PROMPT_APPLY_LABEL = 'Ask before applying Prompt bar edits'

export const PROMPT_APPLY_HELP =
  'Without a Claude key, the Prompt bar first shows what it would change — clip by clip, with times — ' +
  'and changes nothing until you press Apply. Turn this off to apply edits straight away; ' +
  'every edit is still checked, and one ⌘Z undoes it.'

/** The line under the switch: where the value comes from and what it means now. */
export function promptApplyNote(s: PromptSettingsWire): string {
  if (s.source === 'env') {
    return `Set by VAI_PROMPT_CONFIRM for this run (${s.confirm_before_apply ? 'on' : 'off'}); change it where it was set.`
  }
  return s.confirm_before_apply
    ? 'On — Prompt bar edits wait for Apply.'
    : 'Off — Prompt bar edits apply as soon as they are planned.'
}

/** A wire answer → the setting, or null when it is not one. */
export function normalizePromptSettings(raw: unknown): PromptSettingsWire | null {
  if (!raw || typeof raw !== 'object') return null
  const r = raw as Record<string, unknown>
  if (typeof r.confirm_before_apply !== 'boolean') return null
  return {
    confirm_before_apply: r.confirm_before_apply,
    source: typeof r.source === 'string' ? r.source : 'default',
    default: typeof r.default === 'boolean' ? r.default : true,
  }
}
