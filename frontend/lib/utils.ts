import { type ClassValue, clsx } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * Format salary range for display.
 */
export function formatSalary(
  min: number | null | undefined,
  max: number | null | undefined,
  period: string | null | undefined
): string {
  if (min == null && max == null) return 'Not listed'

  const fmt = (val: number) => {
    if (val >= 1_000_000) return `$${(val / 1_000_000).toFixed(1)}M`
    if (val >= 1_000) return `$${Math.round(val / 1_000)}k`
    return `$${val}`
  }

  const suffix = period === 'hourly' ? '/hr' : period === 'yearly' || period === 'yr' ? '/yr' : ''

  if (min != null && max != null && min !== max) {
    return `${fmt(min)} – ${fmt(max)}${suffix}`
  }

  const val = min ?? max
  if (val != null) return `${fmt(val)}${suffix}`

  return 'Not listed'
}

/**
 * Format a date into a human-readable relative time string.
 */
export function formatDistanceToNow(dateInput: string | Date | null | undefined): string {
  if (!dateInput) return ''
  const date = new Date(dateInput)
  if (isNaN(date.getTime())) return ''
  const now = new Date()
  const diffInSeconds = Math.floor((now.getTime() - date.getTime()) / 1000)

  if (diffInSeconds < 0) return 'just now'
  if (diffInSeconds < 60) return `${diffInSeconds}s`
  const diffInMinutes = Math.floor(diffInSeconds / 60)
  if (diffInMinutes < 60) return `${diffInMinutes}m`
  const diffInHours = Math.floor(diffInMinutes / 60)
  if (diffInHours < 24) return `${diffInHours}h`
  const diffInDays = Math.floor(diffInHours / 24)
  if (diffInDays < 30) return `${diffInDays}d`
  const diffInMonths = Math.floor(diffInDays / 30)
  if (diffInMonths < 12) return `${diffInMonths}mo`
  return `${Math.floor(diffInMonths / 12)}y`
}

/**
 * Format a scheduled date/time (e.g. "Mon, Oct 24, 2:30 PM").
 */
export function formatScheduled(scheduledAt: string | Date | null | undefined): string {
  if (!scheduledAt) return 'TBD'
  const date = new Date(scheduledAt)
  if (isNaN(date.getTime())) return 'TBD'
  return date.toLocaleDateString('en-US', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/**
 * Format a date for display in tables and cards.
 */
export function formatDate(dateInput: string | Date | null | undefined): string {
  if (!dateInput) return '—'
  const date = new Date(dateInput)
  if (isNaN(date.getTime())) return '—'
  return date.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  })
}

/**
 * Capitalize the first letter of a string.
 */
export function capitalize(str: string): string {
  if (!str) return ''
  return str.charAt(0).toUpperCase() + str.slice(1).toLowerCase()
}
