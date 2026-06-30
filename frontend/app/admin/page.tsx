'use client'

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { KPICard } from '@/components/dashboard/KPICard'
import { Search, Send, Target, Award, Briefcase, Clock, Sparkles, RefreshCw, Play, CheckCircle, AlertCircle, X, Trash2 } from 'lucide-react'
import { formatDistanceToNow } from '@/lib/utils'

export default function AdminPage() {
  const [search, setSearch] = useState('')
  const [selectedCandidateId, setSelectedCandidateId] = useState('')
  const [isMatchingRunning, setIsMatchingRunning] = useState(false)
  const [matchingResult, setMatchingResult] = useState<any>(null)
  const [matchingError, setMatchingError] = useState<string | null>(null)
  const [showModal, setShowModal] = useState(false)
  const [isFailingStuck, setIsFailingStuck] = useState(false)
  const [failStuckResult, setFailStuckResult] = useState<string | null>(null)
  
  const { data: candidates, isLoading: candidatesLoading } = useQuery({
    queryKey: ['admin-candidates'],
    queryFn: () => api.getCandidates(),
  })

  const handleRunMatching = async () => {
    if (!selectedCandidateId) return
    setIsMatchingRunning(true)
    setMatchingError(null)
    setMatchingResult(null)
    try {
      const res = await api.runMatching(selectedCandidateId)
      setMatchingResult(res)
      setShowModal(true)
    } catch (err: any) {
      console.error(err)
      const detail = err.response?.data?.detail
      const msg = typeof detail === 'string' ? detail : detail?.message || err.message || 'An unexpected error occurred.'
      setMatchingError(msg)
    } finally {
      setIsMatchingRunning(false)
    }
  }

  const handleFailStuck = async () => {
    if (!confirm('Force-fail ALL pending/queued applications created in the last 5 hours?')) return
    setIsFailingStuck(true)
    setFailStuckResult(null)
    try {
      const res = await fetch('/api/applications/admin/fail-stuck?hours=5', { method: 'POST' })
      const data = await res.json()
      setFailStuckResult(`Killed ${data.failed_count} stuck application(s) from the last 5 hours.`)
    } catch (err: any) {
      setFailStuckResult(`Error: ${err.message}`)
    } finally {
      setIsFailingStuck(false)
    }
  }

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

  const filteredApps = applications?.filter(app => 
    app.job_title.toLowerCase().includes(search.toLowerCase()) ||
    app.company.toLowerCase().includes(search.toLowerCase()) ||
    app.platform.toLowerCase().includes(search.toLowerCase()) ||
    (app.candidate_name && app.candidate_name.toLowerCase().includes(search.toLowerCase())) ||
    (app.bd_user_name && app.bd_user_name.toLowerCase().includes(search.toLowerCase())) ||
    (app.bd_user_email && app.bd_user_email.toLowerCase().includes(search.toLowerCase()))
  )

  const getStatusColor = (status: string) => {
    switch (status.toUpperCase()) {
      case 'OFFER':
        return 'bg-green-500/10 border-green-500/20 text-green-500'
      case 'REJECTED':
      case 'FAILED':
        return 'bg-red-500/10 border-red-500/20 text-red-500'
      case 'INTERVIEW_R1':
      case 'INTERVIEW_R2':
        return 'bg-purple-500/10 border-purple-500/20 text-purple-500'
      case 'SUBMITTED':
      case 'CONFIRMED':
        return 'bg-blue-500/10 border-blue-500/20 text-blue-500'
      case 'QUEUED':
      case 'APPLICATION_STARTED':
        return 'bg-yellow-500/10 border-yellow-500/20 text-yellow-500'
      default:
        return 'bg-bg-primary border-bg-border text-text-muted'
    }
  }

  return (
    <div className="space-y-8 animate-in fade-in duration-500 max-w-7xl mx-auto">
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 border-b border-bg-border pb-6">
        <div>
          <h1 className="text-2xl font-bold text-text-primary tracking-tight">Admin Console Overview</h1>
          <p className="text-sm text-text-muted mt-1">Cross-platform statistics, BD user activities, and daily applied jobs.</p>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          {/* Fail stuck applications */}
          <button
            onClick={handleFailStuck}
            disabled={isFailingStuck}
            title="Force-fail all FOUND/QUEUED applications stuck in the last 5 hours"
            className="flex items-center gap-2 px-3 py-1.5 bg-red-500/10 hover:bg-red-500/20 border border-red-500/20 disabled:opacity-50 text-red-500 font-medium text-xs rounded-lg transition-colors"
          >
            {isFailingStuck ? <RefreshCw className="w-3.5 h-3.5 animate-spin" /> : <Trash2 className="w-3.5 h-3.5" />}
            Fail Stuck (5h)
          </button>

          {/* Manual Matching Trigger Control Panel */}
          <div className="flex items-center gap-3 bg-bg-secondary border border-bg-border p-3 rounded-xl shadow-sm">
            <div className="text-xs font-semibold text-text-muted mr-1">Manual Matching:</div>
            <select
              value={selectedCandidateId}
              onChange={(e) => setSelectedCandidateId(e.target.value)}
              disabled={isMatchingRunning || candidatesLoading}
              className="px-3 py-1.5 bg-bg-primary border border-bg-border rounded-lg text-sm text-text-primary focus:outline-none focus:border-accent min-w-[200px]"
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
              className="flex items-center gap-2 px-4 py-1.5 bg-accent hover:bg-accent-hover disabled:opacity-50 text-white font-medium text-sm rounded-lg transition-colors shadow-sm"
            >
              {isMatchingRunning ? (
                <>
                  <RefreshCw className="w-4 h-4 animate-spin" />
                  Matching...
                </>
              ) : (
                <>
                  <Play className="w-4 h-4 fill-current" />
                  Run Match
                </>
              )}
            </button>
          </div>
        </div>
      </div>

      {matchingError && (
        <div className="flex items-center gap-3 bg-red-500/10 border border-red-500/20 p-4 rounded-xl text-sm text-red-500 animate-in slide-in-from-top duration-300">
          <AlertCircle className="w-5 h-5 shrink-0" />
          <div className="flex-1 font-medium">{matchingError}</div>
          <button onClick={() => setMatchingError(null)} className="hover:text-red-400">
            <X className="w-4 h-4" />
          </button>
        </div>
      )}

      {failStuckResult && (
        <div className="flex items-center gap-3 bg-amber-500/10 border border-amber-500/20 p-4 rounded-xl text-sm text-amber-600 animate-in slide-in-from-top duration-300">
          <CheckCircle className="w-5 h-5 shrink-0" />
          <div className="flex-1 font-medium">{failStuckResult}</div>
          <button onClick={() => setFailStuckResult(null)} className="hover:text-amber-500">
            <X className="w-4 h-4" />
          </button>
        </div>
      )}



      {/* KPI Cards */}
      <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <KPICard 
          label="Total Jobs Found Today" 
          value={kpis?.applied_today ?? 0} 
          icon={<Briefcase className="w-4 h-4" />} 
          accent="info"  
          loading={kpisLoading} 
        />
        <KPICard 
          label="Global Applications In Progress" 
          value={kpis?.pending_in_queue ?? 0} 
          icon={<Send className="w-4 h-4" />} 
          accent="warning" 
          loading={kpisLoading} 
        />
        <KPICard 
          label="Global Interviews Scheduled" 
          value={kpis?.interviews_this_week ?? 0} 
          icon={<Target className="w-4 h-4" />} 
          accent="purple" 
          loading={kpisLoading} 
        />
        <KPICard 
          label="Global Offers Secured" 
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
          <div className="card p-6 bg-bg-secondary border border-bg-border rounded-xl">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-6">
              <div>
                <h2 className="text-lg font-semibold text-text-primary">Daily Applied Jobs & Applications</h2>
                <p className="text-xs text-text-muted mt-0.5">Real-time status of all active job applications.</p>
              </div>
              <div className="relative">
                <Search className="w-4 h-4 text-text-muted absolute left-3 top-1/2 -translate-y-1/2" />
                <input 
                  type="text" 
                  placeholder="Filter by title, company, candidate, BD user..." 
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  className="pl-9 pr-4 py-2 bg-bg-primary border border-bg-border rounded-lg text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent transition-colors w-72"
                />
              </div>
            </div>

            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="bg-bg-primary/50 text-text-muted uppercase text-xs tracking-wider border-b border-bg-border">
                  <tr>
                    <th className="px-4 py-3 font-medium">Job & Company</th>
                    <th className="px-4 py-3 font-medium">Candidate</th>
                    <th className="px-4 py-3 font-medium">Managed By (BD User)</th>
                    <th className="px-4 py-3 font-medium">Score</th>
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
                              <a href={app.job_url} target="_blank" rel="noopener noreferrer" className="hover:text-accent hover:underline">
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
                            <span className={`text-xs font-semibold ${app.fit_score >= 70 ? 'text-green-500' : app.fit_score >= 40 ? 'text-yellow-500' : 'text-text-muted'}`}>
                              {Math.round(app.fit_score)}% Fit
                            </span>
                          ) : (
                            <span className="text-text-muted text-xs">—</span>
                          )}
                        </td>
                        <td className="px-4 py-3 whitespace-nowrap">
                          <span className={`inline-flex items-center px-2.5 py-0.5 border text-xs font-medium rounded-full ${getStatusColor(app.status)}`}>
                            {app.status}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-right text-text-muted whitespace-nowrap text-xs">
                          {formatDistanceToNow(app.created_at)} ago
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>

        {/* Global Activity Feed */}
        <div className="xl:col-span-1">
          <div className="card p-6 bg-bg-secondary border border-bg-border rounded-xl space-y-6">
            <div>
              <h2 className="text-lg font-semibold text-text-primary">Global Activity</h2>
              <p className="text-xs text-text-muted mt-0.5">Real-time actions across all BD Users.</p>
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
                      <p className="text-text-primary font-medium leading-snug break-words">
                        {event.summary}
                      </p>
                      <span className="flex items-center gap-1 text-xs text-text-muted">
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

      {showModal && matchingResult && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-4 overflow-y-auto animate-in fade-in duration-200">
          <div className="bg-bg-secondary border border-bg-border rounded-xl shadow-xl w-full max-w-2xl overflow-hidden flex flex-col max-h-[85vh] animate-in zoom-in-95 duration-200">
            {/* Modal Header */}
            <div className="flex items-center justify-between border-b border-bg-border p-4">
              <div className="flex items-center gap-2">
                <Sparkles className="w-5 h-5 text-accent" />
                <h3 className="text-lg font-bold text-text-primary">Matching Run Results</h3>
              </div>
              <button
                onClick={() => setShowModal(false)}
                className="text-text-muted hover:text-text-primary rounded-lg p-1 hover:bg-bg-hover transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            {/* Modal Body */}
            <div className="p-6 overflow-y-auto space-y-6">
              {/* Stats Summary Grid */}
              <div className="grid grid-cols-4 gap-4 text-center">
                <div className="bg-bg-primary border border-bg-border p-3 rounded-lg">
                  <div className="text-2xl font-bold text-text-primary">{matchingResult.jobs_scanned}</div>
                  <div className="text-[10px] uppercase font-semibold tracking-wider text-text-muted mt-1">Jobs Checked</div>
                </div>
                <div className="bg-bg-primary border border-bg-border p-3 rounded-lg">
                  <div className="text-2xl font-bold text-text-primary">{matchingResult.pgvector_passed}</div>
                  <div className="text-[10px] uppercase font-semibold tracking-wider text-text-muted mt-1">Vector Similarity Passed</div>
                </div>
                <div className="bg-bg-primary border border-bg-border p-3 rounded-lg">
                  <div className="text-2xl font-bold text-green-500">{matchingResult.llm_passed}</div>
                  <div className="text-[10px] uppercase font-semibold tracking-wider text-text-muted mt-1">LLM Passed (&gt;=70)</div>
                </div>
                <div className="bg-bg-primary border border-bg-border p-3 rounded-lg">
                  <div className="text-2xl font-bold text-accent">{matchingResult.enqueued_count}</div>
                  <div className="text-[10px] uppercase font-semibold tracking-wider text-text-muted mt-1">Applications Enqueued</div>
                </div>
              </div>

              {/* Scanned Jobs List */}
              <div className="space-y-3">
                <h4 className="font-semibold text-text-primary text-sm">Detailed Evaluations</h4>
                <div className="border border-bg-border rounded-lg overflow-hidden max-h-[300px] overflow-y-auto">
                  <table className="w-full text-left text-xs">
                    <thead className="bg-bg-primary/50 text-text-muted uppercase tracking-wider border-b border-bg-border font-medium">
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
                        matchingResult.details.map((detail: any, index: number) => (
                          <tr key={index} className="hover:bg-bg-hover transition-colors">
                            <td className="px-4 py-2">
                              <div className="font-semibold text-text-primary">{detail.job_title}</div>
                              <div className="text-[10px] text-text-muted">{detail.company}</div>
                            </td>
                            <td className="px-4 py-2 font-medium">
                              <span className={detail.score >= 70 ? 'text-green-500 font-bold' : detail.score >= 40 ? 'text-yellow-500' : 'text-text-muted'}>
                                {Math.round(detail.score)}%
                              </span>
                            </td>
                            <td className="px-4 py-2 text-right">
                              <span className={`inline-flex items-center px-2 py-0.5 rounded text-[10px] font-semibold ${
                                detail.passed ? 'bg-green-500/10 text-green-500 border border-green-500/20' : 'bg-red-500/10 text-red-500 border border-red-500/20'
                              }`}>
                                {detail.passed ? 'QUEUED' : 'REJECTED'}
                              </span>
                            </td>
                          </tr>
                        ))
                      )}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>

            {/* Modal Footer */}
            <div className="border-t border-bg-border p-4 flex justify-end">
              <button
                onClick={() => setShowModal(false)}
                className="px-4 py-2 bg-bg-primary hover:bg-bg-hover border border-bg-border rounded-lg text-sm text-text-primary transition-colors font-medium"
              >
                Close Summary
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
