// QA-074: "Captions from transcript" offers the explicit rebuild as a labelled
// checkbox, off by default — a style change keeps hand-edited cues.
import { describe, expect, it } from 'vitest'
import { AI_CATALOG } from './aiCatalog'
import { fieldsFor } from './schemaForm'

const schema = {
  name: 'add_caption_track', description: '', input_schema: {
    type: 'object', properties: {
      style: { type: 'string', enum: ['default', 'ig_chunky', 'word_emphasis'], default: 'default' },
      position: { type: 'string', enum: ['bottom', 'center', 'top'], default: 'bottom' },
      rebuild: { type: 'boolean', default: false },
    },
  },
}

describe('captions restyle form', () => {
  it('shows rebuild as an unticked checkbox that names what it discards', () => {
    const entry = AI_CATALOG.find((e) => e.tool === 'add_caption_track')!
    const f = fieldsFor(schema as never, entry).find((x) => x.name === 'rebuild')!
    expect(f.widget).toBe('checkbox')
    expect(f.default).toBe(false)
    expect(f.label).toMatch(/discards hand edits/)
  })
})
