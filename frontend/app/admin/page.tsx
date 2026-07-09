'use client'

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { KPICard } from '@/components/dashboard/KPICard'
import { StatusBadge } from '@/components/shared/StatusBadge'
import { Modal } from '@/components/ui/Modal'
import { useConfirm } from '@/components/ui/ConfirmDialog'
import { useToast } from '@/components/ui/Toast'
import { Search, Send, Target, Award, Briefcase, Clock, Sparkles, RefreshCw, Play, Trash2 } from 'lucide-react'
import { formatDistanceToNow } from '@/lib/utils'
import { formatJobUrl } from '@/components/utils'

interface MatchingDetail {
  job_title: string
  company: string
  score: number
  passed: boolean
}

interface MatchingResult {
  jobs_scanned: number
  pgvector_passed: number
  llm_passed: number
  enqueued_count: number
  details: MatchingDetail[]
}

export default function AdminPage() {
  const confirm = useConfirm()
  const toast = useToast()

  const [search, setSearch] = useState('')
  const [selectedCandidateId, setSelectedCandidateId] = useState('')
  const [isMatchingRunning, setIsMatchingRunning] = useState(false)
  const [matchingResult, setMatchingResult] = useState<MatchingResult | null>(null)
  const [showModal, setShowModal] = useState(false)
  const [isFailingStuck, setIsFailingStuck] = useState(false)

  const { data: candidates, isLoading: candidatesLoading } = useQuery({
    queryKey: ['admin-candidates'],
    queryFn: () => api.getCandidates(),
  })

  const { data: kpis, isLoading: kpisLoading } = useQuery({
    queryKey: ['admin-kpis'],
    queryFn: () => api.getKPIs(),
    refetchInterval: 30_000,
  })

  const { data: applications, isLoading: appsLoading } = useQuery({
    queryKey: ['admin-applications'],
    queryFn: () => api.getApplications({ limit: 100 }),
    refetchInterval: 30_000,
  })

  const { data: activity, isLoading: activityLoading } = useQuery({
    queryKey: ['admin-activity'],
    queryFn: () => api.getActivityFeed(15),
    refetchInterval: 30_000,
  })

  const handleRunMatching = async () => {
    if (!selectedCandidateId) return
    setIsMatchingRunning(true)
    try {
      const res = await api.runMatching(selectedCandidateId)
      setMatchingResult(res as MatchingResult)
      setShowModal(true)
      toast.success('Matching run completed!')
    } catch (err: unknown) {
      const errorObj = err as { response?: { data?: { detail?: string | { message?: string } } }; message?: string }
      const detail = errorObj.response?.data?.detail
      const msg = typeof detail === 'string' ? detail : detail?.message || errorObj.message || 'Matching run failed.'
      toast.error(msg)
    } finally {
      setIsMatchingRunning(false)
    }
  }

  const handleFailStuck = async () => {
    const isConfirmed = await confirm({
      title: 'Fail Stuck Applications?',
      message: 'Force-fail ALL pending or queued applications created in the last 5 hours?',
      confirmLabel: 'Force Fail',
      variant: 'danger',
    })

    if (!isConfirmed) return

    setIsFailingStuck(true)
    try {
      const res = await fetch('/api/applications/admin/fail-stuck?hours=5', { method: 'POST' })
      const data = await res.json()
      toast.success(`Killed ${data.failed_count} stuck application(s) from the last 5 hours.`)
    } catch (err: unknown) {
      const errorObj = err as { message?: string }
      toast.error(`Error: ${errorObj.message || 'Failed to clean stuck applications'}`)
    } finally {
      setIsFailingStuck(false)
    }
  }

  const filteredApps = applications?.filter(app => 
    app.job_title.toLowerCase().includes(search.toLowerCase()) ||
    app.company.toLowerCase().includes(search.toLowerCase()) ||
    app.platform.toLowerCase().includes(search.toLowerCase()) ||
    (app.candidate_name && app.candidate_name.toLowerCase().includes(search.toLowerCase())) ||
    (app.bd_user_name && app.bd_user_name.toLowerCase().includes(search.toLowerCase())) ||
    (app.bd_user_email && app.bd_user_email.toLowerCase().includes(search.toLowerCase()))
  )

  return (
    <div className="space-y-8 animate-fade-in max-w-7xl mx-auto">
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 border-b border-bg-border pb-6">
        <div>
          <h1 className="page-title">Admin Console</h1>
          <p className="page-subtitle">Cross-platform statistics, BD user activities, and application controls.</p>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          {/* Fail stuck applications */}
          <button
            onClick={handleFailStuck}
            disabled={isFailingStuck}
            title="Force-fail all FOUND/QUEUED applications stuck in the last 5 hours"
            className="flex items-center gap-2 px-3.5 py-2 bg-danger/10 hover:bg-danger/20 border border-danger/20 disabled:opacity-50 text-danger font-medium text-xs rounded-lg transition-colors"
          >
            {isFailingStuck ? <RefreshCw className="w-3.5 h-3.5 animate-spin" /> : <Trash2 className="w-3.5 h-3.5" />}
            Fail Stuck (5h)
          </button>

          {/* Manual Matching Trigger Control Panel */}
          <div className="flex items-center gap-3 bg-bg-secondary border border-bg-border p-2 px-3 rounded-xl shadow-sm">
            <span className="text-xs font-semibold text-text-muted">Manual Match:</span>
            <select
              value={selectedCandidateId}
              onChange={(e) => setSelectedCandidateId(e.target.value)}
              disabled={isMatchingRunning || candidatesLoading}
              className="input !py-1.5 !text-xs min-w-[180px]"
            >
              <option value="">Select Candidate...</option>
              {candidates?.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
            <button
              onClick={handleRunMatching}
              disabled={!selectedCandidateId || isMatchingRunning}
              className="btn-primary !py-1.5 !px-3 !text-xs"
            >
              {isMatchingRunning ? (
                <>
                  <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                  Matching...
                </>
              ) : (
                <>
                  <Play className="w-3.5 h-3.5 fill-current" />
                  Run Match
                </>
              )}
            </button>
          </div>
        </div>
      </div>

      {/* KPI Cards */}
      <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <KPICard 
          label="Jobs Applied Today" 
          value={kpis?.applied_today ?? 0} 
          icon={<Briefcase className="w-4 h-4" />} 
          accent="info"  
          loading={kpisLoading} 
        />
        <KPICard 
          label="Global Queue" 
          value={kpis?.pending_in_queue ?? 0} 
          icon={<Send className="w-4 h-4" />} 
          accent="warning" 
          loading={kpisLoading} 
        />
        <KPICard 
          label="Global Interviews" 
          value={kpis?.interviews_this_week ?? 0} 
          icon={<Target className="w-4 h-4" />} 
          accent="purple" 
          loading={kpisLoading} 
        />
        <KPICard 
          label="Global Offers" 
          value={kpis?.total_offers ?? 0} 
          icon={<Award className="w-4 h-4" />} 
          accent="success" 
          loading={kpisLoading} 
        />
      </section>

      {/* Main Grid */}
      <div className="grid grid-cols-1 xl:grid-cols-4 gap-6">
        {/* Applications List */}
        <div className="xl:col-span-3 space-y-6">
          <div className="card p-6 bg-bg-card border border-bg-border rounded-xl">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-6">
              <div>
                <h2 className="text-base font-semibold text-text-primary">Global Applications</h2>
                <p className="text-xs text-text-muted mt-0.5">Real-time status of all candidate applications across all BD users.</p>
              </div>
              <div className="relative">
                <Search className="w-4 h-4 text-text-muted absolute left-3 top-1/2 -translate-y-1/2" />
                <input 
                  type="text" 
                  placeholder="Filter by title, company, candidate, BD user..." 
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  className="input pl-9 w-72"
                />
              </div>
            </div>

            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="bg-bg-primary text-text-muted uppercase text-xs tracking-wider border-b border-bg-border">
                  <tr>
                    <th className="px-4 py-3 font-medium">Job & Company</th>
                    <th className="px-4 py-3 font-medium">Candidate</th>
                    <th className="px-4 py-3 font-medium">BD User</th>
                    <th className="px-4 py-3 font-medium">Fit Score</th>
                    <th className="px-4 py-3 font-medium">Status</th>
                    <th className="px-4 py-3 font-medium text-right">Applied</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-bg-border">
                  {appsLoading ? (
                    Array.from({ length: 6 }).map((_, i) => (
                      <tr key={i}>
                        <td className="px-4 py-3"><div className="h-4 bg-bg-border rounded w-40 animate-pulse mb-1"></div><div className="h-3 bg-bg-border rounded w-24 animate-pulse"></div></td>
                        <td className="px-4 py-3"><div className="h-4 bg-bg-border rounded w-28 animate-pulse"></div></td>
                        <td className="px-4 py-3"><div className="h-4 bg-bg-border rounded w-32 animate-pulse"></div></td>
                        <td className="px-4 py-3"><div className="h-4 bg-bg-border rounded w-10 animate-pulse"></div></td>
                        <td className="px-4 py-3"><div className="h-6 bg-bg-border rounded w-16 animate-pulse"></div></td>
                        <td className="px-4 py-3 text-right"><div className="h-4 bg-bg-border rounded w-16 animate-pulse ml-auto"></div></td>
                      </tr>
                    ))
                  ) : !filteredApps || filteredApps.length === 0 ? (
                    <tr>
                      <td colSpan={6} className="px-4 py-12 text-center text-text-muted">
                        No applications found matching search criteria.
                      </td>
                    </tr>
                  ) : (
                    filteredApps.map((app) => (
                      <tr key={app.application_id} className="hover:bg-bg-hover transition-colors">
                        <td className="px-4 py-3">
                          <div className="font-medium text-text-primary truncate max-w-[200px]" title={app.job_title}>
                            {app.job_url ? (
                              <a href={formatJobUrl(app.job_url)} target="_blank" rel="noopener noreferrer" className="hover:text-accent hover:underline">
                                {app.job_title}
                              </a>
                            ) : app.job_title}
                          </div>
                          <div className="text-xs text-text-muted mt-0.5">{app.company} • <span className="capitalize">{app.platform}</span></div>
                        </td>
                        <td className="px-4 py-3 text-text-primary font-medium whitespace-nowrap">
                          {app.candidate_name || 'Unknown'}
                        </td>
                        <td className="px-4 py-3">
                          <div className="text-text-primary font-medium">{app.bd_user_name}</div>
                          <div className="text-xs text-text-muted">{app.bd_user_email}</div>
                        </td>
                        <td className="px-4 py-3 whitespace-nowrap">
                          {app.fit_score != null ? (
                            <span className={`text-xs font-semibold ${app.fit_score >= 70 ? 'text-success' : app.fit_score >= 40 ? 'text-warning' : 'text-text-muted'}`}>
                              {Math.round(app.fit_score)}%
                            </span>
                          ) : (
                            <span className="text-text-muted text-xs">—</span>
                          )}
                        </td>
                        <td className="px-4 py-3 whitespace-nowrap">
                          <StatusBadge status={app.status} />
                        </td>
                        <td className="px-4 py-3 text-right text-text-muted whitespace-nowrap text-xs">
                          {formatDistanceToNow(app.created_at)} ago
                        </td>
                      </tr>
                    ))
                  )
                }
                </tbody>
              </table>
            </div>
          </div>
        </div>

        {/* Global Activity Feed */}
        <div className="xl:col-span-1">
          <div className="card p-6 bg-bg-card border border-bg-border rounded-xl space-y-6">
            <div>
              <h2 className="text-base font-semibold text-text-primary">Global Activity</h2>
              <p className="text-xs text-text-muted mt-0.5">Real-time actions across all users.</p>
            </div>

            <div className="space-y-4 max-h-[600px] overflow-y-auto pr-1">
              {activityLoading ? (
                Array.from({ length: 8 }).map((_, i) => (
                  <div key={i} className="flex gap-3">
                    <div className="w-1.5 h-1.5 rounded-full bg-bg-border mt-2 shrink-0 animate-pulse"></div>
                    <div className="space-y-1.5 w-full">
                      <div className="h-3 bg-bg-border rounded w-full animate-pulse"></div>
                      <div className="h-2 bg-bg-border rounded w-24 animate-pulse"></div>
                    </div>
                  </div>
                ))
              ) : !activity || activity.length === 0 ? (
                <div className="text-center py-8 text-text-muted text-sm">
                  No activity events recorded yet.
                </div>
              ) : (
                activity.map((event, idx) => (
                  <div key={idx} className="flex gap-3 text-sm group">
                    <div className="w-1.5 h-1.5 rounded-full bg-accent mt-2 shrink-0 group-hover:scale-125 transition-transform"></div>
                    <div className="space-y-0.5 min-w-0">
                      <p className="text-text-primary font-medium leading-snug break-words text-xs">
                        {event.summary}
                      </p>
                      <span className="flex items-center gap-1 text-[10px] text-text-muted">
                        <Clock className="w-3 h-3" />
                        {formatDistanceToNow(event.timestamp)} ago
                      </span>
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Reusable Matching Result Modal */}
      <Modal
        open={showModal}
        onClose={() => setShowModal(false)}
        title="Matching Run Results"
        size="2xl"
      >
        {matchingResult && (
          <div className="space-y-6">
            {/* Stats Summary Grid */}
            <div className="grid grid-cols-4 gap-4 text-center">
              <div className="bg-bg-primary border border-bg-border p-3 rounded-lg">
                <div className="text-2xl font-bold text-text-primary">{matchingResult.jobs_scanned}</div>
                <div className="text-[10px] uppercase font-semibold tracking-wider text-text-muted mt-1">Jobs Checked</div>
              </div>
              <div className="bg-bg-primary border border-bg-border p-3 rounded-lg">
                <div className="text-2xl font-bold text-text-primary">{matchingResult.pgvector_passed}</div>
                <div className="text-[10px] uppercase font-semibold tracking-wider text-text-muted mt-1">Vector Passed</div>
              </div>
              <div className="bg-bg-primary border border-bg-border p-3 rounded-lg">
                <div className="text-2xl font-bold text-success">{matchingResult.llm_passed}</div>
                <div className="text-[10px] uppercase font-semibold tracking-wider text-text-muted mt-1">LLM Passed (≥70)</div>
              </div>
              <div className="bg-bg-primary border border-bg-border p-3 rounded-lg">
                <div className="text-2xl font-bold text-accent">{matchingResult.enqueued_count}</div>
                <div className="text-[10px] uppercase font-semibold tracking-wider text-text-muted mt-1">Enqueued</div>
              </div>
            </div>

            {/* Scanned Jobs List */}
            <div className="space-y-3">
              <h4 className="font-semibold text-text-primary text-sm">Detailed Evaluations</h4>
              <div className="border border-bg-border rounded-lg overflow-hidden max-h-[300px] overflow-y-auto">
                <table className="w-full text-left text-xs">
                  <thead className="bg-bg-primary text-text-muted uppercase tracking-wider border-b border-bg-border font-medium">
                    <tr>
                      <th className="px-4 py-2">Job & Company</th>
                      <th className="px-4 py-2 font-medium">Fit Score</th>
                      <th className="px-4 py-2 text-right">Result</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-bg-border">
                    {matchingResult.details?.length === 0 ? (
                      <tr>
                        <td colSpan={3} className="px-4 py-8 text-center text-text-muted">
                          No jobs passed pgvector similarity for LLM evaluation.
                        </td>
                      </tr>
                    ) : (
                      matchingResult.details.map((detail, index) => (
                        <tr key={index} className="hover:bg-bg-hover transition-colors">
                          <td className="px-4 py-2">
                            <div className="font-semibold text-text-primary">{detail.job_title}</div>
                            <div className="text-[10px] text-text-muted">{detail.company}</div>
                          </td>
                          <td className="px-4 py-2 font-medium">
                            <span className={detail.score >= 70 ? 'text-success font-bold' : detail.score >= 40 ? 'text-warning' : 'text-text-muted'}>
                              {Math.round(detail.score)}%
                            </span>
                          </td>
                          <td className="px-4 py-2 text-right">
                            <StatusBadge status={detail.passed ? 'QUEUED' : 'REJECTED'} />
                          </td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}
      </Modal>
    </div>
  )
}
