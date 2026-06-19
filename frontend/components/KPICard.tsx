'use client'

import { type ReactNode } from 'react'
import { clsx } from 'clsx'

interface KPICardProps {
  label: string
  value: string | number
  subtext?: string
  icon: ReactNode
  accent?: 'accent' | 'success' | 'warning' | 'danger' | 'purple' | 'info'
  loading?: boolean
}

const accentMap = {
  accent:  { icon: 'text-accent  bg-accent/10  border-accent/20',  value: 'text-gradient-accent'  },
  success: { icon: 'text-success bg-success/10 border-success/20', value: 'text-gradient-success' },
  warning: { icon: 'text-warning bg-warning/10 border-warning/20', value: 'text-warning'           },
  danger:  { icon: 'text-danger  bg-danger/10  border-danger/20',  value: 'text-danger'            },
  purple:  { icon: 'text-purple  bg-purple/10  border-purple/20',  value: 'text-purple'            },
  info:    { icon: 'text-info    bg-info/10    border-info/20',    value: 'text-info'              },
}

export function KPICard({ label, value, subtext, icon, accent = 'accent', loading }: KPICardProps) {
  const colors = accentMap[accent]

  if (loading) {
    return (
      <div className="card p-5 animate-fade-in">
        <div className="flex items-start justify-between">
          <div className="skeleton h-10 w-10 rounded-lg" />
          <div className="skeleton h-4 w-16 rounded" />
        </div>
        <div className="mt-4 skeleton h-9 w-24 rounded" />
        <div className="mt-2 skeleton h-3 w-32 rounded" />
      </div>
    )
  }

  return (
    <div className="card card-glow p-5 animate-fade-in group">
      <div className="flex items-start justify-between">
        {/* Icon */}
        <div
          className={clsx(
            'w-11 h-11 rounded-xl flex items-center justify-center border text-xl',
            colors.icon,
          )}
        >
          {icon}
        </div>
      </div>

      {/* Value */}
      <div className={clsx('mt-4 text-4xl font-bold tabular-nums', colors.value)}>
        {typeof value === 'number' && !isNaN(value)
          ? value.toLocaleString()
          : value}
      </div>

      {/* Label */}
      <div className="mt-1 text-sm font-medium text-text-secondary">{label}</div>

      {/* Subtext */}
      {subtext && (
        <div className="mt-1 text-xs text-text-muted">{subtext}</div>
      )}
    </div>
  )
}
