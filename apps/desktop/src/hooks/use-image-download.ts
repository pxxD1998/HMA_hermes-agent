import { useCallback, useState } from 'react'

import { useI18n } from '@/i18n'
import { downloadBlob, downloadFilename, imageFilename } from '@/lib/download'
import { notify, notifyError } from '@/store/notifications'

function isMissingIpcHandler(error: unknown): boolean {
  const message = error instanceof Error ? error.message : typeof error === 'string' ? error : ''

  return message.includes("No handler registered for 'hermes:saveImageFromUrl'")
}

async function startBrowserDownload(src: string) {
  const response = await fetch(src)

  if (!response.ok) {
    throw new Error(`Could not fetch image: ${response.status}`)
  }

  const blob = await response.blob()
  downloadBlob(blob, downloadFilename(src, blob.type))
}

/** A blob: URL exists only inside this renderer. `hermes:saveImageFromUrl`
 *  fetches the URL in the main process, which accepts http/https/data/file, so
 *  a blob: source dies there with ERR_INVALID_PROTOCOL. Chromium can read it
 *  directly, and will-download routes that save through the Downloads dialog. */
function isRendererBlobSource(src: string): boolean {
  return /^blob:/i.test(src)
}

/** Save an image to disk via the desktop IPC bridge, falling back to a browser
 *  download when the handler is unavailable (older shell / web preview). */
export function useImageDownload(src?: string) {
  const { t } = useI18n()
  const copy = t.desktop
  const [saving, setSaving] = useState(false)

  const download = useCallback(async () => {
    if (!src || saving) {
      return
    }

    setSaving(true)

    try {
      // The bridge would fetch a blob: itself in the main process and fail, so
      // those bytes go to Chromium's download manager instead.
      if (isRendererBlobSource(src)) {
        await startBrowserDownload(src)

        return
      }

      if (window.hermesDesktop?.saveImageFromUrl) {
        if (await window.hermesDesktop.saveImageFromUrl(src)) {
          notify({ kind: 'success', title: copy.imageSaved, message: imageFilename(src) })
        }

        return
      }

      await startBrowserDownload(src)
    } catch (error) {
      if (isMissingIpcHandler(error)) {
        try {
          await startBrowserDownload(src)
          notify({ kind: 'info', title: copy.downloadStarted, message: copy.restartToUseSaveImage })
        } catch (fallbackError) {
          notifyError(fallbackError, copy.restartToSaveImages)
        }

        return
      }

      notifyError(error, copy.imageDownloadFailed)
    } finally {
      setSaving(false)
    }
  }, [copy, saving, src])

  return { download, saving }
}
