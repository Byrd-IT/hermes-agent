/**
 * No-session Talk keeps speech in the composer. A later Enter must not treat
 * that unsaved text as a shell command. It still joins the unbound queue and
 * carries onto the session that appears next, as a prompt.
 */
import { mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { PassThrough } from 'node:stream'

import { renderSync, Text } from '@hermes/ink'
import React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { mustStayLiteral } from '../app/submissionTalkDraft.js'
import { clearTalkDrafts, isTalkDraft, markTalkDraft } from '../app/talkDraftProvenance.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { useSubmission } from '../app/useSubmission.js'
import { useQueue } from '../hooks/useQueue.js'

const LEAK = 'gate-code-output-should-not-surface'

function streams() {
  return {
    stdin: new PassThrough() as never,
    stdout: Object.assign(new PassThrough(), { columns: 80, rows: 20, isTTY: false }) as never,
    stderr: new PassThrough() as never,
    patchConsole: false
  }
}

describe('no-session composer Enter never executes shell', () => {
  let home = ''

  afterEach(() => {
    resetUiState()
    clearTalkDrafts()
    vi.unstubAllEnvs()

    if (home) {
      rmSync(home, { recursive: true, force: true })
      home = ''
    }
  })

  function mount(failEnqueue = false) {
    home = realpathSync(mkdtempSync(join(tmpdir(), 'talk-nosid-enter-')))
    vi.stubEnv('HERMES_HOME', home)
    resetUiState()

    const sys: string[] = []
    const slash = vi.fn(() => true)

    const request = vi.fn((method: string) => {
      if (method === 'shell.exec') {
        return Promise.resolve({ stdout: LEAK, stderr: '', code: 0 })
      }

      return Promise.resolve({ status: 'streaming' })
    })

    let queue!: ReturnType<typeof useQueue>
    let submission!: ReturnType<typeof useSubmission>

    function Harness() {
      queue = useQueue()
      submission = useSubmission({
        appendMessage: () => {},
        composerActions: {
          ...queue,
          enqueue: failEnqueue
            ? () => {
                throw new Error(`write ${LEAK}`)
              }
            : queue.enqueue,
          clearIn: () => {},
          collapsePaste: (value: string) => value,
          prependQueue: queue.prependQ,
          pushHistory: () => {},
          takeQueue: queue.takeQ
        } as never,
        composerRefs: {
          queueRef: queue.queueRef,
          queueEditRef: queue.queueEditRef,
          tokensRef: { current: [] }
        } as never,
        composerState: { input: '', inputBuf: [], completions: [] } as never,
        gw: { request } as never,
        setLastUserMsg: () => {},
        slashRef: { current: slash as never },
        submitRef: { current: () => {} },
        sys: text => sys.push(text)
      })

      return <Text>{queue.queuedDisplay.join('|')}</Text>
    }

    const instance = renderSync(<Harness />, streams())

    return {
      queue,
      request,
      slash,
      submission,
      sys,
      unmount: () => instance.unmount()
    }
  }

  it('carries a spoken bang line onto the next session instead of running it', async () => {
    const spoken = '!printenv GATE_CODE'
    const live = mount()

    try {
      markTalkDraft(spoken)
      live.submission.submit(spoken)
      await new Promise(resolve => setTimeout(resolve, 0))

      expect(live.request.mock.calls.map(call => call[0])).not.toContain('shell.exec')
      expect(live.sys.join('\n')).not.toContain(LEAK)
      expect(live.queue.queueRef.current.map(item => item.text)).toEqual([spoken])
      expect(live.queue.queueRef.current[0]?.preparedText).toBe(spoken)

      patchUiState({ gatewayConnected: true, sid: 'sess-later' })
      expect(live.queue.queueRef.current[0]?.destination?.sid).toBe('sess-later')
      expect(live.queue.queueRef.current[0]?.preparedText).toBe(spoken)
      const journal = join(home, 'tui-pending-inputs')
      const files = readdirSync(journal).filter(name => name.endsWith('.json'))

      expect(files).toHaveLength(1)
      expect(JSON.parse(readFileSync(join(journal, files[0]!), 'utf8'))).toMatchObject({
        preparedText: spoken,
        text: spoken
      })

      const item = live.queue.dequeue()

      expect(item?.text).toBe(spoken)
      live.submission.sendQueued(item!)
      await new Promise(resolve => setTimeout(resolve, 0))

      expect(live.request.mock.calls.map(call => call[0])).not.toContain('shell.exec')
      expect(live.request).toHaveBeenCalledWith(
        'prompt.submit',
        expect.objectContaining({ session_id: 'sess-later', text: spoken })
      )
      expect(live.sys.join('\n')).not.toContain(LEAK)
    } finally {
      live.unmount()
    }
  })

  it('carries a spoken interpolation onto the next session instead of running it', async () => {
    const spoken = 'please run {!printenv GATE_CODE}'
    const live = mount()

    try {
      markTalkDraft(spoken)
      live.submission.submit(spoken)
      await new Promise(resolve => setTimeout(resolve, 0))

      expect(live.request.mock.calls.map(call => call[0])).not.toContain('shell.exec')
      expect(live.queue.queueRef.current[0]?.preparedText).toBe(spoken)

      patchUiState({ gatewayConnected: true, sid: 'sess-later' })
      const item = live.queue.dequeue()

      expect(item?.preparedText).toBe(spoken)
      live.submission.sendQueued(item!)
      await new Promise(resolve => setTimeout(resolve, 0))

      expect(live.request.mock.calls.map(call => call[0])).not.toContain('shell.exec')
      expect(live.request).toHaveBeenCalledWith(
        'prompt.submit',
        expect.objectContaining({ session_id: 'sess-later', text: spoken })
      )
      expect(live.sys.join('\n')).not.toContain(LEAK)
    } finally {
      live.unmount()
    }
  })

  it('still carries ordinary no-session Enter without turning it into a literal shell escape', () => {
    const text = 'turn the porch light off'
    const live = mount()

    try {
      live.submission.submit(text)

      expect(live.request.mock.calls.map(call => call[0])).toEqual([])
      expect(live.queue.queueRef.current.map(item => item.text)).toEqual([text])
      expect(live.queue.queueRef.current[0]?.preparedText).toBeUndefined()

      patchUiState({ gatewayConnected: true, sid: 'sess-later' })
      expect(live.queue.queueRef.current.map(item => item.text)).toEqual([text])
      expect(live.queue.queueRef.current[0]?.destination?.sid).toBe('sess-later')
      expect(live.queue.queueRef.current[0]?.preparedText).toBeUndefined()
    } finally {
      live.unmount()
    }
  })

  it('still runs a bang command once a session owns the composer', async () => {
    const live = mount()

    try {
      patchUiState({ gatewayConnected: true, sid: 'sess-live' })
      live.submission.submit('!pwd')
      await new Promise(resolve => setTimeout(resolve, 0))

      expect(live.request).toHaveBeenCalledWith(
        'shell.exec',
        expect.objectContaining({ command: 'pwd', session_id: 'sess-live' })
      )
      expect(live.queue.queueRef.current).toEqual([])
    } finally {
      live.unmount()
    }
  })

  it('keeps a Talk draft literal when a session attaches before Enter', async () => {
    const spoken = '!printenv GATE_CODE'
    const live = mount()

    try {
      markTalkDraft(spoken)
      patchUiState({ gatewayConnected: true, sid: 'sess-later' })
      live.submission.submit(spoken)
      await new Promise(resolve => setTimeout(resolve, 0))

      expect(live.request.mock.calls.map(call => call[0])).not.toContain('shell.exec')
      expect(live.sys.join('\n')).not.toContain(LEAK)
      expect(live.request).toHaveBeenCalledWith(
        'prompt.submit',
        expect.objectContaining({ session_id: 'sess-later', text: spoken })
      )
    } finally {
      live.unmount()
    }
  })

  it('keeps a spoken slash draft literal once a session exists', async () => {
    const spoken = '/quit now'
    const live = mount()

    try {
      markTalkDraft(spoken)
      patchUiState({ gatewayConnected: true, sid: 'sess-later' })
      live.submission.submit(spoken)
      await new Promise(resolve => setTimeout(resolve, 0))

      expect(live.slash).not.toHaveBeenCalled()
      expect(live.request.mock.calls.map(call => call[0])).not.toContain('shell.exec')
    } finally {
      live.unmount()
    }
  })

  it('lets typed commands run after the user edited or deleted a Talk draft', async () => {
    const live = mount()

    try {
      markTalkDraft('status')
      patchUiState({ gatewayConnected: true, sid: 'sess-live' })
      live.submission.submit('!git status')
      await new Promise(resolve => setTimeout(resolve, 0))

      expect(live.request).toHaveBeenCalledWith(
        'shell.exec',
        expect.objectContaining({ command: 'git status', session_id: 'sess-live' })
      )
      expect(isTalkDraft('status')).toBe(true)
    } finally {
      live.unmount()
    }
  })

  it('does not treat a typed bang line with no session as a Talk draft', () => {
    markTalkDraft('something else')

    expect(mustStayLiteral('!pwd')).toBe(false)
    expect(mustStayLiteral('{!pwd}')).toBe(false)
  })

  it('queues a Talk draft literally behind a busy turn', async () => {
    const spoken = 'run {!printenv GATE_CODE}'
    const live = mount()

    try {
      markTalkDraft(spoken)
      patchUiState({ busy: true, gatewayConnected: true, sid: 'sess-busy' })
      live.submission.submit(spoken)
      await new Promise(resolve => setTimeout(resolve, 0))

      expect(live.request.mock.calls.map(call => call[0])).not.toContain('shell.exec')
      expect(live.queue.queueRef.current[0]?.preparedText).toBe(spoken)
    } finally {
      live.unmount()
    }
  })

  it('treats the same text as a command after the draft was consumed', async () => {
    const live = mount()

    try {
      markTalkDraft('!pwd')
      patchUiState({ gatewayConnected: true, sid: 'sess-live' })
      live.submission.submit('!pwd')
      await new Promise(resolve => setTimeout(resolve, 0))
      live.submission.submit('!pwd')
      await new Promise(resolve => setTimeout(resolve, 0))

      expect(live.request.mock.calls.filter(call => call[0] === 'shell.exec')).toHaveLength(1)
    } finally {
      live.unmount()
    }
  })

  it('reports a failed draft save without echoing the error text', async () => {
    const live = mount(true)

    try {
      markTalkDraft('!printenv GATE_CODE')
      live.submission.submit('!printenv GATE_CODE')
      await new Promise(resolve => setTimeout(resolve, 0))

      expect(live.sys.join('\n')).toContain('queue write failed')
      expect(live.sys.join('\n')).not.toContain(LEAK)
      expect(live.request.mock.calls.map(call => call[0])).not.toContain('shell.exec')
    } finally {
      live.unmount()
    }
  })
})
