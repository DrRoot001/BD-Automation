'use client'

import { useQuery } from '@tanstack/react-query'
import { api, type ConversionFunnel as FunnelData } from '@/lib/api'
import { clsx } from 'clsx'

interface FunnelStage {
  label: string
  key: keyof FunnelData
  color: string
  rateKey?: keyof FunnelData
  rateLabel?: string
}

const STAGES: FunnelStage[] = [
  { label: 'Applied',   key: 'total_applied',   color: '#111110' },
  { label: 'Confirmed', key: 'total_confirmed',  color: '#44443c' },
  { label: 'Round 1',   key: 'total_r1',         color: '#1d4ed8', rateKey: 'apply_to_r1_rate', rateLabel: 'Apply → R1' },
  { label: 'Round 2',   key: 'total_r2',         color: '#7c3aed', rateKey: 'r1_to_r2_rate',    rateLabel: 'R1 → R2'   },
  { label: 'Offers',    key: 'total_offers',     color: '#16a34a', rateKey: 'r2_to_offer_rate', rateLabel: 'R2 → Offer' },
]

function FunnelBar({ label, count, max, color, rate, rateLabel }: {
  label: string; count: number; max: number; color: string; rate?: number; rateLabel?: string
}) {
  const pct = max > 0 ? Math.max(3, (count / max) * 100) : 3
  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <span className="text-xs text-text-secondary">{label}</span>
        <div className="flex items-center gap-3">
          {rate != null && rateLabel && (
            <span className="text-xs text-text-muted">
              <span style={{ color }}>{rate.toFixed(1)}%</span>{' '}
              <span className="text-text-muted">{rateLabel}</span>
            </span>
          )}
          <span className="text-xs font-semibold tabular-nums" style={{ color }}>
            {count.toLocaleString()}
          </span>
        </div>
      </div>
      <div className="h-6 rounded overflow-hidden bg-bg-secondary border border-bg-border/60">
        <div
          className="h-full rounded transition-all duration-500 ease-out"
          style={{ width: `${pct}%`, backgroundColor: color, opacity: 0.7 }}
        />
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
    <div className="card">
      <div className="card-header">
        <h2 className="card-title">Conversion Funnel</h2>
        {funnel && (
          <span className="text-xs text-danger">
            {((funnel.total_rejected / Math.max(funnel.total_applied, 1)) * 100).toFixed(1)}% rejected
          </span>
        )}
      </div>

      <div className="p-4">
        {isLoading ? (
          <div className="space-y-3">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i}>
                <div className="skeleton h-3 w-16 rounded mb-1" />
                <div className="skeleton h-6 rounded" />
              </div>
            ))}
          </div>
        ) : isError || !funnel ? (
          <div className="py-8 text-center text-danger text-sm">Failed to load analytics</div>
        ) : (
          <div className="space-y-3">
            {STAGES.map((stage) => (
              <FunnelBar
                key={stage.key}
                label={stage.label}
                count={(funnel[stage.key] as number) ?? 0}
                max={max}
                color={stage.color}
                rate={stage.rateKey ? (funnel[stage.rateKey] as number) : undefined}
                rateLabel={stage.rateLabel}
              />
            ))}

            <div className="mt-4 pt-3 border-t border-bg-border grid grid-cols-3 gap-2">
              {[
                { label: 'Apply → R1', actual: funnel.apply_to_r1_rate, target: 12 },
                { label: 'R1 → R2',   actual: funnel.r1_to_r2_rate,    target: 41 },
                { label: 'R2 → Offer', actual: funnel.r2_to_offer_rate, target: 20 },
              ].map(({ label, actual, target }) => (
                <div key={label} className="rounded bg-bg-secondary border border-bg-border p-2.5 text-center">
                  <div className="text-xs text-text-muted mb-1">{label}</div>
                  <div className={clsx('text-sm font-semibold tabular-nums', actual >= target ? 'text-success' : 'text-warning')}>
                    {actual.toFixed(1)}%
                  </div>
                  <div className="text-xs text-text-muted">target {target}%</div>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
