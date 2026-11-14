// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'

import { useImageDownload } from './use-image-download'

const { notify, notifyError } = vi.hoisted(() => ({ notify: vi.fn(), notifyError: vi.fn() }))

vi.mock('@/store/notifications', () => ({ notify, notifyError }))

const BLOB_SRC = 'blob:http://127.0.0.1:9119/2f0c1f4e-image'
const HTTP_SRC = 'https://images.example/product.png'

let saveImageFromUrl: ReturnType<typeof vi.fn>
let savedDownloadNames: string[]

function renderDownload(src: string) {
  return renderHook(() => useImageDownload(src), { wrapper: I18nProvider })
}

async function runDownload(result: { current: { download: () => Promise<void> } }) {
  await act(async () => {
    await result.current.download()
  })
}

beforeEach(() => {
  saveImageFromUrl = vi.fn(async () => true)
  ;(window as unknown as { hermesDesktop: object }).hermesDesktop = { saveImageFromUrl }
  savedDownloadNames = []
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
    this: HTMLAnchorElement
  ) {
    savedDownloadNames.push(this.download)
  })
  vi.stubGlobal('URL', {
    ...URL,
    createObjectURL: vi.fn(() => 'blob:renderer-download'),
    revokeObjectURL: vi.fn()
  })
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(new Blob([new Uint8Array([137, 80, 78, 71])], { type: 'image/png' })))
  )
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  delete (window as unknown as { hermesDesktop?: object }).hermesDesktop
  notify.mockReset()
  notifyError.mockReset()
})

describe('saving an image whose URL only exists inside the renderer', () => {
  it('reads a blob: source through the download manager instead of handing the protocol to the main process', async () => {
    const { result } = renderDownload(BLOB_SRC)

    await runDownload(result)

    // The main process fetches the URL itself and accepts http/https/data/file
    // only, so a blob: source must never be routed there.
    expect(saveImageFromUrl).not.toHaveBeenCalled()
    expect(fetch).toHaveBeenCalledWith(BLOB_SRC)
    expect(savedDownloadNames).toEqual(['2f0c1f4e-image.png'])
    expect(notifyError).not.toHaveBeenCalled()
  })

  it('keeps an http source on the desktop bridge, which owns the save dialog', async () => {
    const { result } = renderDownload(HTTP_SRC)

    await runDownload(result)

    expect(saveImageFromUrl).toHaveBeenCalledWith(HTTP_SRC)
    expect(fetch).not.toHaveBeenCalled()
    expect(savedDownloadNames).toEqual([])
  })
})
