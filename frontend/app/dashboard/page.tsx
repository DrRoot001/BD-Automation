'use client'

import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { useWebSocket } from '@/hooks/useWebSocket'
import { KPICard } from '@/components/KPICard'
import { ApplicationsQueue } from '@/components/ApplicationsQueue'
import { InterviewList } from '@/components/InterviewList'
import { ConversionFunnel } from '@/components/ConversionFunnel'
import { ActivityFeed } from '@/components/ActivityFeed'
import { PlatformStatsChart } from '@/components/PlatformStatsChart'
import CandidateProfileForm from '@/components/CandidateProfileForm'
import { useState, useCallback, useEffect } from 'react'
import { clsx } from 'clsx'
import { Send, Zap, Target, TrendingUp, Hourglass, XCircle, Award } from 'lucide-react'

export default function DashboardPage() {
  const [activeTab, setActiveTab] = useState<'dashboard' | 'profile' | 'analytics'>('dashboard')
  const [wsConnected, setWsConnected] = useState(false)

  const handleEvent = useCallback((evt: { event: string }) => {
    if (evt.event === 'connected') setWsConnected(true)
    else if (evt.event === 'disconnected') setWsConnected(false)
  }, [])

  useWebSocket(handleEvent)

  const { data: kpis, isLoading: kpisLoading } = useQuery({
    queryKey: ['kpis'],
    queryFn: () => api.getKPIs(),
    refetchInterval: 60_000,
  })

  const { data: analytics } = useQuery({
    queryKey: ['analytics'],
    queryFn: () => api.getAnalytics(),
    refetchInterval: 5 * 60_000,
  })

  const tabs = [
    { id: 'dashboard', label: 'Dashboard' },
    { id: 'profile',   label: 'Candidate Profile' },
    { id: 'analytics', label: 'Analytics' },
  ] as const

  return (
    <div className="min-h-screen bg-bg-primary">
      {/* ── Topbar ───────────────────────────────────────────────────────── */}
      <header className="sticky top-0 z-30 bg-bg-secondary border-b border-bg-border">
        <div className="max-w-screen-xl mx-auto px-6 h-12 flex items-center justify-between gap-6">
          {/* Logo */}
          <div className="shrink-0">
            <span className="text-sm font-semibold text-text-primary tracking-tight">BD Automator</span>
          </div>

          {/* Tabs */}
          <nav className="flex items-center gap-0.5">
            {tabs.map((tab) => (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={clsx(
                  'px-3 py-1.5 rounded text-sm transition-colors duration-100',
                  activeTab === tab.id
                    ? 'bg-bg-hover text-text-primary font-medium'
                    : 'text-text-muted hover:text-text-secondary hover:bg-bg-hover/60',
                )}
              >
                {tab.label}
              </button>
            ))}
          </nav>

          {/* Status */}
          <div className="flex items-center gap-2 shrink-0">
            <span
              className={clsx(
                'w-1.5 h-1.5 rounded-full',
                wsConnected ? 'bg-success' : 'bg-bg-border',
              )}
            />
            <span className="text-xs text-text-muted">
              {wsConnected ? 'Live' : 'Offline'}
            </span>
          </div>
        </div>
      </header>

      {/* ── Content ──────────────────────────────────────────────────────── */}
      {activeTab === 'profile' ? (
        <main className="max-w-screen-xl mx-auto px-6 py-8">
          <CandidateProfileForm onSuccess={() => setActiveTab('dashboard')} />
        </main>

      ) : activeTab === 'analytics' ? (
        <main className="max-w-screen-xl mx-auto px-6 py-8 space-y-5 animate-fade-in">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
            <ConversionFunnel />
            <PlatformStatsChart />
          </div>
        </main>

      ) : (
        <main className="max-w-screen-xl mx-auto px-6 py-8 space-y-5 animate-fade-in">

          {/* Page title row */}
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-base font-semibold text-text-primary">Overview</h1>
              <p className="text-xs text-text-muted mt-0.5">Job search automation status</p>
            </div>
            {analytics?.avg_time_to_response_hours != null && (
              <div className="text-xs text-text-muted">
                Avg. response:{' '}
                <span className="text-text-secondary font-medium">
                  {analytics.avg_time_to_response_hours.toFixed(1)}h
                </span>
              </div>
            )}
          </div>

          {/* KPI row */}
          <section className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-3">
            <KPICard label="Total Applied"    value={kpis?.total_applied ?? 0}                           icon={<Send className="w-4 h-4" />}      accent="accent"  loading={kpisLoading} subtext="all time"            />
            <KPICard label="Applied Today"    value={kpis?.applied_today ?? 0}                           icon={<Zap className="w-4 h-4" />}       accent="info"    loading={kpisLoading} subtext="since midnight"       />
            <KPICard label="Interviews"       value={kpis?.interviews_this_week ?? 0}                    icon={<Target className="w-4 h-4" />}    accent="purple"  loading={kpisLoading} subtext="next 7 days"         />
            <KPICard label="Success Rate"     value={`${kpis?.success_rate?.toFixed(1) ?? '0.0'}%`}     icon={<TrendingUp className="w-4 h-4" />} accent="success" loading={kpisLoading} subtext="interviews / applied" />
            <KPICard label="In Queue"         value={kpis?.pending_in_queue ?? 0}                        icon={<Hourglass className="w-4 h-4" />} accent="warning" loading={kpisLoading} subtext="pending"              />
            <KPICard label="Rejected"         value={kpis?.total_rejected ?? 0}                          icon={<XCircle className="w-4 h-4" />}   accent="danger"  loading={kpisLoading} subtext="all time"            />
            <KPICard label="Offers"           value={kpis?.total_offers ?? 0}                            icon={<Award className="w-4 h-4" />}     accent="success" loading={kpisLoading} subtext="received"            />
          </section>

          {/* Main grid */}
          <div className="grid grid-cols-1 xl:grid-cols-3 gap-5">
            <div className="xl:col-span-2 space-y-5">
              <ApplicationsQueue />
              <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
                <ConversionFunnel />
                <PlatformStatsChart />
              </div>
            </div>
            <div className="space-y-5">
              <InterviewList />
              <ActivityFeed />
            </div>
          </div>

        </main>
      )}
    </div>
  )
}
