import { EventEmitter } from 'node:events'

import React, { useRef } from 'react'
import { describe, expect, it, vi } from 'vitest'

vi.mock('@hermes/ink', async () => {
  const stdin = await import('../../packages/hermes-ink/src/ink/hooks/use-stdin.js')

  return { useStdin: stdin.default }
})

import { InputEvent } from '../../packages/hermes-ink/src/ink/events/input-event.js'
import useInput from '../../packages/hermes-ink/src/ink/hooks/use-input.js'
import useStdin from '../../packages/hermes-ink/src/ink/hooks/use-stdin.js'
import Ink from '../../packages/hermes-ink/src/ink/ink.js'
import type { ParsedKey } from '../../packages/hermes-ink/src/ink/parse-keypress.js'
import { DASHBOARD_TALK_PREFIX } from '../lib/dashboardTalkPaste.js'

import { useDashboardTalkPaste } from './useDashboardTalkPaste.js'

process.env.HERMES_TUI_DASHBOARD = '1'

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
  sys
}: {
  seen: string[]
  setInput: (value: string | ((current: string) => string)) => void
  submitRef: React.MutableRefObject<(value: string) => void>
  sys: (text: string) => void
}) {
  const { inputEmitter } = useStdin()
  const emitterRef = useRef(inputEmitter)

  emitterRef.current = inputEmitter
  useDashboardTalkPaste({ setInput, submitRef, sys })
  ;(Harness as { emitter?: typeof inputEmitter }).emitter = inputEmitter

  return <Composer seen={seen} />
}

describe('dashboard Talk paste on the real Ink emitter', () => {
  it('registers ahead of useInput so the composer never sees the raw prefix', async () => {
    const stdout = new FakeStream()
    const stdin = new FakeStream()
    const stderr = new FakeStream()
    const seen: string[] = []
    const applied: string[] = []
    const submitted: string[] = []
    const sys: string[] = []
    const submitRef = { current: (value: string) => submitted.push(value) }

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
  })
})
