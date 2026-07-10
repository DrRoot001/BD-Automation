'use client'

import { ConversionFunnel } from '@/components/dashboard/ConversionFunnel'
import { PlatformStatsChart } from '@/components/dashboard/PlatformStatsChart'

export default function AdminAnalyticsPage() {
  return (
    <div className="space-y-6 max-w-7xl mx-auto animate-fade-in">
      {/* Header */}
      <div className="border-b border-bg-border pb-6">
        <h1 className="page-title">Analytics</h1>
        <p className="page-subtitle">
          Global conversion funnel and per-platform performance across all candidates.
        </p>
      </div>

      {/* Charts */}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        <ConversionFunnel />
        <PlatformStatsChart />
      </div>
    </div>
  )
}
