import { useStdin } from '@hermes/ink'
import { useLayoutEffect, useRef } from 'react'
import type { MutableRefObject } from 'react'

import { DASHBOARD_TUI_MODE } from '../config/env.js'
import { handleDashboardTalkInput } from '../lib/dashboardTalkPaste.js'

import { $isBlocked } from './overlayStore.js'
import { markNextSubmitVoice } from './submissionCore.js'

type SetInput = (value: string | ((current: string) => string)) => void

interface DashboardTalkPasteOptions {
  setInput: SetInput
  clearInput: () => void
  submitLiteralRef: MutableRefObject<(value: string) => void>
  sys: (text: string) => void
}

export function dashboardTalkEnabled(): boolean {
  return DASHBOARD_TUI_MODE
}

function applyTalkPaste(opts: DashboardTalkPasteOptions, text: string, mode: 'direct' | 'draft'): void {
  if (mode === 'draft') {
    opts.setInput(current => (current.trim() ? `${current.trimEnd()} ${text}` : text))

    return
  }

  // Submit synchronously to the literal pipeline: yielding lets the session or
  // overlay change, and the Enter handler interprets speech as shell/slash syntax.
  opts.clearInput()
  markNextSubmitVoice(text)
  opts.submitLiteralRef.current(text)
}

/** Consume Talk before overlays and the composer can see the protocol prefix. */
export function useDashboardTalkPaste(opts: DashboardTalkPasteOptions): void {
  const { inputEmitter } = useStdin()
  const optsRef = useRef(opts)
  optsRef.current = opts

  useLayoutEffect(() => {
    const onInput = (event: Parameters<typeof handleDashboardTalkInput>[0]) => {
      const current = optsRef.current
      handleDashboardTalkInput(event, {
        dashboard: dashboardTalkEnabled(),
        // The store may have changed since React last rendered this hook.
        blocked: $isBlocked.get(),
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
