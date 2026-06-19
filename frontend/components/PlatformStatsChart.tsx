'use client'

import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  Legend,
} from 'recharts'

const CustomTooltip = ({
  active,
  payload,
  label,
}: {
  active?: boolean
  payload?: { value: number; name: string; color: string }[]
  label?: string
}) => {
  if (!active || !payload?.length) return null
  return (
    <div className="bg-bg-card border border-bg-border rounded-xl p-3 shadow-card text-sm">
      <p className="text-text-secondary font-medium mb-2 capitalize">{label}</p>
      {payload.map((p) => (
        <div key={p.name} className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full" style={{ background: p.color }} />
          <span className="text-text-muted capitalize">{p.name}:</span>
          <span className="text-text-primary font-semibold">{p.value}</span>
        </div>
      ))}
    </div>
  )
}

export function PlatformStatsChart() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['analytics'],
    queryFn: () => api.getAnalytics(),
    refetchInterval: 5 * 60_000,
  })

  const platformData = (data?.per_platform_stats ?? []).map((p) => ({
    ...p,
    platform: p.platform.charAt(0).toUpperCase() + p.platform.slice(1),
  }))

  return (
    <div className="card animate-slide-up">
      <div className="px-5 py-4 border-b border-bg-border">
        <h2 className="font-semibold text-text-primary">Applications by Platform</h2>
        <p className="text-xs text-text-muted mt-0.5">
          Interview vs application ratio per job board
        </p>
      </div>

      <div className="p-5">
        {isLoading ? (
          <div className="skeleton h-48 rounded-lg" />
        ) : isError || platformData.length === 0 ? (
          <div className="h-48 flex items-center justify-center">
            <div className="text-center text-text-muted">
              <div className="text-3xl mb-2">📊</div>
              <div className="text-sm">No platform data yet</div>
            </div>
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={platformData} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
              <CartesianGrid
                strokeDasharray="3 3"
                stroke="#252535"
                vertical={false}
              />
              <XAxis
                dataKey="platform"
                tick={{ fill: '#64748b', fontSize: 11 }}
                axisLine={false}
                tickLine={false}
              />
              <YAxis
                tick={{ fill: '#64748b', fontSize: 11 }}
                axisLine={false}
                tickLine={false}
              />
              <Tooltip content={<CustomTooltip />} cursor={{ fill: 'rgba(99,102,241,0.06)' }} />
              <Legend
                formatter={(value) => (
                  <span style={{ color: '#94a3b8', fontSize: 11, textTransform: 'capitalize' }}>
                    {value}
                  </span>
                )}
              />
              <Bar
                dataKey="applications"
                fill="#6366f1"
                radius={[4, 4, 0, 0]}
                maxBarSize={40}
              />
              <Bar
                dataKey="interviews"
                fill="#a855f7"
                radius={[4, 4, 0, 0]}
                maxBarSize={40}
              />
            </BarChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  )
}
