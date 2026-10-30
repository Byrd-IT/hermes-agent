/**
 * Dashboard Talk paste contract shared by the web chat and the TUI.
 *
 * The web chat sends one bracketed paste. The TUI accepts this prefix only
 * on a real paste, then sanitizes the decoded transcript itself: the web
 * client is not trusted. Both sides must use this module so the prefix and
 * the control strip cannot drift.
 *
 * Controls become spaces so a transcript cannot inject Enter, Ctrl, or an
 * escape. A regex class would trip no-control-regex and is easier to get
 * wrong here.
 */

export const DASHBOARD_TALK_PREFIX = 'HERMES_DASH_TALK:'

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
