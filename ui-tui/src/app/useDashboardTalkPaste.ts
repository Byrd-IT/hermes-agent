import { useStdin } from '@hermes/ink'
import { useLayoutEffect, useRef } from 'react'
import type { MutableRefObject } from 'react'

import { DASHBOARD_TUI_MODE } from '../config/env.js'
import { t } from '../i18n/runtime.js'
import { handleDashboardTalkInput } from '../lib/dashboardTalkPaste.js'

import type { ComposerActions } from './interfaces.js'
import { $isBlocked } from './overlayStore.js'
import { clearNextSubmitVoice, markNextSubmitVoice } from './submissionCore.js'
import { markTalkDraft } from './talkDraftProvenance.js'
import { getUiState } from './uiStore.js'

type SetInput = (value: string | ((current: string) => string)) => void

interface DashboardTalkPasteOptions {
  setInput: SetInput
  clearInput: () => void
  submitLiteralRef: MutableRefObject<(value: string) => void>
  sys: (text: string) => void
  enqueue?: ComposerActions['enqueue']
}

export function dashboardTalkEnabled(): boolean {
  return DASHBOARD_TUI_MODE
}

function keepTalkDraft(opts: DashboardTalkPasteOptions, text: string): void {
  // Provenance is the exact composer text Enter will see, typed prefix included.
  opts.setInput(current => {
    const next = current.trim() ? `${current.trimEnd()} ${text}` : text

    markTalkDraft(next)

    return next
  })
}

// A journal error can echo speech, a path, or a key. The status line is this
// text only — never error.message or String(error).
const TALK_SAVE_FAILED = 'queue write failed'
const TALK_NO_SESSION = 'no session yet'

function applyTalkPaste(opts: DashboardTalkPasteOptions, text: string, mode: 'direct' | 'draft'): void {
  if (mode === 'draft') {
    keepTalkDraft(opts, text)

    return
  }

  // Same gate as Enter: no session, or the socket is down, so prompt.submit
  // would drop the text. Do not clear the composer before that write.
  const live = getUiState()

  if (!live.sid || live.gatewayConnected === false) {
    // Not markNextSubmitVoice: that marker is consumed by the next submit, so a
    // later identical typed prompt would inherit the voice turn.
    if (!opts.enqueue) {
      keepTalkDraft(opts, text)

      return
    }

    // savePendingInput returns without a file when there is no sid. A non-throwing
    // enqueue would look saved, vanish on remount, and attach to whichever
    // session appears next. Keep the only copy in the composer and say so.
    if (!live.sid) {
      keepTalkDraft(opts, text)
      opts.sys(t('canonical.queue.notSavedDraftKept', TALK_NO_SESSION))

      return
    }

    try {
      // Literal before the journal write. Speech is not a composer command:
      // drain would otherwise run a spoken ! / {!} on the gateway host.
      opts.enqueue(text, text, undefined, undefined, true)
    } catch {
      keepTalkDraft(opts, text)
      opts.sys(t('canonical.queue.notSavedDraftKept', TALK_SAVE_FAILED))
    }

    return
  }

  // Submit synchronously to the literal pipeline: yielding lets the session or
  // overlay change, and the Enter handler interprets speech as shell/slash syntax.
  markNextSubmitVoice(text)

  try {
    opts.submitLiteralRef.current(text)
    opts.clearInput()
  } catch {
    clearNextSubmitVoice()
    keepTalkDraft(opts, text)
    opts.sys(t('canonical.queue.notSavedDraftKept', TALK_SAVE_FAILED))
  }
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

/** Wire Talk to the composer's own actions; useMainApp stays under its line cap. */
export function useComposerTalkPaste(
  composerActions: ComposerActions,
  submitLiteralRef: MutableRefObject<(value: string) => void>,
  sys: (text: string) => void
): void {
  useDashboardTalkPaste({
    clearInput: composerActions.clearIn,
    enqueue: composerActions.enqueue,
    setInput: composerActions.setInput,
    submitLiteralRef,
    sys
  })
}
