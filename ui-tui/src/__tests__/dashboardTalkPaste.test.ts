import { describe, expect, it } from 'vitest'

import { buildTalkPaste } from '../../../web/src/lib/chat-talk.ts'
import {
  DASHBOARD_TALK_PREFIX,
  decideDashboardTalk,
  MAX_TALK_TEXT_CHARS,
  parseDashboardTalkPaste,
  sanitizeTalkText
} from '../lib/dashboardTalkPaste.js'

function body(text: string, mode: 'direct' | 'draft'): string {
  return Buffer.from(JSON.stringify({ text, mode }), 'utf8').toString('base64')
}

function paste(text: string, mode: 'direct' | 'draft'): string {
  return `\u001b[200~${DASHBOARD_TALK_PREFIX}${body(text, mode)}\u001b[201~`
}

describe('parseDashboardTalkPaste', () => {
  it('reads a bracketed Talk paste and ignores ordinary input', () => {
    expect(parseDashboardTalkPaste(paste('turn the porch light off', 'direct'))).toEqual({
      kind: 'talk',
      payload: { text: 'turn the porch light off', mode: 'direct' }
    })
    expect(parseDashboardTalkPaste('hello')).toBeNull()
  })

  it('leaves a typed prefix alone so the composer can show it', () => {
    expect(parseDashboardTalkPaste(`${DASHBOARD_TALK_PREFIX}${body('hello', 'direct')}`)).toBeNull()
    expect(parseDashboardTalkPaste(`${DASHBOARD_TALK_PREFIX}%%%`)).toBeNull()
  })

  it('accepts the prefix when Ink already marked the input as a paste', () => {
    const raw = `${DASHBOARD_TALK_PREFIX}${body('porch light', 'draft')}`

    expect(parseDashboardTalkPaste(raw, { pasted: true })).toEqual({
      kind: 'talk',
      payload: { text: 'porch light', mode: 'draft' }
    })
  })

  it('drops a real paste whose payload is not Talk instead of typing the prefix', () => {
    expect(parseDashboardTalkPaste(`\u001b[200~${DASHBOARD_TALK_PREFIX}%%%\u001b[201~`)).toEqual({
      kind: 'invalid'
    })
    expect(parseDashboardTalkPaste(`${DASHBOARD_TALK_PREFIX}%%%`, { pasted: true })).toEqual({
      kind: 'invalid'
    })
  })

  it('re-sanitizes controls and rejects an oversize transcript', () => {
    expect(sanitizeTalkText('hello\r\nworld\u001b[31m')).toBe('hello world [31m')
    expect(sanitizeTalkText('line\u2028sep\u2029end')).toBe('line sep end')
    expect(sanitizeTalkText('x\u007fy\u0080z\u009fw\u00a0v\u007eu')).toBe('x y z w\u00a0v~u')
    expect(parseDashboardTalkPaste(paste('x\u007fy\u0080z\u009fw', 'direct'))).toEqual({
      kind: 'talk',
      payload: { text: 'x y z w', mode: 'direct' }
    })
    expect(parseDashboardTalkPaste(paste('keep\u00a0me', 'draft'))).toEqual({
      kind: 'talk',
      payload: { text: 'keep\u00a0me', mode: 'draft' }
    })
    expect(parseDashboardTalkPaste(paste('hello\r\nworld\u001b[31m', 'direct'))).toEqual({
      kind: 'talk',
      payload: { text: 'hello world [31m', mode: 'direct' }
    })
    expect(parseDashboardTalkPaste(paste('x'.repeat(MAX_TALK_TEXT_CHARS + 1), 'direct'))).toEqual({
      kind: 'invalid'
    })
  })
})

describe('decideDashboardTalk', () => {
  const parsed = parseDashboardTalkPaste(paste('hello', 'direct'))

  it('applies only in the dashboard when no overlay is open', () => {
    expect(decideDashboardTalk({ dashboard: true, blocked: false, parsed })).toBe('apply')
    expect(decideDashboardTalk({ dashboard: false, blocked: false, parsed })).toBe('ignore')
    expect(decideDashboardTalk({ dashboard: true, blocked: true, parsed })).toBe('refuse')
  })

  it('accepts the paste the web client builds', () => {
    const paste = buildTalkPaste('turn the porch light off', 'direct')

    expect(parseDashboardTalkPaste(paste)).toEqual({
      kind: 'talk',
      payload: { text: 'turn the porch light off', mode: 'direct' }
    })
  })
})
