import type { QueueItem } from '../hooks/useQueue.js'

import type { ComposerActions, ComposerToken } from './interfaces.js'
import { stagePastes } from './pasteStore.js'
import type { SubmissionDestination } from './submissionDestination.js'
import { consumeTalkDraft, isTalkDraft } from './talkDraftProvenance.js'
import { getUiState } from './uiStore.js'

interface TalkDraftContext {
  composerActions: ComposerActions
  destination: SubmissionDestination
  full: string
  journaled: <T>(write: () => T) => { value: T } | undefined
  send: (
    text: string,
    showUserMessage: boolean,
    displayText: string,
    expandOverride: (value: string) => string,
    submitOpts: { attachments?: QueueItem['attachments']; literal?: boolean; skipDetectDrop?: boolean }
  ) => void
  submission: { attachments?: QueueItem['attachments']; display: string; text: string }
  tokens: ComposerToken[]
  toHistory: string
}

/** Text that must reach the model as a prompt: the spoken draft Talk kept in the composer. */
export const mustStayLiteral = (full: string): boolean => isTalkDraft(full)

/**
 * Enter on spoken text Talk left in the composer. It goes out as a prompt, never
 * as `/cmd`, `!cmd` or `{!cmd}`, whichever session exists by now. Without a live,
 * idle session it joins the literal queue so the drain cannot run it either.
 */
export function submitTalkDraft(ctx: TalkDraftContext): void {
  const { composerActions, destination, full, journaled, send, submission, tokens, toHistory } = ctx

  consumeTalkDraft(full)
  composerActions.pushHistory(toHistory)
  stagePastes(full, submission.text, tokens)
  const state = getUiState()

  // Busy goes to the local literal queue on purpose: the draft must never reach
  // handleBusyInput's interrupt/steer paths, which treat text as a command line.
  if (!state.sid || state.gatewayConnected === false || state.busy) {
    journaled(() =>
      composerActions.enqueue(submission.text, submission.display, destination, submission.attachments, true)
    )

    return
  }

  journaled(() =>
    send(submission.text, true, submission.display, value => value, {
      attachments: submission.attachments,
      literal: true,
      skipDetectDrop: true
    })
  )
}
