// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { SidebarProvider } from '@/components/ui/sidebar'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { setSidebarGrouping } from '@/store/layout'
import { $profiles, setShowAllProfiles } from '@/store/profile'
import type * as ProfileStore from '@/store/profile'
import { $sessions } from '@/store/session'
import { makeSessionInfo } from '@/test/session-info'

import { fallbackNewSessionTargets, newSessionTargets } from './new-session-picker'
import type { SidebarSessionGroup } from './projects/workspace-groups'

const profileStore = vi.hoisted(() => ({
  newSessionInAgent: vi.fn(),
  newSessionInProfile: vi.fn()
}))

vi.mock('@/store/profile', async importOriginal => ({
  ...(await importOriginal<typeof ProfileStore>()),
  newSessionInAgent: profileStore.newSessionInAgent,
  newSessionInProfile: profileStore.newSessionInProfile
}))

import { ChatSidebar } from './index'

const noop = () => {}

const noopAsync = async () => {}

const renderSidebar = (onNavigate: (item: unknown) => void = noop) =>
  render(
    <MemoryRouter initialEntries={['/']}>
      <SidebarProvider>
        <ChatSidebar
          currentView="chat"
          onArchiveSession={noop}
          onBranchSession={noop}
          onDeleteSession={noop}
          onLoadMoreSessions={noop}
          onManageCronJob={noop}
          onNavigate={onNavigate as never}
          onNewSessionInWorkspace={noop}
          onNewSessionSplit={noop}
          onResumeSession={noop}
          onRetrySessions={noopAsync}
          onTriggerCronJob={noopAsync}
        />
      </SidebarProvider>
    </MemoryRouter>
  )

// The nav row, not a section's own `+` (which carries the same label).
const newSessionRow = () => {
  const row = screen.getByText('New session', { selector: '[data-tour="sidebar-nav-new-session"]' }).closest('button')

  if (!row) {
    throw new Error('New session row not rendered')
  }

  return row
}

