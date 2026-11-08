import { describe, expect, it } from 'vitest'

import { DASHBOARD_TALK_PREFIX, handleDashboardTalkInput, TALK_REFUSED } from '../lib/dashboardTalkPaste.js'

/**
 * Same stop contract as Ink's EventEmitter: a listener that calls
 * stopImmediatePropagation hides the event from every listener after it.
 * The Talk hook registers first (useLayoutEffect), ahead of overlays and
 * the composer.
 */
function fanout(listeners: Array<(event: TalkEvent) => void>, event: TalkEvent): void {
  for (const listener of listeners) {
    listener(event)

    if (event.stopped) {
      return
    }
  }
}

interface TalkEvent {
  input: string
  keypress: { isPasted?: boolean }
  stopped: boolean
  stopImmediatePropagation: () => void
}

function event(input: string, pasted: boolean): TalkEvent {
  const row: TalkEvent = {
    input,
    keypress: { isPasted: pasted },
    stopped: false,
    stopImmediatePropagation() {
      this.stopped = true
    }
  }

  return row
}

function envelope(text: string, mode: 'direct' | 'draft'): string {
  const body = Buffer.from(JSON.stringify({ text, mode }), 'utf8').toString('base64')

  return `${DASHBOARD_TALK_PREFIX}${body}`
}

describe('dashboard Talk paste does not reach the composer or overlays', () => {
  it('stops a real paste before later listeners and applies only the sanitized transcript', () => {
    const raw = envelope('turn the porch light off\r\n\u001b[31m', 'direct')
    const seen: string[] = []
    const applied: string[] = []
    const sys: string[] = []
    const overlay: string[] = []
    const composer: string[] = []

    const talk = (row: TalkEvent) => {
      seen.push(row.input)
      handleDashboardTalkInput(row, {
        dashboard: true,
        blocked: false,
        sys: text => sys.push(text),
        apply: text => applied.push(text)
      })
    }

    const overlayListener = (row: TalkEvent) => {
      overlay.push(row.input)
    }

    const composerListener = (row: TalkEvent) => {
      composer.push(row.input)
    }

    fanout([talk, overlayListener, composerListener], event(raw, true))

    expect(applied).toEqual(['turn the porch light off [31m'])
    expect(applied[0]).not.toContain('\u001b')
    expect(overlay).toEqual([])
    expect(composer).toEqual([])
    expect(sys).toEqual([])
    expect(JSON.stringify({ applied, overlay, composer, sys })).not.toContain(DASHBOARD_TALK_PREFIX)
    expect(JSON.stringify({ applied, overlay, composer, sys })).not.toContain(raw)
  })

  it('refuses an open overlay without handing the prefix or the transcript to anyone else', () => {
    const raw = envelope('secret porch code', 'direct')
    const applied: string[] = []
    const sys: string[] = []
    const overlay: string[] = []

    fanout(
      [
        row => {
          handleDashboardTalkInput(row, {
            dashboard: true,
            blocked: true,
            sys: text => sys.push(text),
            apply: text => applied.push(text)
          })
        },
        row => {
          overlay.push(row.input)
        }
      ],
      event(raw, true)
    )

    expect(applied).toEqual([])
    expect(overlay).toEqual([])
    expect(sys).toEqual([TALK_REFUSED])
    expect(sys[0]).not.toContain(DASHBOARD_TALK_PREFIX)
    expect(sys[0]).not.toContain('secret porch code')
  })

  it('does not consume a typed prefix, so the composer still sees it', () => {
    const raw = envelope('hello', 'direct')
    const composer: string[] = []

    fanout(
      [
        row => {
          handleDashboardTalkInput(row, {
            dashboard: true,
            blocked: false,
            sys: () => undefined,
            apply: () => undefined
          })
        },
        row => {
          composer.push(row.input)
        }
      ],
      event(raw, false)
    )

    expect(composer).toEqual([raw])
  })
})
