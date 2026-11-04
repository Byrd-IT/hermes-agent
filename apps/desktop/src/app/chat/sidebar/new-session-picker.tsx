import { useStore } from '@nanostores/react'
import type { RefObject } from 'react'

import { setWorkspaceScope } from '@/components/pane-shell/workspace-scope'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { ProfileGlyph } from '@/components/ui/profile-glyph'
import { useI18n } from '@/i18n'
import { resolveProfileColor } from '@/lib/profile-color'
import { $profileColors, $profiles, newSessionInAgent, newSessionInProfile, normalizeProfileKey } from '@/store/profile'
import { $sessions } from '@/store/session'

import { useGatewaySessionGroups } from './gateway-group-model'
import type { SidebarSessionGroup } from './projects/workspace-groups'

/** One place a new session can start: an exact `[connectionId, profile]` owner. */
export interface NewSessionTarget {
  color: null | string
  connectionId: null | string
  id: string
  label: string
  profile: string
}

/** The owners the picker offers: the same sections the All profiles list shows (a profile
 *  with no chats keeps its entry), so a pick lands exactly where that section's own `+` does. */
export function newSessionTargets(groups: readonly SidebarSessionGroup[]): NewSessionTarget[] {
  return groups
    .filter(group => group.profile)
    .map(group => ({
      color: group.color ?? null,
      connectionId: group.connectionId ?? null,
      id: group.id,
      label: group.label,
      profile: group.profile!
    }))
}

/** The picker's list when the session list yields no sections (it failed to load, or a filter
 *  narrowed it): the active gateway's own profiles, so the menu is never empty. */
export function fallbackNewSessionTargets(
  profiles: readonly { name: string }[],
  colors: Record<string, string>
): NewSessionTarget[] {
  return profiles.map(({ name }) => {
    const profile = normalizeProfileKey(name)

    return {
      color: resolveProfileColor(profile, colors),
      connectionId: null,
      id: JSON.stringify([null, profile]),
      label: profile,
      profile
    }
  })
}

/** Start the draft in `target`'s owner — the call a section's `+` makes. The caller has
 *  already left the single-profile path: this never touches the All profiles scope. */
export function startNewSessionIn(target: Pick<NewSessionTarget, 'connectionId' | 'profile'>): void {
  setWorkspaceScope('sessions')

  if (target.connectionId) {
    newSessionInAgent({ connectionId: target.connectionId, profile: target.profile })
  } else {
    newSessionInProfile(target.profile)
  }
}

interface NewSessionProfilePickerProps {
  /** The New session row: focus returns to it when the menu closes. */
  anchorRef: RefObject<HTMLElement | null>
  onOpenChange: (open: boolean) => void
  open: boolean
}

/**
 * The profile menu the New session row opens while the sidebar is on All profiles.
 * There the live gateway's profile is just whichever one was touched last, so a bare
 * New session would start a chat in a profile the user never chose. The menu is
 * controlled by the row (its pointer-drag and split gestures stay on the button), and
 * anchors to an inert overlay inside the row so it never takes the row's clicks.
 * Must be mounted inside a `relative` container (the sidebar menu item).
 */
export function NewSessionProfilePicker({ anchorRef, onOpenChange, open }: NewSessionProfilePickerProps) {
  const { t } = useI18n()
  const sessions = useStore($sessions)
  const profiles = useStore($profiles)
  const colors = useStore($profileColors)
  const listed = newSessionTargets(useGatewaySessionGroups(sessions, true, false) ?? [])
  const targets = listed.length ? listed : fallbackNewSessionTargets(profiles, colors)

  return (
    <DropdownMenu modal onOpenChange={onOpenChange} open={open}>
      <DropdownMenuTrigger asChild>
        {/* Radix names the menu from its trigger (aria-labelledby beats aria-label). */}
        <span
          aria-hidden="true"
          aria-label={t.sidebar.nav['new-session']}
          className="pointer-events-none absolute inset-0"
          data-new-session-anchor=""
        />
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        aria-label={t.sidebar.nav['new-session']}
        className="min-w-48 max-w-72"
        collisionPadding={8}
        onCloseAutoFocus={event => {
          event.preventDefault()
          anchorRef.current?.focus({ preventScroll: true })
        }}
        side="bottom"
      >
        {targets.map(target => (
          <DropdownMenuItem
            className="min-w-0"
            data-new-session-target={target.id}
            key={target.id}
            onSelect={() => startNewSessionIn(target)}
          >
            <span className="flex min-w-0 items-center gap-1.5">
              <ProfileGlyph
                aria-hidden="true"
                color={target.color}
                isDefault={normalizeProfileKey(target.profile) === 'default' && !target.connectionId}
                name={target.profile}
              />
              <span className="truncate">{target.label}</span>
            </span>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
