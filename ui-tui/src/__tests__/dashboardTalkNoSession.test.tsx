/**
 * No-session Talk must not look queued. savePendingInput writes nothing
 * without a sid, so a memory enqueue is lost on remount and would attach to
 * whichever session appears next.
 */
import { EventEmitter } from 'node:events'
import { existsSync, mkdtempSync, readdirSync, realpathSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@hermes/ink', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('@hermes/ink')
  const stdin = await import('../../packages/hermes-ink/src/ink/hooks/use-stdin.js')

  return { ...actual, useStdin: stdin.default }
})

import { InputEvent } from '../../packages/hermes-ink/src/ink/events/input-event.js'
import useStdin from '../../packages/hermes-ink/src/ink/hooks/use-stdin.js'
import Ink from '../../packages/hermes-ink/src/ink/ink.js'
import type { ParsedKey } from '../../packages/hermes-ink/src/ink/parse-keypress.js'
import { mustStayLiteral } from '../app/submissionTalkDraft.js'
import { isTalkDraft } from '../app/talkDraftProvenance.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { useDashboardTalkPaste } from '../app/useDashboardTalkPaste.js'
import { useQueue } from '../hooks/useQueue.js'
import { DASHBOARD_TALK_PREFIX } from '../lib/dashboardTalkPaste.js'

vi.mock('../config/env.js', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  DASHBOARD_TUI_MODE: true
}))

