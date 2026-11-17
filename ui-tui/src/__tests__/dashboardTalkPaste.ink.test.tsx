import { EventEmitter } from 'node:events'

import React, { useRef } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@hermes/ink', async () => {
  const stdin = await import('../../packages/hermes-ink/src/ink/hooks/use-stdin.js')

  return { useStdin: stdin.default }
})

import { InputEvent } from '../../packages/hermes-ink/src/ink/events/input-event.js'
import useInput from '../../packages/hermes-ink/src/ink/hooks/use-input.js'
import useStdin from '../../packages/hermes-ink/src/ink/hooks/use-stdin.js'
import Ink from '../../packages/hermes-ink/src/ink/ink.js'
import type { ParsedKey } from '../../packages/hermes-ink/src/ink/parse-keypress.js'
import { patchOverlayState, resetOverlayState } from '../app/overlayStore.js'
import { submitPrompt } from '../app/submissionCore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { useDashboardTalkPaste } from '../app/useDashboardTalkPaste.js'
import { DASHBOARD_TALK_PREFIX } from '../lib/dashboardTalkPaste.js'

vi.mock('../config/env.js', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  DASHBOARD_TUI_MODE: true
}))

function pasted(input: string): InputEvent {
  const keypress: ParsedKey = {
    kind: 'key',
    fn: false,
    name: undefined,
    ctrl: false,
    meta: false,
    shift: false,
    option: false,
    super: false,
    sequence: input,
    raw: input,
    isPasted: true
  }

  return new InputEvent(keypress)
}

function envelope(text: string): string {
  const body = Buffer.from(JSON.stringify({ text, mode: 'direct' }), 'utf8').toString('base64')

  return `${DASHBOARD_TALK_PREFIX}${body}`
}

class FakeStream extends EventEmitter {
  columns = 80
  rows = 24
  isTTY = true
  chunks: string[] = []

  write(chunk: string | Uint8Array): boolean {
    this.chunks.push(typeof chunk === 'string' ? chunk : Buffer.from(chunk).toString('utf8'))

    return true
  }

  setRawMode(): this {
    return this
  }

  setEncoding(): this {
    return this
  }

  ref(): this {
    return this
  }

  unref(): this {
    return this
  }

  read(): null {
    return null
  }
}

function Composer({ seen }: { seen: string[] }) {
  useInput(input => {
    seen.push(input)
  })

  return null
}

function Harness({
  seen,
  setInput,
  submitRef,
  sys,
  enqueue,
  onClear
}: {
  seen: string[]
  setInput: (value: string | ((current: string) => string)) => void
  submitRef: React.MutableRefObject<(value: string) => void>
  sys: (text: string) => void
  enqueue?: (text: string, display?: string) => void
  onClear?: () => void
}) {
  const { inputEmitter } = useStdin()
  const emitterRef = useRef(inputEmitter)

  emitterRef.current = inputEmitter
  useDashboardTalkPaste({
    setInput,
    clearInput: () => {
      onClear?.()
      setInput('')
    },
    submitLiteralRef: submitRef,
    sys,
    enqueue
  })
  ;(Harness as { emitter?: typeof inputEmitter }).emitter = inputEmitter

  return <Composer seen={seen} />
}

