import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { normalizeMathLineBreaks } from './utils.js'

describe('normalizeMathLineBreaks', () => {
  it('leaves text without $ untouched (same reference)', () => {
    const s = 'plain reasoning, no math here'
    assert.equal(normalizeMathLineBreaks(s), s)
    assert.equal(normalizeMathLineBreaks(''), '')
    assert.equal(normalizeMathLineBreaks(null), null)
  })

  it('leaves single-line inline math untouched', () => {
    const s = 'Santoor $\\rightarrow$ `SANTOOR` (Melody)'
    assert.equal(normalizeMathLineBreaks(s), s)
  })

  it('joins newlines inside multiline inline math', () => {
    assert.equal(
      normalizeMathLineBreaks('value $a +\nb$ end'),
      'value $a + b$ end'
    )
  })

  it('leaves newlines outside math alone', () => {
    assert.equal(
      normalizeMathLineBreaks('line one\n$x$\nline three'),
      'line one\n$x$\nline three'
    )
  })

  it('leaves fenced code blocks untouched', () => {
    const s = 'text $a\nb$ done\n```\n$x\n+\n$y\n```\ntail'
    assert.equal(
      normalizeMathLineBreaks(s),
      'text $a b$ done\n```\n$x\n+\n$y\n```\ntail'
    )
  })

  it('leaves $$ display blocks untouched', () => {
    const s = 'before\n$$\nx +\ny\n$$\nafter $p\nq$ end'
    assert.equal(
      normalizeMathLineBreaks(s),
      'before\n$$\nx +\ny\n$$\nafter $p q$ end'
    )
  })

  it('does not toggle on escaped dollars', () => {
    const s = 'cost \\$5\nand $x$'
    assert.equal(normalizeMathLineBreaks(s), s)
  })

  it('is safe on unclosed math (joins to end, parser leaves raw)', () => {
    assert.equal(
      normalizeMathLineBreaks('open $a\nb\nc'),
      'open $a b c'
    )
  })
})
