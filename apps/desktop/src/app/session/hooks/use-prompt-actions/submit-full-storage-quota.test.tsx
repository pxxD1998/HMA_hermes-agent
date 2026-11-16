import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { en } from '@/i18n/en'
import { createClientSessionState } from '@/lib/chat-runtime'
import type { ComposerAttachment } from '@/store/composer'
import { $activeGatewayProfile } from '@/store/profile'
import { $connection, $sessions } from '@/store/session'
import { $sessionStates } from '@/store/session-states'

import { useSubmitPrompt } from './submit'
import type { GatewayRequest } from './utils'

vi.mock('@/store/gateway', async original => ({
  ...(await original<Record<string, unknown>>()),
  retainGatewayForSessionTurn: vi.fn(async () => () => undefined)
}))

beforeEach(() => {
  vi.stubGlobal('hermesDesktop', undefined)
  window.localStorage.clear()
  $sessions.set([])
  $sessionStates.set({})
  $connection.set(null)
  $activeGatewayProfile.set('default')
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

type ClientSessionState = ReturnType<typeof createClientSessionState>

// The browser journal is the only copy of a send's retry identity, and a phone reaches the
// origin's localStorage quota with an ordinary image draft: every chip carries a bounded data
// URL and the crash-recovery journals share the same budget. A journal write the origin refuses
// must not stop the prompt the user asked for — the entry stays in this window's memory instead.
it('a full origin quota does not stop an image send from reaching prompt.submit', async () => {
  let state: ClientSessionState = createClientSessionState()
  const submitted: Record<string, unknown>[] = []

  const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
    if (method === 'prompt.submit') {
      submitted.push(params ?? {})

      return { session_id: 'runtime-a', status: 'started', submission_id: params?.submission_id } as never
    }

    return {} as never
  })

  const attachment: ComposerAttachment = {
    id: '/cache/a.png',
    kind: 'image',
    label: 'a.png',
    occurrenceId: 'occ-1',
    path: '/cache/a.png'
  }
  const deps = submitDeps(requestGateway as GatewayRequest, updater => (state = updater(state)))
  const { result } = renderHook(() => useSubmitPrompt(deps as never))

  const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
  const keys: string[] = []
  const passThrough = window.localStorage.setItem.bind(window.localStorage)
  const setItem = vi.spyOn(window.localStorage, 'setItem').mockImplementation((key: string, value: string) => {
    keys.push(key)

    if (key.startsWith('hermes.desktop.preparedSubmissions.v2:')) {
      throw new DOMException(
        "Failed to execute 'setItem' on 'Storage': Setting the value exceeded the quota.",
        'QuotaExceededError'
      )
    }

    passThrough(key, value)
  })

  let refusedJournalWrite = false
  let warned = false

  try {
    await act(async () => {
      expect(await result.current('send this chart', { attachments: [attachment] })).toBe(true)
    })
    refusedJournalWrite = keys.some(key => key.startsWith('hermes.desktop.preparedSubmissions.v2:'))
    warned = warn.mock.calls.some(([tag]) => tag === '[prepared-submission-unstored]')
  } finally {
    setItem.mockRestore()
    warn.mockRestore()
  }

  // The journal write really was attempted and refused, and the prompt still reached the owner:
  // the refused write cost the reload, not the turn.
  expect(refusedJournalWrite).toBe(true)
  expect(warned).toBe(true)
  expect(submitted).toHaveLength(1)
  expect(submitted[0]).toMatchObject({ text: expect.stringContaining('send this chart') })
  expect(state.messages.some(message => message.error)).toBe(false)
})

function submitDeps(
  requestGateway: GatewayRequest,
  update: (updater: (s: ClientSessionState) => ClientSessionState) => ClientSessionState
) {
  return {
    activeSessionIdRef: { current: 'runtime-a' as string | null },
    busyRef: { current: false },
    copy: en.desktop,
    createBackendSessionForSend: vi.fn(async () => null),
    getRoutedStoredSessionId: () => null,
    getRouteToken: () => 'same-route',
    getRuntimeIdForStoredSession: () => 'runtime-a',
    requestGateway,
    resumeStoredSession: vi.fn(),
    runtimeIdByStoredSessionIdRef: { current: new Map([['stored-a', 'runtime-a']]) },
    selectedStoredSessionIdRef: { current: 'stored-a' as string | null },
    syncAttachmentsForSubmit: vi.fn(async (sessionId: string, attachments: ComposerAttachment[]) => ({
      attachments,
      sessionId
    })),
    updateSessionState: vi.fn((_sid: string, updater: (s: ClientSessionState) => ClientSessionState) => update(updater))
  }
}
