/**
 * Spoken text that Talk left in the composer because it could not be queued.
 * Enter on that draft must submit it as a prompt even after a session attaches:
 * the speaker never typed `!`, `/` or `{!…}`, so none of them may reach the
 * shell or the slash handler. Matched by exact equality with the composer text:
 * once the user edits or deletes the draft, what they type next is theirs and
 * keeps its normal meaning. A match is consumed by `consumeTalkDraft`.
 */
const MAX_DRAFTS = 32

const drafts = new Set<string>()

export function markTalkDraft(text: string): void {
  const spoken = text.trim()

  if (!spoken) {
    return
  }

  drafts.delete(spoken)
  drafts.add(spoken)

  if (drafts.size > MAX_DRAFTS) {
    drafts.delete(drafts.values().next().value as string)
  }
}

export const isTalkDraft = (input: string): boolean => drafts.has(input.trim())

export const consumeTalkDraft = (input: string): boolean => drafts.delete(input.trim())

export function clearTalkDrafts(): void {
  drafts.clear()
}
