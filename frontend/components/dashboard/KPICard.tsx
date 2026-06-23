'use client'

import { type ReactNode } from 'react'

interface KPICardProps {
  label: string
  value: string | number
  subtext?: string
  icon: ReactNode
  accent?: 'accent' | 'success' | 'warning' | 'danger' | 'purple' | 'info'
  loading?: boolean
}

const accentColor: Record<string, string> = {
  accent:  '#5865f2',
  success: '#22c55e',
  warning: '#eab308',
  danger:  '#ef4444',
  purple:  '#a855f7',
  info:    '#3b82f6',
}

export function KPICard({ label, value, subtext, icon, accent = 'accent', loading }: KPICardProps) {
  const color = accentColor[accent]

  if (loading) {
    return (
      <div className="card p-4">
        <div className="skeleton h-4 w-4 rounded mb-3" />
        <div className="skeleton h-7 w-16 rounded mb-1.5" />
        <div className="skeleton h-3 w-24 rounded" />
      </div>
    )
  }

  return (
    <div className="card p-4">
      <div className="mb-3" style={{ color }}>
        {icon}
      </div>
      <div className="text-2xl font-semibold text-text-primary tabular-nums leading-none mb-1">
        {typeof value === 'number' && !isNaN(value) ? value.toLocaleString() : value}
      </div>
      <div className="text-xs font-medium text-text-secondary">{label}</div>
      {subtext && <div className="text-xs text-text-muted mt-0.5">{subtext}</div>}
    </div>
  )
}
