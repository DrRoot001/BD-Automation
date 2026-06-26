import { type ClassValue, clsx } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function formatSalary(min: number | null, max: number | null, period: string | null): string {
  if (min == null && max == null) {
    return "Salary not listed"
  }

  const formatCurrency = (val: number) => {
    if (val >= 1000 && (period === 'yearly' || period === 'yr')) {
      return `$${Math.round(val / 1000)}k`
    }
    return `$${val}`
  }

  const periodSuffix = period === 'hourly' ? '/hr' : period === 'yearly' ? '/yr' : ''

  if (min != null && max != null && min !== max) {
    return `${formatCurrency(min)} - ${formatCurrency(max)}${periodSuffix}`
  }

  const val = min ?? max
  if (val != null) {
    return `${formatCurrency(val)}${periodSuffix}`
  }

  return "Salary not listed"
}

export function formatDistanceToNow(dateInput: string | Date | null | undefined): string {
  if (!dateInput) return ''
  const date = new Date(dateInput)
  const now = new Date()
  const diffInSeconds = Math.floor((now.getTime() - date.getTime()) / 1000)
  
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
