import { describe, expect, it } from 'vitest'
import { applies, selected } from './when.js'

// The power binding of a Home Assistant device (plugins/_homeassistant/bindings.py).
const specs = {
  source: { kind: 'str', selector: true, choices: ['single', 'split'], default: 'single' },
  entity: { kind: 'str', required: true, when: { source: 'single' } },
  invert_sign: { kind: 'bool', default: false, when: { source: 'single' } },
  import_entity: { kind: 'str', required: true, when: { source: 'split' } },
  export_entity: { kind: 'str', required: true, when: { source: 'split' } },
  scale: { kind: 'float', default: 1 },
}

describe('selector', () => {
  it('follows the fields that are set', () => {
    expect(selected('source', {}, specs)).toBe('single')
    expect(selected('source', { entity: 'sensor.p' }, specs)).toBe('single')
    expect(selected('source', { export_entity: 'sensor.e' }, specs)).toBe('split')
    expect(selected('source', { invert_sign: false }, specs, 'split')).toBe('single')
  })
  it('takes the pick while none of its fields is set', () => {
    expect(selected('source', { scale: 2 }, specs, 'split')).toBe('split')
  })
  it('decides which fields apply', () => {
    const split = { import_entity: 'sensor.i' }
    expect(applies(specs.entity, split, specs)).toBe(false)
    expect(applies(specs.export_entity, split, specs)).toBe(true)
    expect(applies(specs.entity, {}, specs)).toBe(true)
    expect(applies(specs.import_entity, { source: 'split' }, specs)).toBe(true)
    expect(applies(specs.scale, split, specs)).toBe(true)
  })
})
