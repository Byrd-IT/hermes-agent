import { useEffect, useState } from 'react'

import { DEFAULT_VOICE_RECORD_KEY, type ParsedVoiceRecordKey } from '../lib/platform.js'

/**
 * Session chrome that useMainApp only reads and passes down. Grouped so the
 * caller reads `chrome.voice.enabled` instead of unpacking every name onto
 * one line (a format pass would put useMainApp back over its line cap).
 */
export function useSessionChromeState() {
  const [enabled, setEnabled] = useState(false)
  const [tts, setTts] = useState(false)
  const [recording, setRecording] = useState(false)
  const [processing, setProcessing] = useState(false)
  const [recordKey, setRecordKey] = useState<ParsedVoiceRecordKey>(DEFAULT_VOICE_RECORD_KEY)
  const [sessionStartedAt, setSessionStartedAt] = useState(() => Date.now())
  const [freshSessionId, setFreshSessionId] = useState<null | string>(null)
  const [turnStartedAt, setTurnStartedAt] = useState<null | number>(null)
  const [lastTurnEndedAt, setLastTurnEndedAt] = useState<null | number>(null)
  const [onComplete, setOnComplete] = useState(false)
  const [onPrompt, setOnPrompt] = useState(false)

  return {
    bell: { onComplete, onPrompt, setOnComplete, setOnPrompt },
    clock: {
      freshSessionId,
      lastTurnEndedAt,
      sessionStartedAt,
      setFreshSessionId,
      setLastTurnEndedAt,
      setSessionStartedAt,
      setTurnStartedAt,
      turnStartedAt
    },
    voice: {
      enabled,
      processing,
      recordKey,
      recording,
      setEnabled,
      setProcessing,
      setRecordKey,
      setRecording,
      setTts,
      tts
    }
  }
}

/** Stamp turn start and end from the busy flag. Kept out of useMainApp. */
export function useTurnClock(
  clock: {
    setLastTurnEndedAt: (value: null | number) => void
    setTurnStartedAt: (value: null | number | ((prev: null | number) => null | number)) => void
    turnStartedAt: null | number
  },
  busy: boolean
): void {
  const { setLastTurnEndedAt, setTurnStartedAt, turnStartedAt } = clock

  useEffect(() => {
    if (busy) {
      setTurnStartedAt(prev => prev ?? Date.now())
    } else if (turnStartedAt != null) {
      // Only stamp the idle marker when a turn was actually live — busy is
      // also false on mount and we don't want a phantom "done" timestamp
      // before the first turn has completed.
      setLastTurnEndedAt(Date.now())
      setTurnStartedAt(null)
    }
  }, [busy, setLastTurnEndedAt, setTurnStartedAt, turnStartedAt])
}