const NO_SESSION_STATUS = 'input not saved: no session yet — draft kept'

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

  write(): boolean {
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

function journalFiles(home: string): string[] {
  const dir = join(home, 'tui-pending-inputs')

  return existsSync(dir) ? readdirSync(dir).filter(name => name.endsWith('.json')) : []
}

function envelope(text: string, mode: 'direct' | 'draft' = 'direct'): string {
  const body = Buffer.from(JSON.stringify({ text, mode }), 'utf8').toString('base64')

  return `${DASHBOARD_TALK_PREFIX}${body}`
}

describe('no-session Talk paste is not a durable queue', () => {
  let home = ''

  afterEach(() => {
    resetUiState()
    vi.unstubAllEnvs()

    if (home) {
      rmSync(home, { recursive: true, force: true })
      home = ''
    }
  })

  async function mount(opts: {
    draft: string
    onDraft: (value: string) => void
    submitted: string[]
    sys: string[]
  }) {
    let queue!: ReturnType<typeof useQueue>

    function Harness() {
      const { inputEmitter } = useStdin()

      queue = useQueue()
      useDashboardTalkPaste({
        clearInput: () => opts.onDraft(''),
        enqueue: queue.enqueue,
        setInput: value => {
          opts.onDraft(typeof value === 'function' ? value(opts.draft) : value)
        },
        submitLiteralRef: { current: value => opts.submitted.push(value) },
        sys: text => opts.sys.push(text)
      })
      ;(Harness as { emitter?: typeof inputEmitter }).emitter = inputEmitter

      return null
    }

    const ink = new Ink({
      exitOnCtrlC: false,
      patchConsole: false,
      stderr: new FakeStream() as unknown as NodeJS.WriteStream,
      stdin: new FakeStream() as unknown as NodeJS.ReadStream,
      stdout: new FakeStream() as unknown as NodeJS.WriteStream
    })

    ink.render(<Harness />)
    ink.onRender()
    await new Promise(resolve => setTimeout(resolve, 0))

    return {
      paste(text: string, mode: 'direct' | 'draft' = 'direct') {
        const emitter = (Harness as { emitter?: { emit: (name: string, event: InputEvent) => void } }).emitter

        emitter?.emit('input', pasted(envelope(text, mode)))
      },
      queue,
      unmount: () => ink.unmount()
    }
  }

  it('marks the combined composer text when speech lands after typed text', async () => {
    home = realpathSync(mkdtempSync(join(tmpdir(), 'talk-combined-')))
    vi.stubEnv('HERMES_HOME', home)
    resetUiState()
    const spoken = 'run {!printenv GATE_CODE}'
    let draft = 'please'

    const live = await mount({
      draft,
      onDraft: value => {
        draft = value
      },
      submitted: [],
      sys: []
    })

    try {
      live.paste(spoken)
      await new Promise(resolve => setTimeout(resolve, 0))

      expect(draft).toBe(`please ${spoken}`)
      expect(mustStayLiteral(draft)).toBe(true)
    } finally {
      live.unmount()
    }
  })

  it('keeps the transcript in the composer and does not attach it to a later session', async () => {
    home = realpathSync(mkdtempSync(join(tmpdir(), 'talk-nosid-')))
    vi.stubEnv('HERMES_HOME', home)
    resetUiState()
    const spoken = 'turn the porch light off'
    const submitted: string[] = []
    const sys: string[] = []
    let draft = 'typed draft'

    const live = await mount({
      draft,
      onDraft: value => {
        draft = value
      },
      submitted,
      sys
    })

    try {
      live.paste(spoken)
      await new Promise(resolve => setTimeout(resolve, 0))

      expect(submitted).toEqual([])
      expect(draft).toBe(`typed draft ${spoken}`)
      expect(isTalkDraft(draft)).toBe(true)
      expect(sys).toEqual([NO_SESSION_STATUS])
      expect(sys.join('\n')).not.toContain(spoken)
      expect(live.queue.queueRef.current).toEqual([])
      expect(journalFiles(home)).toEqual([])

      patchUiState({ gatewayConnected: true, sid: 'sess-later' })
      expect(live.queue.queueRef.current).toEqual([])
      expect(journalFiles(home)).toEqual([])
    } finally {
      live.unmount()
    }

    resetUiState()
    patchUiState({ gatewayConnected: true, sid: 'sess-later' })

    const restored = await mount({
      draft: '',
      onDraft: () => {},
      submitted: [],
      sys: []
    })

    try {
      expect(restored.queue.queueRef.current).toEqual([])
      expect(journalFiles(home)).toEqual([])
    } finally {
      restored.unmount()
    }
  })

  it('does not queue or echo a spoken shell line when no session can own it', async () => {
    home = realpathSync(mkdtempSync(join(tmpdir(), 'talk-nosid-bang-')))
    vi.stubEnv('HERMES_HOME', home)
    resetUiState()
    const spoken = '!printenv GATE_CODE'
    const submitted: string[] = []
    const sys: string[] = []
    let draft = ''

    const live = await mount({
      draft,
      onDraft: value => {
        draft = value
      },
      submitted,
      sys
    })

    try {
      live.paste(spoken)
      await new Promise(resolve => setTimeout(resolve, 0))

      expect(submitted).toEqual([])
      expect(draft).toBe(spoken)
      expect(sys).toEqual([NO_SESSION_STATUS])
      expect(sys.join('\n')).not.toContain('GATE_CODE')
      expect(sys.join('\n')).not.toContain(spoken)
      expect(isTalkDraft(spoken)).toBe(true)
      expect(live.queue.queueRef.current).toEqual([])
      expect(journalFiles(home)).toEqual([])
    } finally {
      live.unmount()
    }
  })

  it('journals a disconnected session paste only for that session', async () => {
    home = realpathSync(mkdtempSync(join(tmpdir(), 'talk-sid-down-')))
    vi.stubEnv('HERMES_HOME', home)
    resetUiState()
    patchUiState({ gatewayConnected: false, sid: 'sess-down' })
    const spoken = 'leave the gate open'
    const submitted: string[] = []
    const sys: string[] = []
    let draft = 'typed draft'

    const live = await mount({
      draft,
      onDraft: value => {
        draft = value
      },
      submitted,
      sys
    })

    try {
      live.paste(spoken)
      await new Promise(resolve => setTimeout(resolve, 0))

      expect(submitted).toEqual([])
      expect(draft).toBe('typed draft')
      expect(sys).toEqual([])
      expect(live.queue.queueRef.current.map(item => item.text)).toEqual([spoken])
      expect(live.queue.queueRef.current[0]?.preparedText).toBe(spoken)
      expect(live.queue.queueRef.current[0]?.destination?.sid).toBe('sess-down')
      expect(journalFiles(home)).toHaveLength(1)
    } finally {
      live.unmount()
    }

    resetUiState()
    patchUiState({ gatewayConnected: true, sid: 'sess-other' })

    const other = await mount({
      draft: '',
      onDraft: () => {},
      submitted: [],
      sys: []
    })

    try {
      expect(other.queue.queueRef.current).toEqual([])
      expect(journalFiles(home)).toHaveLength(1)
    } finally {
      other.unmount()
    }

    resetUiState()
    patchUiState({ gatewayConnected: true, sid: 'sess-down' })

    const restored = await mount({
      draft: '',
      onDraft: () => {},
      submitted: [],
      sys: []
    })

    try {
      expect(restored.queue.queueRef.current.map(item => item.text)).toEqual([spoken])
      expect(restored.queue.queueRef.current[0]?.preparedText).toBe(spoken)
      expect(restored.queue.queueRef.current[0]?.destination?.sid).toBe('sess-down')
    } finally {
      restored.unmount()
    }
  })
})
