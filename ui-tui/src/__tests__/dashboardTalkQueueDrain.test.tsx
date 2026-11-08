import { EventEmitter } from 'node:events'
import { mkdtempSync, realpathSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { PassThrough } from 'node:stream'

import React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@hermes/ink', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('@hermes/ink')
  const stdin = await import('../../packages/hermes-ink/src/ink/hooks/use-stdin.js')

  return { ...actual, useStdin: stdin.default }
})

import { renderSync, Text } from '@hermes/ink'

import { InputEvent } from '../../packages/hermes-ink/src/ink/events/input-event.js'
import useStdin from '../../packages/hermes-ink/src/ink/hooks/use-stdin.js'
import Ink from '../../packages/hermes-ink/src/ink/ink.js'
import type { ParsedKey } from '../../packages/hermes-ink/src/ink/parse-keypress.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { useDashboardTalkPaste } from '../app/useDashboardTalkPaste.js'
import { useSubmission } from '../app/useSubmission.js'
import { useQueue } from '../hooks/useQueue.js'
import { DASHBOARD_TALK_PREFIX } from '../lib/dashboardTalkPaste.js'

vi.mock('../config/env.js', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  DASHBOARD_TUI_MODE: true
}))

const SECRET = 'sk-live-TalkSecret-9f3a'

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

function envelope(text: string): string {
  const body = Buffer.from(JSON.stringify({ text, mode: 'direct' }), 'utf8').toString('base64')

  return `${DASHBOARD_TALK_PREFIX}${body}`
}

describe('offline Talk survives a restart and drains as literal text', () => {
  let home = ''

  afterEach(() => {
    resetUiState()
    vi.unstubAllEnvs()

    if (home) {
      rmSync(home, { recursive: true, force: true })
      home = ''
    }
  })

  async function drainSpoken(spoken: string): Promise<void> {
    home = realpathSync(mkdtempSync(join(tmpdir(), 'talk-drain-')))
    vi.stubEnv('HERMES_HOME', home)
    resetUiState()
    patchUiState({ gatewayConnected: false, sid: 'sess-down' })

    let queue!: ReturnType<typeof useQueue>

    function Capture() {
      const { inputEmitter } = useStdin()

      queue = useQueue()
      useDashboardTalkPaste({
        clearInput: () => {},
        enqueue: queue.enqueue,
        setInput: () => {},
        submitLiteralRef: { current: () => {} },
        sys: () => {}
      })
      ;(Capture as { emitter?: typeof inputEmitter }).emitter = inputEmitter

      return null
    }

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

    ink.render(<Capture />)
    ink.onRender()
    await new Promise(resolve => setTimeout(resolve, 0))
    const emitter = (Capture as { emitter?: { emit: (name: string, event: InputEvent) => void } }).emitter

    emitter?.emit('input', pasted(envelope(spoken)))
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(queue.queueRef.current.map(item => item.text)).toEqual([spoken])
    ink.unmount()

    resetUiState()
    patchUiState({ gatewayConnected: true, sid: 'sess-down' })
    const sys: string[] = []

    const request = vi.fn((method: string) => {
      if (method === 'shell.exec') {
        return Promise.reject(new Error(`command failed: ${SECRET}`))
      }

      return Promise.resolve({ status: 'streaming' })
    })

    let drained!: ReturnType<typeof useQueue>
    let submission!: ReturnType<typeof useSubmission>

    function Drain() {
      drained = useQueue()
      submission = useSubmission({
        appendMessage: () => {},
        composerActions: {
          ...drained,
          prependQueue: drained.prependQ,
          takeQueue: drained.takeQ,
          clearIn: () => {}
        } as never,
        composerRefs: {
          queueRef: drained.queueRef,
          queueEditRef: drained.queueEditRef,
          tokensRef: { current: [] }
        } as never,
        composerState: { input: '', inputBuf: [], completions: [] } as never,
        gw: { request } as never,
        setLastUserMsg: () => {},
        slashRef: { current: () => true },
        submitRef: { current: () => {} },
        sys: text => sys.push(text)
      })

      return <Text>{drained.queuedDisplay.join('|')}</Text>
    }

    const instance = renderSync(<Drain />, {
      stdin: new PassThrough() as never,
      stdout: Object.assign(new PassThrough(), { columns: 80, rows: 20, isTTY: false }) as never,
      stderr: new PassThrough() as never,
      patchConsole: false
    })

    try {
      const item = drained.dequeue()

      expect(item?.text).toBe(spoken)
      submission.sendQueued(item!)
      await new Promise(resolve => setTimeout(resolve, 0))

      expect(request.mock.calls.map(call => call[0])).not.toContain('shell.exec')
      expect(request).toHaveBeenCalledWith(
        'prompt.submit',
        expect.objectContaining({ session_id: 'sess-down', text: spoken })
      )
      expect(JSON.stringify(sys)).not.toContain(SECRET)
    } finally {
      instance.unmount()
    }
  }

  it('does not run a spoken !command, and a shell failure cannot disclose its secret', async () => {
    await drainSpoken(`!printenv ${SECRET}`)
  })

  it('does not interpolate a spoken {!command} after the queue is restored', async () => {
    await drainSpoken(`please run {!printenv ${SECRET}}`)
  })
})
