import { useStdin } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { useLayoutEffect, useRef } from 'react'
import type { MutableRefObject } from 'react'

import { DASHBOARD_TUI_MODE } from '../config/env.js'
import { handleDashboardTalkInput } from '../lib/dashboardTalkPaste.js'

import { $isBlocked } from './overlayStore.js'
import { markNextSubmitVoice } from './submissionCore.js'
import { captureDestination, isCurrentDestination, type SubmissionDestination } from './submissionDestination.js'

type SetInput = (value: string | ((current: string) => string)) => void

interface DashboardTalkPasteOptions {
  setInput: SetInput
  submitRef: MutableRefObject<(value: string) => void>
  enqueue?: (text: string, display?: string, destination?: SubmissionDestination) => void
  sys: (text: string) => void
}

/** Live read so a test can set the flag after the module has loaded. */
export function dashboardTalkEnabled(): boolean {
  if (DASHBOARD_TUI_MODE) {
    return true
  }

  return /^(?:1|true|yes|on)$/i.test((process.env.HERMES_TUI_DASHBOARD ?? '').trim())
}

function applyTalkPaste(opts: DashboardTalkPasteOptions, text: string, mode: 'direct' | 'draft'): void {
  const destination = captureDestination()

  if (!isCurrentDestination(destination)) {
    opts.enqueue?.(text, text, destination)

    return
  }

  if (mode === 'draft') {
    opts.setInput(current => (current.trim() ? `${current.trimEnd()} ${text}` : text))

    return
  }

  opts.setInput('')
  setTimeout(() => {
    if (isCurrentDestination(destination)) {
      markNextSubmitVoice(text)
      opts.submitRef.current(text)
    } else {
      opts.enqueue?.(text, text, destination)
    }
  }, 0)
}

/**
 * Consume a dashboard Talk paste before overlays and the composer see it.
 * Registered in layout so it stays ahead of overlay useInput listeners.
 * The handler stops propagation; later listeners never receive the raw prefix.
 */
export function useDashboardTalkPaste(opts: DashboardTalkPasteOptions): void {
  const blocked = useStore($isBlocked)
  const { inputEmitter } = useStdin()
  const optsRef = useRef(opts)
  const blockedRef = useRef(blocked)

  optsRef.current = opts
  blockedRef.current = blocked

  useLayoutEffect(() => {
    const onInput = (event: Parameters<typeof handleDashboardTalkInput>[0]) => {
      const current = optsRef.current

      handleDashboardTalkInput(event, {
        dashboard: dashboardTalkEnabled(),
        blocked: blockedRef.current,
        sys: current.sys,
        apply: (text, mode) => applyTalkPaste(current, text, mode)
      })
    }

    inputEmitter?.on('input', onInput)

    return () => {
      inputEmitter?.removeListener('input', onInput)
    }
  }, [inputEmitter])
}