describe('dashboard Talk paste on the real Ink emitter', () => {
  it('retains draft and speech when live submission cannot persist', async () => {
    resetUiState()
    patchUiState({ gatewayConnected: true, sid: 'sess-live' })
    const stream = new FakeStream()
    let draft = 'typed draft'
    const sys: string[] = []

    const ink = new Ink({ exitOnCtrlC: false, patchConsole: false,
      stderr: stream as unknown as NodeJS.WriteStream,
      stdin: new FakeStream() as unknown as NodeJS.ReadStream,
      stdout: stream as unknown as NodeJS.WriteStream })

    ink.render(<Harness seen={[]} setInput={value => { draft = typeof value === 'function' ? value(draft) : value }}
      submitRef={{ current: () => { throw new Error('journal failed') } }} sys={text => sys.push(text)} />)
    ink.onRender()
    await new Promise(resolve => setTimeout(resolve, 0))
    const emitter = (Harness as { emitter?: { emit: (name: string, event: InputEvent) => void } }).emitter
    expect(() => emitter?.emit('input', pasted(envelope('spoken text')))).not.toThrow()
    expect(draft).toContain('typed draft')
    expect(draft).toContain('spoken text')
    expect(sys).toContain('input not saved: queue write failed — draft kept')
    ink.unmount()
  })

  it('registers ahead of useInput so the composer never sees the raw prefix', async () => {
    const stdout = new FakeStream()
    const stdin = new FakeStream()
    const stderr = new FakeStream()
    const seen: string[] = []
    const applied: string[] = []
    const submitted: string[] = []
    const sys: string[] = []
    const submitRef = { current: (value: string) => submitted.push(value) }

    resetUiState()
    patchUiState({ gatewayConnected: true, sid: 'sess-live' })

    const ink = new Ink({
      exitOnCtrlC: false,
      patchConsole: false,
      stderr: stderr as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream
    })

    ink.render(
      <Harness
        seen={seen}
        setInput={value => applied.push(typeof value === 'function' ? value('') : value)}
        submitRef={submitRef}
        sys={text => sys.push(text)}
      />
    )
    ink.onRender()
    await new Promise(resolve => setTimeout(resolve, 0))

    const raw = envelope('turn the porch light off\r\n\u001b[31m')
    const emitter = (Harness as { emitter?: { emit: (name: string, event: InputEvent) => void } }).emitter

    expect(emitter).toBeTruthy()
    emitter?.emit('input', pasted(raw))
    await new Promise(resolve => setTimeout(resolve, 0))

    expect(seen).toEqual([])
    expect(applied).toEqual([''])
    expect(submitted).toEqual(['turn the porch light off [31m'])
    expect(sys).toEqual([])
    expect(JSON.stringify({ seen, sys })).not.toContain(DASHBOARD_TALK_PREFIX)
    expect(submitted[0]).not.toContain('\u001b')

    patchOverlayState({ sessions: true })
    emitter?.emit('input', pasted(envelope('must not submit')))
    patchOverlayState({ sessions: false })
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(submitted).toEqual(['turn the porch light off [31m'])
    expect(sys).toEqual(['Talk ignored: close the open menu first.'])

    const typed = `${DASHBOARD_TALK_PREFIX}not-a-paste`

    const typedEvent = new InputEvent({
      kind: 'key',
      fn: false,
      name: undefined,
      ctrl: false,
      meta: false,
      shift: false,
      option: false,
      super: false,
      sequence: typed,
      raw: typed,
      isPasted: false
    })

    emitter?.emit('input', typedEvent)
    await new Promise(resolve => setTimeout(resolve, 0))
    ink.unmount()

    expect(seen).toEqual([typed])
    expect(submitted).toEqual(['turn the porch light off [31m'])
    resetUiState()
    resetOverlayState()
  })
})

/** Status line when the journal write throws. Never built from the exception. */
const TALK_SAVE_DIAGNOSTIC = 'input not saved: queue write failed — draft kept'
const TALK_NO_SESSION_STATUS = 'input not saved: no session yet — draft kept'

