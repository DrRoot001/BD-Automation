// Bridge to the BD co-pilot Chrome extension (bd-indeed-extension/).
// The extension whitelists this app's origin via externally_connectable, so
// we can chrome.runtime.sendMessage(EXTENSION_ID, …) straight from the page.
// ID is pinned by the "key" in the extension's manifest.json.

const EXTENSION_ID =
  process.env.NEXT_PUBLIC_EXTENSION_ID || 'mcjhnaipickgldpcaobhlgfgdnopiiad'

export interface ManualApplyPayload {
  candidateId: string
  jobUrl: string
  applicationId?: string | null
  jobId?: string | null
  resumeId?: string | null
  title?: string | null
  company?: string | null
}

type ExtResponse = Record<string, unknown> | undefined

function sendToExtension(msg: object, timeoutMs = 600): Promise<ExtResponse> {
  return new Promise((resolve) => {
    const chrome = (window as any).chrome
    if (!chrome?.runtime?.sendMessage) return resolve(undefined) // not Chrome / no extension support
    const timer = setTimeout(() => resolve(undefined), timeoutMs)
    try {
      chrome.runtime.sendMessage(EXTENSION_ID, msg, (resp: ExtResponse) => {
        clearTimeout(timer)
        // lastError = extension not installed / not listening
        resolve(chrome.runtime.lastError ? undefined : resp)
      })
    } catch {
      clearTimeout(timer)
      resolve(undefined)
    }
  })
}

/** Read the auth_token cookie set by the Next.js middleware. */
function getAuthTokenFromCookie(): string | undefined {
  if (typeof document === 'undefined') return undefined
  return document.cookie
    .split(';')
    .map((c) => c.trim())
    .find((c) => c.startsWith('auth_token='))
    ?.split('=')[1]
}

export async function pingExtension(): Promise<{ installed: boolean; authed: boolean }> {
  const resp = await sendToExtension({ type: 'PING' })
  return { installed: !!resp?.installed, authed: !!resp?.authed }
}

/**
 * Hand one job to the extension to open + pre-fill (fill-only: the user
 * reviews and submits). Returns a user-facing outcome:
 *  - 'filling'       extension took the job; it opens its own tab
 *  - 'not_installed' extension absent → caller should open the job page itself
 *  - 'error'         extension present but refused (run active…)
 */
export async function manualApplyViaExtension(
  payload: ManualApplyPayload,
): Promise<{ outcome: 'filling' | 'not_installed' | 'error'; detail?: string }> {
  const { installed } = await pingExtension()
  if (!installed) return { outcome: 'not_installed' }
  // Pass the app's session token so the extension doesn't need a prior popup login.
  const token = getAuthTokenFromCookie()
  const resp = await sendToExtension({ type: 'MANUAL_APPLY', ...payload, token }, 15_000)
  if (resp && (resp as any).ok) return { outcome: 'filling' }
  return {
    outcome: 'error',
    detail:
      ((resp as any)?.detail as string) ||
      ((resp as any)?.error as string) ||
      'Extension did not respond',
  }
}

/**
 * Shared "Apply Manually" click flow. Tries the co-pilot extension first
 * (fill-only handoff); when it's absent or refuses, opens the job page
 * directly so the user is never blocked. `notify` is the app's useToast().
 */
export async function applyManuallyClick(
  payload: ManualApplyPayload,
  fallbackUrl: string,
  notify: { success: (m: string) => void; info: (m: string) => void; warning: (m: string) => void },
): Promise<void> {
  if (!payload.candidateId || !payload.jobUrl) {
    window.open(fallbackUrl, '_blank', 'noopener,noreferrer')
    return
  }
  const res = await manualApplyViaExtension(payload)
  if (res.outcome === 'filling') {
    notify.success('Co-pilot extension is opening the job and pre-filling it — review and submit there.')
    return
  }
  window.open(fallbackUrl, '_blank', 'noopener,noreferrer')
  if (res.outcome === 'not_installed') {
    notify.info('Tip: install the BD co-pilot extension (chrome://extensions → Load unpacked → bd-indeed-extension) to auto-fill this form next time.')
  } else {
    notify.warning(`Co-pilot couldn't take the job (${res.detail}) — opened the page for a fully manual apply.`)
  }
}
