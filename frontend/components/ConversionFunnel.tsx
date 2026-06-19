'use client'

import { useQuery } from '@tanstack/react-query'
import { api, type ConversionFunnel as FunnelData } from '@/lib/api'
import { clsx } from 'clsx'

interface FunnelStage {
  label: string
  key: keyof FunnelData
  color: string
  bgColor: string
  rateKey?: keyof FunnelData
  rateLabel?: string
}

const STAGES: FunnelStage[] = [
  {
    label: 'Applied',
    key: 'total_applied',
    color: '#6366f1',
    bgColor: 'rgba(99,102,241,0.15)',
  },
  {
    label: 'Confirmed',
    key: 'total_confirmed',
    color: '#3b82f6',
    bgColor: 'rgba(59,130,246,0.15)',
  },
  {
    label: 'Round 1',
    key: 'total_r1',
    color: '#a855f7',
    bgColor: 'rgba(168,85,247,0.15)',
    rateKey: 'apply_to_r1_rate',
    rateLabel: 'Apply → R1',
  },
  {
    label: 'Round 2',
    key: 'total_r2',
    color: '#ec4899',
    bgColor: 'rgba(236,72,153,0.15)',
    rateKey: 'r1_to_r2_rate',
    rateLabel: 'R1 → R2',
  },
  {
    label: 'Offers',
    key: 'total_offers',
    color: '#10b981',
    bgColor: 'rgba(16,185,129,0.15)',
    rateKey: 'r2_to_offer_rate',
    rateLabel: 'R2 → Offer',
  },
]

function FunnelBar({
  label,
  count,
  max,
  color,
  bgColor,
  rate,
  rateLabel,
  delay,
}: {
  label: string
  count: number
  max: number
  color: string
  bgColor: string
  rate?: number
  rateLabel?: string
  delay: number
}) {
  const pct = max > 0 ? Math.max(4, (count / max) * 100) : 4

  return (
    <div className="group">
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-sm font-medium text-text-secondary">{label}</span>
        <div className="flex items-center gap-3">
          {rate != null && rateLabel && (
            <span className="text-xs text-text-muted">
              <span style={{ color }}>{rate.toFixed(1)}%</span>{' '}
              <span className="text-text-muted">{rateLabel}</span>
            </span>
          )}
          <span className="text-sm font-bold tabular-nums" style={{ color }}>
            {count.toLocaleString()}
          </span>
        </div>
      </div>
      <div className="h-9 rounded-lg overflow-hidden bg-bg-secondary border border-bg-border">
        <div
          className="h-full rounded-lg transition-all duration-700 ease-out flex items-center px-3"
          style={{
            width: `${pct}%`,
            background: `linear-gradient(90deg, ${color}33 0%, ${color} 100%)`,
            transitionDelay: `${delay}ms`,
          }}
        >
          {pct > 15 && (
            <span className="text-xs font-medium text-white/80 whitespace-nowrap">
              {label}
            </span>
          )}
        </div>
      </div>
    </div>
  )
}

export function ConversionFunnel() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['analytics'],
    queryFn: () => api.getAnalytics(),
    refetchInterval: 5 * 60_000,
  })

  const funnel = data?.conversion_funnel
  const max = funnel?.total_applied ?? 1

  return (
    <div className="card animate-slide-up">
      <div className="flex items-center justify-between px-5 py-4 border-b border-bg-border">
        <h2 className="font-semibold text-text-primary">Conversion Funnel</h2>
        {funnel && (
          <span className="text-xs text-text-muted">
            {funnel.total_rejected.toLocaleString()} rejected ·{' '}
            <span className="text-danger">{((funnel.total_rejected / Math.max(funnel.total_applied, 1)) * 100).toFixed(1)}% rejection rate</span>
          </span>
        )}
      </div>

      <div className="p-5">
        {isLoading ? (
          <div className="space-y-4">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i}>
                <div className="skeleton h-3 w-24 rounded mb-1.5" />
                <div className="skeleton h-9 rounded-lg" />
              </div>
            ))}
          </div>
        ) : isError || !funnel ? (
          <div className="py-8 text-center text-danger text-sm">
            Failed to load analytics
          </div>
        ) : (
          <div className="space-y-3">
            {STAGES.map((stage, i) => (
              <FunnelBar
                key={stage.key}
                label={stage.label}
                count={(funnel[stage.key] as number) ?? 0}
                max={max}
                color={stage.color}
                bgColor={stage.bgColor}
                rate={stage.rateKey ? (funnel[stage.rateKey] as number) : undefined}
                rateLabel={stage.rateLabel}
                delay={i * 80}
              />
            ))}

            {/* Target lines */}
            <div className="mt-5 pt-4 border-t border-bg-border grid grid-cols-3 gap-3 text-center">
              {[
                { label: 'Apply → R1', actual: funnel.apply_to_r1_rate, target: 12 },
                { label: 'R1 → R2',   actual: funnel.r1_to_r2_rate,    target: 41 },
                { label: 'R2 → Offer', actual: funnel.r2_to_offer_rate, target: 20 },
              ].map(({ label, actual, target }) => (
                <div key={label} className="rounded-lg bg-bg-secondary border border-bg-border p-3">
                  <div className="text-xs text-text-muted mb-1">{label}</div>
                  <div
                    className={clsx(
                      'text-lg font-bold tabular-nums',
                      actual >= target ? 'text-success' : 'text-warning',
                    )}
                  >
                    {actual.toFixed(1)}%
                  </div>
                  <div className="text-xs text-text-muted">target: {target}%</div>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
