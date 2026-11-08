import { mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { PassThrough } from 'node:stream'

import { renderSync, Text } from '@hermes/ink'
import React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { patchUiState, resetUiState } from '../app/uiStore.js'
import { useSubmission } from '../app/useSubmission.js'
import { useQueue } from '../hooks/useQueue.js'
import * as pendingInputs from '../lib/pendingInputs.js'

const LEAK = 'porch-light-4821'

function streams() {
  return {
    stdin: new PassThrough() as never,
    stdout: Object.assign(new PassThrough(), { columns: 80, rows: 20, isTTY: false }) as never,
    stderr: new PassThrough() as never,
    patchConsole: false
  }
}

describe('a stage/claim crash recovers spoken shell syntax as literal text', () => {
  let home = ''

  afterEach(() => {
    resetUiState()
    vi.unstubAllEnvs()
    vi.restoreAllMocks()

    if (home) {
      rmSync(home, { recursive: true, force: true })
      home = ''
    }
  })

  async function crashThenRetry(spoken: string): Promise<void> {
    home = realpathSync(mkdtempSync(join(tmpdir(), 'talk-crash-')))
    vi.stubEnv('HERMES_HOME', home)
    resetUiState()
    patchUiState({ gatewayConnected: true, sid: 'sess-live' })

    const realSave = pendingInputs.savePendingInput

    const save = vi.spyOn(pendingInputs, 'savePendingInput').mockImplementation(item => {
      realSave(item)
      save.mockRestore()
      throw new Error('crash after claim persist')
    })

    let submission!: ReturnType<typeof useSubmission>

    function Live() {
      const queue = useQueue()

      submission = useSubmission({
        appendMessage: () => {},
        composerActions: {
          ...queue,
          prependQueue: queue.prependQ,
          takeQueue: queue.takeQ,
          clearIn: () => {}
        } as never,
        composerRefs: {
          queueRef: queue.queueRef,
          queueEditRef: queue.queueEditRef,
          tokensRef: { current: [] }
        } as never,
        composerState: { input: '', inputBuf: [], completions: [] } as never,
        gw: { request: vi.fn(async () => ({ status: 'streaming' })) } as never,
        setLastUserMsg: () => {},
        slashRef: { current: () => true },
        submitRef: { current: () => {} },
        sys: () => {}
      })

      return null
    }

    const live = renderSync(<Live />, streams())

    try {
      // Live Talk calls submitLiteral, which stages before preparedText is set.
      expect(() => submission.submitLiteral(spoken)).toThrow('crash after claim persist')
    } finally {
      live.unmount()
    }

    const journal = join(home, 'tui-pending-inputs')
    const files = readdirSync(journal).filter(name => name.endsWith('.json'))

    expect(files).toHaveLength(1)
    expect(JSON.parse(readFileSync(join(journal, files[0]!), 'utf8'))).toMatchObject({
      attempted: true,
      preparedText: spoken,
      text: spoken
    })

    resetUiState()
    patchUiState({ gatewayConnected: true, sid: 'sess-live' })
    const sys: string[] = []

    const request = vi.fn((method: string) => {
      if (method === 'shell.exec') {
        return Promise.reject(new Error(`command failed: ${LEAK}`))
      }

      return Promise.resolve({ status: 'streaming' })
    })

    let drained!: ReturnType<typeof useQueue>
    let recovered!: ReturnType<typeof useSubmission>

    function Recover() {
      drained = useQueue()
      recovered = useSubmission({
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

    const instance = renderSync(<Recover />, streams())

    try {
      const item = drained.dequeue(true)

      expect(item?.text).toBe(spoken)
      recovered.sendQueued(item!)
      await new Promise(resolve => setTimeout(resolve, 0))
      expect(request.mock.calls.map(call => call[0])).not.toContain('shell.exec')
      expect(request).toHaveBeenCalledWith(
        'prompt.submit',
        expect.objectContaining({ session_id: 'sess-live', text: spoken })
      )
      expect(JSON.stringify(sys)).not.toContain(LEAK)
    } finally {
      instance.unmount()
    }
  }

  it('does not run a spoken !command recovered from a stage/claim crash', async () => {
    await crashThenRetry(`!echo ${LEAK}`)
  })

  it('does not interpolate a spoken {!command} recovered from a stage/claim crash', async () => {
    await crashThenRetry(`please run {!echo ${LEAK}}`)
  })
})
