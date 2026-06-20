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
import { Send, Zap, Target, TrendingUp, Hourglass, XCircle, Award, Sun } from 'lucide-react'


function WSStatus({ connected }: { connected: boolean }) {
  return (
    <div className="flex items-center gap-1.5 text-xs">
      <span
        className={clsx(
          'w-2 h-2 rounded-full transition-colors duration-500',
          connected ? 'bg-success animate-pulse-slow' : 'bg-danger',
        )}
      />
      <span className={connected ? 'text-success' : 'text-danger'}>
        {connected ? 'Live' : 'Reconnecting…'}
      </span>
    </div>
  )
}

export default function DashboardPage() {
  const [activeTab, setActiveTab] = useState<'dashboard' | 'profile' | 'analytics'>('dashboard')
  const [wsConnected, setWsConnected] = useState(false)
  const [lastEvent, setLastEvent] = useState<string | null>(null)
  const [greeting, setGreeting] = useState('Welcome')
  const [dateString, setDateString] = useState('')

  useEffect(() => {
    const now = new Date()
    const currentGreeting =
      now.getHours() < 12
        ? 'Good morning'
        : now.getHours() < 18
        ? 'Good afternoon'
        : 'Good evening'
    setGreeting(currentGreeting)
    setDateString(
      now.toLocaleDateString([], {
        weekday: 'long',
        year: 'numeric',
        month: 'long',
        day: 'numeric',
      })
    )
  }, [])

  const handleEvent = useCallback((evt: { event: string }) => {
    if (evt.event === 'connected') {
      setWsConnected(true)
    } else if (evt.event === 'disconnected') {
      setWsConnected(false)
    } else if (evt.event !== 'ping') {
      setLastEvent(evt.event)
      // Flash indication then clear
      setTimeout(() => setLastEvent(null), 3000)
    }
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

  return (
    <div className="min-h-screen bg-bg-primary">
      {/* ── Navbar ───────────────────────────────────────────────────────── */}
      <header className="sticky top-0 z-30 bg-bg-secondary/80 backdrop-blur-xl border-b border-bg-border">
        <div className="max-w-screen-2xl mx-auto px-6 h-14 flex items-center justify-between gap-4">
          {/* Logo */}
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-gradient-accent flex items-center justify-center text-white text-sm font-bold shadow-accent">
              BD
            </div>
            <span className="font-semibold text-text-primary hidden sm:block">
              BD Automator
            </span>
          </div>

          {/* Centre nav tabs */}
          <nav className="flex items-center gap-1 text-sm hidden md:flex">
            {[
              { id: 'dashboard', label: 'Dashboard' },
              { id: 'profile', label: 'Candidate Profile' },
              { id: 'analytics', label: 'Analytics' },
            ].map((tab) => (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id as any)}
                className={clsx(
                  'px-3 py-1.5 rounded-lg transition-colors duration-150 font-medium',
                  activeTab === tab.id
                    ? 'bg-accent/15 text-accent'
                    : 'text-text-muted hover:text-text-primary hover:bg-bg-hover',
                )}
              >
                {tab.label}
              </button>
            ))}
          </nav>

          {/* Right side */}
          <div className="flex items-center gap-4">
            <WSStatus connected={wsConnected} />
            {lastEvent && (
              <span className="text-xs text-accent animate-fade-in">
                ↺ {lastEvent.replace('.', ' ')}
              </span>
            )}
            <div className="w-8 h-8 rounded-full bg-gradient-accent flex items-center justify-center text-white text-xs font-bold">
              SH
            </div>
          </div>
        </div>
      </header>

      {/* ── Hero header ──────────────────────────────────────────────────── */}
      <div className="max-w-screen-2xl mx-auto px-6 pt-8 pb-4">
        <div className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-2">
          <div>
            <h1 className="text-2xl font-bold text-text-primary flex items-center gap-2">
              <Sun className="w-6 h-6 text-warning" /> {greeting}
            </h1>
            <p className="text-text-muted text-sm mt-0.5">
              {dateString}
              {dateString && ' · '}
              Here's your job search overview.
            </p>
          </div>

          {analytics?.avg_time_to_response_hours != null && (
            <div className="text-xs text-text-muted bg-bg-card border border-bg-border rounded-xl px-4 py-2 flex items-center gap-1.5">
              <Zap className="w-3.5 h-3.5 text-accent" /> Avg response time:{' '}
              <span className="text-accent font-semibold">
                {analytics.avg_time_to_response_hours.toFixed(1)}h
              </span>
            </div>
          )}
        </div>
      </div>

      {/* ── Main layout ──────────────────────────────────────────────────── */}
      {activeTab === 'profile' ? (
        <main className="max-w-screen-2xl mx-auto px-6 pb-12">
          <CandidateProfileForm onSuccess={() => setActiveTab('dashboard')} />
        </main>
      ) : activeTab === 'analytics' ? (
        <main className="max-w-screen-2xl mx-auto px-6 pb-12 space-y-6 animate-fade-in">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <section aria-label="Conversion funnel">
              <ConversionFunnel />
            </section>
            <section aria-label="Platform statistics">
              <PlatformStatsChart />
            </section>
          </div>
        </main>
      ) : (
        <main className="max-w-screen-2xl mx-auto px-6 pb-12 space-y-6 animate-fade-in">

          {/* KPI Cards row */}
          <section
            className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-4"
            aria-label="Key metrics"
          >
            <KPICard
              label="Total Applied"
              value={kpis?.total_applied ?? 0}
              icon={<Send className="w-5 h-5" />}
              accent="accent"
              loading={kpisLoading}
              subtext="all time"
            />
            <KPICard
              label="Applied Today"
              value={kpis?.applied_today ?? 0}
              icon={<Zap className="w-5 h-5" />}
              accent="info"
              loading={kpisLoading}
              subtext="since midnight"
            />
            <KPICard
              label="Interviews This Week"
              value={kpis?.interviews_this_week ?? 0}
              icon={<Target className="w-5 h-5" />}
              accent="purple"
              loading={kpisLoading}
              subtext="next 7 days"
            />
            <KPICard
              label="Success Rate"
              value={`${kpis?.success_rate?.toFixed(1) ?? '0.0'}%`}
              icon={<TrendingUp className="w-5 h-5" />}
              accent="success"
              loading={kpisLoading}
              subtext="interviews / applied"
            />
            <KPICard
              label="In Queue"
              value={kpis?.pending_in_queue ?? 0}
              icon={<Hourglass className="w-5 h-5" />}
              accent="warning"
              loading={kpisLoading}
              subtext="pending automation"
            />
            <KPICard
              label="Rejected"
              value={kpis?.total_rejected ?? 0}
              icon={<XCircle className="w-5 h-5" />}
              accent="danger"
              loading={kpisLoading}
              subtext="all time"
            />
            <KPICard
              label="Offers"
              value={kpis?.total_offers ?? 0}
              icon={<Award className="w-5 h-5" />}
              accent="success"
              loading={kpisLoading}
              subtext="congratulations!"
            />
          </section>

          {/* Main content grid */}
          <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">

            {/* Left column (2/3) — queue + funnel */}
            <div className="xl:col-span-2 space-y-6">

              {/* Application queue */}
              <section aria-label="Active job queue">
                <ApplicationsQueue />
              </section>

              {/* Two-column: Funnel + Platform chart */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                <section aria-label="Conversion funnel">
                  <ConversionFunnel />
                </section>
                <section aria-label="Platform statistics">
                  <PlatformStatsChart />
                </section>
              </div>
            </div>

            {/* Right column (1/3) — interviews + activity */}
            <div className="space-y-6">
              <section aria-label="Upcoming interviews">
                <InterviewList />
              </section>
              <section aria-label="Activity feed" className="max-h-[480px] flex flex-col">
                <ActivityFeed />
              </section>
            </div>

          </div>

        </main>
      )}
    </div>
  )
}