describe('direct Talk paste when the session cannot take it', () => {
  afterEach(() => {
    resetUiState()
    resetOverlayState()
  })

  it('keeps a no-session transcript in the composer and does not queue it', async () => {
    resetUiState()
    const seen: string[] = []
    const cleared: string[] = []
    const submitted: string[] = []
    const enqueued: Array<{ text: string; display?: string }> = []
    const sys: string[] = []
    const spoken = 'turn the porch light off [31m'
    let draft = 'typed draft'
    const submitRef = { current: (value: string) => submitted.push(value) }

    const ink = mountTalk({
      seen,
      setInput: value => {
        draft = typeof value === 'function' ? value(draft) : value
      },
      submitRef,
      sys: text => sys.push(text),
      enqueue: (text, display) => enqueued.push({ text, display }),
      onClear: () => cleared.push(draft)
    })

    await emitTalk(ink, 'turn the porch light off\r\n\u001b[31m', 'direct')
    ink.unmount()

    expect(cleared).toEqual([])
    expect(submitted).toEqual([])
    expect(draft).toBe(`typed draft ${spoken}`)
    expect(enqueued).toEqual([])
    expect(sys).toEqual([TALK_NO_SESSION_STATUS])
    expect(sys.join('\n')).not.toContain(spoken)
    expect(seen).toEqual([])
  })

  it('keeps a disconnected transcript queued even when a session id remains', async () => {
    resetUiState()
    patchUiState({ gatewayConnected: false, sid: 'sess-down' })
    const seen: string[] = []
    const cleared: string[] = []
    const submitted: string[] = []
    const enqueued: string[] = []
    let draft = 'typed draft'
    const submitRef = { current: (value: string) => submitted.push(value) }

    const ink = mountTalk({
      seen,
      setInput: value => {
        draft = typeof value === 'function' ? value(draft) : value
      },
      submitRef,
      sys: () => {},
      enqueue: text => enqueued.push(text),
      onClear: () => cleared.push(draft)
    })

    await emitTalk(ink, 'leave the gate open', 'direct')
    ink.unmount()

    expect(cleared).toEqual([])
    expect(submitted).toEqual([])
    expect(draft).toBe('typed draft')
    expect(enqueued).toEqual(['leave the gate open'])
    expect(seen).toEqual([])
  })

  it('keeps the transcript in the composer when the queue write throws, without echoing it', async () => {
    resetUiState()
    patchUiState({ gatewayConnected: false, sid: 'sess-down' })
    const cleared: string[] = []
    const submitted: string[] = []
    const sys: string[] = []
    let draft = 'typed draft'
    const spoken = 'turn the porch light off'
    const submitRef = { current: (value: string) => submitted.push(value) }

    const ink = mountTalk({
      seen: [],
      setInput: value => {
        draft = typeof value === 'function' ? value(draft) : value
      },
      submitRef,
      sys: text => sys.push(text),
      enqueue: () => {
        throw new Error('disk full')
      },
      onClear: () => cleared.push(draft)
    })

    await emitTalk(ink, spoken, 'direct')
    ink.unmount()

    expect(cleared).toEqual([])
    expect(submitted).toEqual([])
    expect(draft).toBe('typed draft turn the porch light off')
    expect(sys).toEqual([TALK_SAVE_DIAGNOSTIC])
    expect(sys.join('\n')).not.toContain('disk full')
    expect(sys.join('\n')).not.toContain(spoken)
  })

  it('does not disclose a secret that the queue-write error echoes', async () => {
    resetUiState()
    patchUiState({ gatewayConnected: false, sid: 'sess-down' })
    const secret = 'sk-live-TalkSecret-9f3a'
    const spoken = `the gate code is ${secret}`
    const sys: string[] = []
    let draft = 'typed draft'

    const ink = mountTalk({
      seen: [],
      setInput: value => {
        draft = typeof value === 'function' ? value(draft) : value
      },
      submitRef: { current: () => {} },
      sys: text => sys.push(text),
      enqueue: () => {
        throw new Error(`ENOSPC writing ${spoken}`)
      }
    })

    await emitTalk(ink, spoken, 'direct')
    ink.unmount()

    expect(draft).toBe(`typed draft ${spoken}`)
    expect(sys).toEqual([TALK_SAVE_DIAGNOSTIC])
    expect(sys.join('\n')).not.toContain('ENOSPC')
    expect(sys.join('\n')).not.toContain(secret)
    expect(sys.join('\n')).not.toContain(spoken)
  })

  it('never puts arbitrary queue-write error text on the status line', async () => {
    resetUiState()
    patchUiState({ gatewayConnected: false, sid: 'sess-down' })
    const leaked = 'EACCES /home/u/.hermes/journal.db pin=4821 probe-token-9f8412'
    const spoken = 'the pin is 4821'
    const sys: string[] = []
    let draft = 'typed draft'

    const ink = mountTalk({
      seen: [],
      setInput: value => {
        draft = typeof value === 'function' ? value(draft) : value
      },
      submitRef: { current: () => {} },
      sys: text => sys.push(text),
      enqueue: () => {
        throw leaked
      }
    })

    await emitTalk(ink, spoken, 'direct')
    ink.unmount()

    expect(draft).toBe(`typed draft ${spoken}`)
    expect(sys).toEqual([TALK_SAVE_DIAGNOSTIC])
    expect(sys.join('\n')).not.toContain('EACCES')
    expect(sys.join('\n')).not.toContain('4821')
    expect(sys.join('\n')).not.toContain('probe-token-9f8412')
    expect(sys.join('\n')).not.toContain(leaked)
    expect(sys.join('\n')).not.toContain(spoken)
  })

  it('keeps the transcript in the composer with no error when nothing can queue it', async () => {
    resetUiState()
    const cleared: string[] = []
    const submitted: string[] = []
    const sys: string[] = []
    let draft = ''
    const submitRef = { current: (value: string) => submitted.push(value) }

    const ink = mountTalk({
      seen: [],
      setInput: value => {
        draft = typeof value === 'function' ? value(draft) : value
      },
      submitRef,
      sys: text => sys.push(text),
      onClear: () => cleared.push('cleared')
    })

    await emitTalk(ink, 'hold this', 'direct')
    ink.unmount()

    expect(cleared).toEqual([])
    expect(submitted).toEqual([])
    expect(draft).toBe('hold this')
    expect(sys).toEqual([])
  })

  it('does not tag the next identical typed submit as a voice turn', async () => {
    resetUiState()
    const spoken = 'leave the light on'

    const ink = mountTalk({
      seen: [],
      setInput: () => {},
      submitRef: { current: () => {} },
      sys: () => {},
      enqueue: () => {}
    })

    await emitTalk(ink, spoken, 'direct')
    ink.unmount()
    patchUiState({ gatewayConnected: true, sid: 'sess-later' })
    const request = vi.fn(async () => ({ status: 'streaming' }))

    submitPrompt(spoken, {
      appendMessage: () => {},
      enqueue: () => {},
      expand: value => value,
      gw: { isCanonical: true, request } as never,
      setLastUserMsg: () => {},
      sys: () => {}
    })
    await vi.waitFor(() => expect(request).toHaveBeenCalled())
    expect(request.mock.calls[0]?.[1]).not.toHaveProperty('voice_turn')
  })

  it('appends a draft paste while disconnected and does not queue or submit it', async () => {
    resetUiState()
    patchUiState({ gatewayConnected: false, sid: 'sess-down' })
    const submitted: string[] = []
    const enqueued: string[] = []
    let draft = 'typed draft'

    const ink = mountTalk({
      seen: [],
      setInput: value => {
        draft = typeof value === 'function' ? value(draft) : value
      },
      submitRef: { current: value => submitted.push(value) },
      sys: () => {},
      enqueue: text => enqueued.push(text)
    })

    await emitTalk(ink, 'hold this', 'draft')
    ink.unmount()

    expect(draft).toBe('typed draft hold this')
    expect(enqueued).toEqual([])
    expect(submitted).toEqual([])
  })
})

function mountTalk(props: React.ComponentProps<typeof Harness>): Ink {
  const stdout = new FakeStream()
  const stdin = new FakeStream()
  const stderr = new FakeStream()

  const ink = new Ink({
    exitOnCtrlC: false,
    patchConsole: false,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    stdout: stdout as unknown as NodeJS.WriteStream
  })

  ink.render(<Harness {...props} />)
  ink.onRender()

  return ink
}

async function emitTalk(ink: Ink, text: string, mode: 'direct' | 'draft'): Promise<void> {
  await new Promise(resolve => setTimeout(resolve, 0))
  const emitter = (Harness as { emitter?: { emit: (name: string, event: InputEvent) => void } }).emitter
  const body = Buffer.from(JSON.stringify({ text, mode }), 'utf8').toString('base64')

  emitter?.emit('input', pasted(`${DASHBOARD_TALK_PREFIX}${body}`))
  await new Promise(resolve => setTimeout(resolve, 0))
}
