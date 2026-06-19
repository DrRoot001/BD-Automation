/**
 * Shared utility functions for components.
 * These are plain functions (not hooks) so they don't need 'use client'.
 */

/** Format an ISO date string as a relative time label, e.g. "2 hours ago" */
export function formatDistanceToNow(isoString: string | null | undefined): string {
  if (!isoString) return '—'
  const date = new Date(isoString)
  const now = Date.now()
  const diffMs = now - date.getTime()
  const diffSec = Math.floor(diffMs / 1_000)
  const diffMin = Math.floor(diffSec / 60)
  const diffHr = Math.floor(diffMin / 60)
  const diffDay = Math.floor(diffHr / 24)

  if (diffSec < 60) return 'just now'
  if (diffMin < 60) return `${diffMin}m ago`
  if (diffHr < 24) return `${diffHr}h ago`
  if (diffDay < 7) return `${diffDay}d ago`
  return date.toLocaleDateString()
}

/** Format a future or past ISO date as a calendar label */
export function formatScheduled(isoString: string | null | undefined): string {
  if (!isoString) return 'Time TBD'
  const d = new Date(isoString)
  const now = new Date()
  const diffDay = Math.floor((d.getTime() - now.getTime()) / 86_400_000)

  const timeStr = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })

  if (diffDay === 0) return `Today at ${timeStr}`
  if (diffDay === 1) return `Tomorrow at ${timeStr}`
  if (diffDay === -1) return `Yesterday at ${timeStr}`
  if (diffDay > 1 && diffDay < 7)
    return `${d.toLocaleDateString([], { weekday: 'long' })} at ${timeStr}`
  return `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} at ${timeStr}`
}
