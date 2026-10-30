import { describe, expect, it } from 'vitest'

import { DASHBOARD_TALK_PREFIX, sanitizeTalkText } from './dashboard-talk'

describe('dashboard talk contract', () => {
  it('uses the one prefix both sides paste and parse', () => {
    expect(DASHBOARD_TALK_PREFIX).toBe('HERMES_DASH_TALK:')
  })

  it('strips controls so a transcript cannot submit early or inject escapes', () => {
    expect(sanitizeTalkText('hello\r\nworld\u001b[31m')).toBe('hello world [31m')
    expect(sanitizeTalkText('  \n\t  ')).toBe('')
    expect(sanitizeTalkText('ok\u0000there')).toBe('ok there')
    expect(sanitizeTalkText('line\u2028sep\u2029end')).toBe('line sep end')
  })
})
