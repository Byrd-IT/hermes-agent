/**
 * A Talk draft stays a prompt, whichever edit is armed.
 *
 * An armed backtrack edit sends through `rewindAndSend`, which queues the text
 * without the literal flag: a spoken draft holding `{!cmd}` could run as shell.
 * The draft disarms the edit instead, and a failed journal write must not push
 * the same history entry twice.
 */
import { mkdtempSync, realpathSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { PassThrough } from 'node:stream'

import { renderSync, Text } from '@hermes/ink'
import React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { $pendingRewind, armPendingRewind } from '../app/backtrackStore.js'
import type { SubmissionDestination } from '../app/submissionDestination.js'
import { clearTalkDrafts, isTalkDraft, markTalkDraft } from '../app/talkDraftProvenance.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { useSubmission } from '../app/useSubmission.js'
import { type QueueItem, useQueue } from '../hooks/useQueue.js'

const LEAK = 'armed-rewind-gate-code'

interface LiteralCall {
  literal: boolean
  text: string
}

function streams() {
  return {
    stdin: new PassThrough() as never,
    stdout: Object.assign(new PassThrough(), { columns: 80, rows: 20, isTTY: false }) as never,
    stderr: new PassThrough() as never,
    patchConsole: false
  }
}

describe('a Talk draft never rides an armed backtrack edit', () => {
  let home = ''

  afterEach(() => {
    resetUiState()
    clearTalkDrafts()
    $pendingRewind.set(null)
    vi.unstubAllEnvs()

    if (home) {
      rmSync(home, { recursive: true, force: true })
      home = ''
    }
  })

  function mount(options: { failEnqueue?: 'once' | 'always' } = {}) {
    home = realpathSync(mkdtempSync(join(tmpdir(), 'talk-armed-rewind-')))
    vi.stubEnv('HERMES_HOME', home)
    resetUiState()

    const sys: string[] = []
    const slash = vi.fn(() => true)
    const pushHistory = vi.fn()
    const staged: LiteralCall[] = []
    const enqueued: LiteralCall[] = []

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
          clearIn: () => {},
          collapsePaste: (value: string) => value,
          prependQueue: queue.prependQ,
          pushHistory,
          takeQueue: queue.takeQ,
          stage: (
            text: string,
            display?: string,
            destination?: SubmissionDestination,
            attachments?: QueueItem['attachments'],
            literal?: boolean
          ) => {
            staged.push({ literal: literal === true, text })

            return queue.stage!(text, display, destination, attachments, literal)
          },
          enqueue: (
            text: string,
            display?: string,
            destination?: SubmissionDestination,
            attachments?: QueueItem['attachments'],
            literal?: boolean
          ) => {
            enqueued.push({ literal: literal === true, text })

            if (options.failEnqueue === 'always' || (options.failEnqueue === 'once' && enqueued.length === 1)) {
              throw new Error(`write ${LEAK}`)
            }

            return queue.enqueue(text, display, destination, attachments, literal)
          }
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
      enqueued,
      pushHistory,
      queue,
      request,
      slash,
      staged,
      submission,
      sys,
      unmount: () => instance.unmount()
    }
  }

  it('sends a spoken draft literally instead of rewinding with it', async () => {
    const live = mount()

    try {
      patchUiState({ gatewayConnected: true, sid: 'sess-live' })

      const armedSend = vi.fn()

      armPendingRewind({ send: armedSend, sid: 'sess-live', target: { rowId: 13, text: 'BETA please', turn: 2 } })

      const spoken = '{!touch /tmp/pwned}'

      markTalkDraft(spoken)
      live.submission.submit(spoken)
      await new Promise(resolve => setTimeout(resolve, 0))

      // The armed edit never ran: the draft is a prompt, not a rewound command line.
      expect(armedSend).not.toHaveBeenCalled()
      expect($pendingRewind.get()).toBeNull()
      expect(live.request.mock.calls.map(call => call[0])).not.toContain('shell.exec')
      expect(live.sys.join('\n')).not.toContain(LEAK)

      // It went out as literal text instead.
      expect(live.staged).toEqual([{ literal: true, text: spoken }])
      expect(live.request).toHaveBeenCalledWith('prompt.submit', expect.objectContaining({ text: spoken }))
    } finally {
      live.unmount()
    }
  })

  it('still rewinds with an ordinary message', async () => {
    const live = mount()

    try {
      patchUiState({ gatewayConnected: true, sid: 'sess-live' })

      const armedSend = vi.fn()

      armPendingRewind({ send: armedSend, sid: 'sess-live', target: { rowId: 13, text: 'BETA please', turn: 2 } })

      live.submission.submit('BETA please v2')
      await new Promise(resolve => setTimeout(resolve, 0))

      expect(armedSend).toHaveBeenCalledTimes(1)
      expect(armedSend.mock.calls[0]![0]).toMatchObject({ text: 'BETA please v2' })
      expect($pendingRewind.get()).toBeNull()
    } finally {
      live.unmount()
    }
  })

  it('pushes the history entry once, only after the write succeeded', async () => {
    const live = mount({ failEnqueue: 'once' })

    try {
      const spoken = 'the porch light is still on'

      markTalkDraft(spoken)
      live.submission.submit(spoken)
      await new Promise(resolve => setTimeout(resolve, 0))

      // The write failed: nothing was sent, so nothing was remembered.
      expect(live.pushHistory).not.toHaveBeenCalled()
      expect(isTalkDraft(spoken)).toBe(true)

      live.submission.submit(spoken)
      await new Promise(resolve => setTimeout(resolve, 0))

      expect(live.pushHistory).toHaveBeenCalledTimes(1)
      expect(live.pushHistory).toHaveBeenCalledWith(spoken)
    } finally {
      live.unmount()
    }
  })
})
