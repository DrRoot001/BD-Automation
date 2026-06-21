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
    <div className="bg-bg-card border border-bg-border rounded-md p-3 text-sm" style={{ boxShadow: '0 2px 8px rgba(0,0,0,0.1)' }}>
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
    <div className="card">
      <div className="card-header">
        <div>
          <h2 className="card-title">By Platform</h2>
        </div>
        <p className="text-xs text-text-muted">interviews vs applications</p>
      </div>

      <div className="p-4">
        {isLoading ? (
          <div className="skeleton h-48 rounded" />
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
                stroke="#e4e4dc"
                vertical={false}
              />
              <XAxis
                dataKey="platform"
                tick={{ fill: '#8a8a7a', fontSize: 11 }}
                axisLine={false}
                tickLine={false}
              />
              <YAxis
                tick={{ fill: '#8a8a7a', fontSize: 11 }}
                axisLine={false}
                tickLine={false}
              />
              <Tooltip content={<CustomTooltip />} cursor={{ fill: 'rgba(0,0,0,0.03)' }} />
              <Legend
                formatter={(value) => (
                  <span style={{ color: '#44443c', fontSize: 11, textTransform: 'capitalize' }}>
                    {value}
                  </span>
                )}
              />
              <Bar
                dataKey="applications"
                fill="#111110"
                radius={[4, 4, 0, 0]}
                maxBarSize={40}
              />
              <Bar
                dataKey="interviews"
                fill="#8a8a7a"
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