describe('ChatSidebar New session under All profiles', () => {
  beforeEach(() => {
    profileStore.newSessionInAgent.mockClear()
    profileStore.newSessionInProfile.mockClear()
    $connectionsRegistry.set({
      version: 2,
      primary: 'local',
      secureTokenStorage: true,
      connections: [{ id: 'local', label: 'This computer', kind: 'local', tokenSet: false, tokenPreview: null }]
    } as NonNullable<typeof $connectionsRegistry.value>)
    $profiles.set([
      { name: 'default', is_default: true },
      { name: 'work', is_default: false },
      { name: 'quiet', is_default: false }
    ] as typeof $profiles.value)
    $sessions.set([
      makeSessionInfo({ id: 'd1', last_active: 3, profile: 'default', started_at: 1, title: 'Default chat' }),
      makeSessionInfo({ id: 'w1', last_active: 2, profile: 'work', started_at: 1, title: 'Work chat' })
    ])
    setSidebarGrouping('profile')
  })

  afterEach(() => {
    cleanup()
    setSidebarGrouping('date')
    setShowAllProfiles(false)
    $sessions.set([])
  })

  it('asks which profile instead of starting in the live gateway profile', () => {
    setShowAllProfiles(true)
    const onNavigate = vi.fn()

    renderSidebar(onNavigate)
    fireEvent.click(newSessionRow())

    const menu = screen.getByRole('menu', { name: 'New session' })
    const items = within(menu).getAllByRole('menuitem')

    // Every profile is offered, including one with no chats yet.
    expect(items.map(item => item.querySelector('span.truncate')?.textContent)).toEqual(['default', 'quiet', 'work'])
    expect(onNavigate).not.toHaveBeenCalled()
    expect(profileStore.newSessionInProfile).not.toHaveBeenCalled()
    expect(newSessionRow().getAttribute('aria-expanded')).toBe('true')
  })

  it('starts the draft in the picked profile and stays on All profiles', () => {
    setShowAllProfiles(true)
    const onNavigate = vi.fn()

    const { container } = renderSidebar(onNavigate)

    fireEvent.click(newSessionRow())
    fireEvent.click(within(screen.getByRole('menu')).getByText('work'))

    expect(profileStore.newSessionInProfile).toHaveBeenCalledTimes(1)
    expect(profileStore.newSessionInProfile).toHaveBeenCalledWith('work')
    expect(profileStore.newSessionInAgent).not.toHaveBeenCalled()
    expect(onNavigate).not.toHaveBeenCalled()
    expect(screen.queryByRole('menu')).toBeNull()
    // Picking never collapses the browse view.
    expect(container.querySelector('[data-gateway-group]')).not.toBeNull()
  })

  it('starts nothing when the menu is dismissed', () => {
    setShowAllProfiles(true)
    const onNavigate = vi.fn()

    renderSidebar(onNavigate)
    fireEvent.click(newSessionRow())
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })

    expect(screen.queryByRole('menu')).toBeNull()
    expect(onNavigate).not.toHaveBeenCalled()
    expect(profileStore.newSessionInProfile).not.toHaveBeenCalled()
    expect(profileStore.newSessionInAgent).not.toHaveBeenCalled()
  })

  it('routes a gateway-owned profile to its exact agent', () => {
    setShowAllProfiles(true)
    $sessions.set([
      makeSessionInfo({
        connection_id: 'homelab',
        id: 'h1',
        last_active: 4,
        profile: 'ops',
        started_at: 1,
        title: 'Homelab chat'
      })
    ])
    $connectionsRegistry.set({
      version: 2,
      primary: 'local',
      secureTokenStorage: true,
      connections: [
        { id: 'local', label: 'This computer', kind: 'local', tokenSet: false, tokenPreview: null },
        { id: 'homelab', label: 'Homelab', kind: 'remote', tokenSet: true, tokenPreview: null }
      ]
    } as NonNullable<typeof $connectionsRegistry.value>)

    renderSidebar()
    fireEvent.click(newSessionRow())
    fireEvent.click(within(screen.getByRole('menu')).getByText('Homelab · ops'))

    expect(profileStore.newSessionInAgent).toHaveBeenCalledWith({ connectionId: 'homelab', profile: 'ops' })
    expect(profileStore.newSessionInProfile).not.toHaveBeenCalled()
  })

  it('keeps the plain new session when a single profile is in scope', () => {
    setShowAllProfiles(false)
    const onNavigate = vi.fn()

    renderSidebar(onNavigate)
    fireEvent.click(newSessionRow())

    expect(screen.queryByRole('menu')).toBeNull()
    expect(onNavigate).toHaveBeenCalledTimes(1)
    expect(onNavigate.mock.calls[0]?.[0]).toMatchObject({ id: 'new-session', action: 'new-session' })
    expect(newSessionRow().getAttribute('aria-haspopup')).toBeNull()
  })

  it('closes the menu when the scope leaves All profiles', () => {
    setShowAllProfiles(true)

    renderSidebar()
    fireEvent.click(newSessionRow())
    expect(screen.getByRole('menu')).toBeTruthy()

    act(() => setShowAllProfiles(false))

    expect(screen.queryByRole('menu')).toBeNull()
    expect(newSessionRow().getAttribute('aria-expanded')).toBeNull()
  })
})

describe('newSessionTargets', () => {
  const group = (over: Partial<SidebarSessionGroup>): SidebarSessionGroup => ({
    id: JSON.stringify([null, 'work']),
    label: 'work',
    path: null,
    sessions: [],
    profile: 'work',
    connectionId: null,
    color: '#123456',
    mode: 'profile',
    ...over
  })

  it('maps owner groups to exact targets and skips groups without a profile', () => {
    const targets = newSessionTargets([
      group({}),
      group({ id: 'x', profile: undefined, mode: 'workspace' }),
      group({ id: JSON.stringify(['homelab', 'ops']), label: 'Homelab · ops', profile: 'ops', connectionId: 'homelab' })
    ])

    expect(targets).toEqual([
      { color: '#123456', connectionId: null, id: JSON.stringify([null, 'work']), label: 'work', profile: 'work' },
      {
        color: '#123456',
        connectionId: 'homelab',
        id: JSON.stringify(['homelab', 'ops']),
        label: 'Homelab · ops',
        profile: 'ops'
      }
    ])
  })

  it('falls back to the gateway profile list when no sections exist', () => {
    const targets = fallbackNewSessionTargets([{ name: 'default' }, { name: 'work' }], {})

    expect(targets.map(target => [target.profile, target.connectionId])).toEqual([
      ['default', null],
      ['work', null]
    ])
  })
})
