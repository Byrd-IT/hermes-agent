/**
 * Dashboard Talk paste. The web chat sends one bracketed paste instead of
 * composer keystrokes. The TUI owns clear + submit, and only when no overlay
 * is taking input.
 *
 * The prefix is accepted only on a real paste (Ink's isPasted flag, or the
 * bracketed-paste markers still on the bytes). A typed prefix is ordinary
 * text and must be left alone. The decoded transcript is sanitized here:
 * the web client is not trusted.
 */

export const DASHBOARD_TALK_PREFIX = 'HERMES_DASH_TALK:'

/** A 120s utterance is a few thousand characters. Longer is a forged dump. */
export const MAX_TALK_TEXT_CHARS = 8000

export const TALK_REFUSED = 'Talk ignored: close the open menu first.'

export type DashboardTalkMode = 'direct' | 'draft'

export interface DashboardTalkPayload {
  text: string
  mode: DashboardTalkMode
}

export type DashboardTalkParse = { kind: 'talk'; payload: DashboardTalkPayload } | { kind: 'invalid' } | null

const PASTE_START = '\u001b[200~'
const PASTE_END = '\u001b[201~'
const BARE_START = '[200~'
const BARE_END = '[201~'

function stripEdge(text: string, edge: string, fromStart: boolean): string {
  if (fromStart && text.startsWith(edge)) {
    return text.slice(edge.length)
  }

  if (!fromStart && text.endsWith(edge)) {
    return text.slice(0, -edge.length)
  }

  return text
}

function isWrappedPaste(input: string): boolean {
  const start = input.startsWith(PASTE_START) || input.startsWith(BARE_START)
  const end = input.endsWith(PASTE_END) || input.endsWith(BARE_END)

  return start && end && input.length > PASTE_START.length + PASTE_END.length
}

function unwrapPaste(input: string): string {
  let text = stripEdge(input, PASTE_START, true)
  text = stripEdge(text, BARE_START, true)
  text = stripEdge(text, PASTE_END, false)
  text = stripEdge(text, BARE_END, false)

  return text
}

/** Strip controls so a forged paste cannot inject Enter, Ctrl, or an escape. */
export function sanitizeTalkText(text: string): string {
  let cleaned = ''

  for (const ch of text) {
    const code = ch.codePointAt(0) ?? 0

    if (code <= 0x1f || code === 0x7f || code === 0x2028 || code === 0x2029) {
      cleaned += ' '
    } else {
      cleaned += ch
    }
  }

  return cleaned.replace(/[ \t\n\f\v]+/g, ' ').trim()
}

function decodePayload(body: string): DashboardTalkPayload | null {
  let raw: string

  try {
    raw = Buffer.from(body, 'base64').toString('utf8')
  } catch {
    return null
  }

  let parsed: unknown

  try {
    parsed = JSON.parse(raw)
  } catch {
    return null
  }

  if (!parsed || typeof parsed !== 'object') {
    return null
  }

  const record = parsed as { text?: unknown; mode?: unknown }

  if (typeof record.text !== 'string') {
    return null
  }

  if (record.mode !== 'direct' && record.mode !== 'draft') {
    return null
  }

  const text = sanitizeTalkText(record.text)

  if (!text || text.length > MAX_TALK_TEXT_CHARS) {
    return null
  }

  return { text, mode: record.mode }
}

export interface TalkPasteOptions {
  /** Ink already classified this input as a bracketed paste. */
  pasted?: boolean
}

/** null means this input is not a Talk paste and must be left alone. */
export function parseDashboardTalkPaste(input: string, opts?: TalkPasteOptions): DashboardTalkParse {
  const wrapped = isWrappedPaste(input)

  if (!opts?.pasted && !wrapped) {
    return null
  }

  const unwrapped = unwrapPaste(input)

  if (!unwrapped.startsWith(DASHBOARD_TALK_PREFIX)) {
    return null
  }

  const payload = decodePayload(unwrapped.slice(DASHBOARD_TALK_PREFIX.length))

  return payload ? { kind: 'talk', payload } : { kind: 'invalid' }
}

export type DashboardTalkDecision = 'ignore' | 'refuse' | 'apply' | 'drop'

export function decideDashboardTalk(opts: {
  dashboard: boolean
  blocked: boolean
  parsed: DashboardTalkParse
}): DashboardTalkDecision {
  if (!opts.dashboard || opts.parsed == null) {
    return 'ignore'
  }

  if (opts.parsed.kind === 'invalid') {
    return 'drop'
  }

  return opts.blocked ? 'refuse' : 'apply'
}

export interface TalkInputEvent {
  input?: string
  keypress?: { isPasted?: boolean }
  stopImmediatePropagation?: () => void
}

/**
 * Consume a Talk paste before later listeners run. Returns false when the
 * event is ordinary input and must keep propagating.
 */
export function handleDashboardTalkInput(
  event: TalkInputEvent,
  ctx: {
    dashboard: boolean
    blocked: boolean
    apply: (text: string, mode: DashboardTalkMode) => void
    sys: (text: string) => void
  }
): boolean {
  const parsed = parseDashboardTalkPaste(event.input ?? '', {
    pasted: event.keypress?.isPasted === true
  })

  const decision = decideDashboardTalk({
    dashboard: ctx.dashboard,
    blocked: ctx.blocked,
    parsed
  })

  if (decision === 'ignore' || parsed == null) {
    return false
  }

  event.stopImmediatePropagation?.()

  if (decision === 'refuse') {
    ctx.sys(TALK_REFUSED)

    return true
  }

  if (decision === 'apply' && parsed.kind === 'talk') {
    ctx.apply(parsed.payload.text, parsed.payload.mode)
  }

  return true
}
