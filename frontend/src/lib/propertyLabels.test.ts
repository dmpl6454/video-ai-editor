import { describe, expect, it } from 'vitest'
import fixture from './__fixtures__/property_labels.json'
import { opLabel, propertyLabel } from './opLabels'

// QA-101-SWEEP: History printed set_property as "Edit — Set.style.size = 120".
// The same cases run against agent/dispatch.property_label (tests/test_property_labels.py).
describe('set_property in editor language', () => {
  it.each(fixture.cases)('$path = $value', ({ path, value, group, phrase }) => {
    expect(propertyLabel(path, value)).toEqual({ group, phrase })
  })

  it('titles the History row with the property group, from the op args', () => {
    for (const { path, value, group, phrase } of fixture.cases) {
      const l = opLabel({ tool: 'set_property', summary: `${group}: ${phrase}`, args: { clip_id: 't_1a2b3c4d', path, value } })
      expect(l.title).toBe(group)
      expect(l.detail).toBe(phrase)
    }
  })

  it.each(fixture.legacy)('reads an op saved before the change: $summary', ({ summary, group, phrase }) => {
    const l = opLabel({ tool: 'set_property', summary })
    expect(l).toMatchObject({ title: group, detail: phrase })
    // No property path, no "=" and no Python repr left in what a user reads.
    expect(`${l.title} ${l.detail}`).not.toMatch(/=|\b(?:style|audio|transform)\.|\bTrue\b|\bNone\b|'/)
  })
})
