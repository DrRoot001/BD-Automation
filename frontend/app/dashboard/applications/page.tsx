'use client'

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Search, Filter, AlertCircle, RefreshCw, Layers } from 'lucide-react'
import { useApplications } from '@/hooks/useApplications'
import { useCurrentUser } from '@/hooks/useCurrentUser'
import { api, ApplicationSummary } from '@/lib/api'
import { StatusBadge } from '@/components/shared/StatusBadge'
import { Skeleton } from '@/components/shared/Skeleton'
import { formatDistanceToNow } from '@/lib/utils'
import { ApplicationDrawer } from '@/components/dashboard/ApplicationDrawer'
import { useWebSocket } from '@/hooks/useWebSocket'

const COMPLETED_STATUSES = [
  'SUBMITTED',
  'CONFIRMED',
  'REJECTED',
  'OFFER',
  'INTERVIEW_R1',
  'INTERVIEW_R2',
  'INTERVIEW_R3',
  'INTERVIEW_R4',
  'ASSESSMENT'
]

function isCompleted(status: string) {
  return COMPLETED_STATUSES.includes(status?.toUpperCase())
}

export default function ApplicationsPage() {
  const [selectedCandidateId, setSelectedCandidateId] = useState<string>('')
  const [searchTerm, setSearchTerm] = useState('')

  // Connect WebSocket to get real-time cache invalidations
  useWebSocket()

  // Fetch candidates managed by the logged-in BD User
  const { data: candidates = [], isLoading: candidatesLoading } = useQuery({
    queryKey: ['candidates'],
    queryFn: () => api.getCandidates(),
  })

  const { data: applications, isLoading, isError, refetch } = useApplications({
    candidateId: selectedCandidateId || undefined,
    limit: 100 // Fetch more to allow proper filtering
  })

  const [selectedApp, setSelectedApp] = useState<ApplicationSummary | null>(null)
  const [isDrawerOpen, setIsDrawerOpen] = useState(false)

  const openDrawer = (app: ApplicationSummary) => {
    setSelectedApp(app)
    setIsDrawerOpen(true)
  }

  const handleRetry = (appId: string) => {
    console.log("Retry application:", appId)
  }

  // Filter applications by company or job title
  const filteredApplications = applications?.filter(app => 
    app.company.toLowerCase().includes(searchTerm.toLowerCase()) ||
    app.job_title.toLowerCase().includes(searchTerm.toLowerCase())
  )

  return (
    <div className="space-y-6 animate-in fade-in duration-500 max-w-7xl mx-auto">
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-text-primary tracking-tight">Applications Tracker</h1>
          <p className="text-sm text-text-muted mt-1">Monitor the status of your automated job applications.</p>
        </div>
        
        <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-3">
          {/* Candidate selector */}
          <select
            value={selectedCandidateId}
            onChange={(e) => setSelectedCandidateId(e.target.value)}
            disabled={candidatesLoading}
            className="bg-bg-secondary border border-bg-border text-text-primary rounded-lg text-sm px-4 py-2 focus:outline-none focus:border-accent transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed min-w-[200px]"
          >
            <option value="">All Candidates</option>
            {candidates.map((cand) => (
              <option key={cand.id} value={cand.id}>
                {cand.name}
              </option>
            ))}
          </select>

          <div className="relative">
            <Search className="w-4 h-4 text-text-muted absolute left-3 top-1/2 -translate-y-1/2" />
            <input 
              type="text" 
              placeholder="Search companies or roles..." 
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              className="pl-9 pr-4 py-2 bg-bg-primary border border-bg-border rounded-lg text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent transition-colors w-64"
            />
          </div>
        </div>
      </div>

      {/* Main Content Area */}
      {isError ? (
        <div className="flex flex-col items-center justify-center min-h-[400px] bg-bg-secondary rounded-xl border border-bg-border">
          <AlertCircle className="w-12 h-12 text-danger mb-4" />
          <h2 className="text-lg font-semibold text-text-primary">Failed to load applications</h2>
          <p className="text-text-muted text-sm mt-1 mb-6">There was an error communicating with the API.</p>
          <button 
            onClick={() => refetch()}
            className="flex items-center gap-2 px-4 py-2 bg-bg-primary border border-bg-border hover:bg-bg-hover text-text-primary rounded-md transition-colors"
          >
            <RefreshCw className="w-4 h-4" />
            Retry
          </button>
        </div>
      ) : (
        <div className="bg-bg-secondary border border-bg-border rounded-xl overflow-hidden shadow-sm">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="bg-bg-primary/50 text-text-muted uppercase text-xs tracking-wider border-b border-bg-border">
                <tr>
                  <th className="px-6 py-4 font-medium">Company & Role</th>
                  <th className="px-6 py-4 font-medium">Platform</th>
                  <th className="px-6 py-4 font-medium">Status</th>
                  <th className="px-6 py-4 font-medium">Combined Score</th>
                  <th className="px-6 py-4 font-medium text-right">Applied</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-bg-border">
                {isLoading ? (
                  Array.from({ length: 5 }).map((_, i) => (
                    <tr key={i}>
                      <td className="px-6 py-4"><Skeleton className="h-5 w-48" /><Skeleton className="h-4 w-32 mt-2" /></td>
                      <td className="px-6 py-4"><Skeleton className="h-5 w-20" /></td>
                      <td className="px-6 py-4"><Skeleton className="h-6 w-24 rounded-full" /></td>
                      <td className="px-6 py-4"><Skeleton className="h-5 w-12" /></td>
                      <td className="px-6 py-4 text-right flex justify-end"><Skeleton className="h-5 w-16" /></td>
                    </tr>
                  ))
                ) : !filteredApplications || filteredApplications.length === 0 ? (
                  <tr>
                    <td colSpan={5} className="px-6 py-16 text-center">
                      <div className="flex flex-col items-center justify-center">
                        <div className="w-16 h-16 bg-bg-primary rounded-full flex items-center justify-center mb-4 border border-bg-border">
                          <Layers className="w-8 h-8 text-text-muted" />
                        </div>
                        <h3 className="text-lg font-medium text-text-primary">No applications found</h3>
                        <p className="text-text-muted text-sm mt-1 max-w-sm">
                          {searchTerm ? "No applications matched your search." : "Once the BD Automator starts applying to jobs on behalf of the selected candidate, they will appear here."}
                        </p>
                      </div>
                    </td>
                  </tr>
                ) : (
                  filteredApplications.map((app) => (
                    <tr 
                      key={app.application_id}
                      onClick={() => openDrawer(app)}
                      className="group hover:bg-bg-hover cursor-pointer transition-colors"
                    >
                      <td className="px-6 py-4">
                        <div className="flex items-start justify-between gap-4">
                          <div>
                            <div className="font-semibold text-text-primary group-hover:text-accent transition-colors">{app.job_title}</div>
                            <div className="text-text-muted mt-0.5">{app.company}</div>
                          </div>
                          {!isCompleted(app.status) && app.job_url && (
                            <a
                              href={app.job_url}
                              target="_blank"
                              rel="noreferrer"
                              onClick={(e) => e.stopPropagation()}
                              className="inline-flex items-center gap-1 text-[10px] text-danger font-semibold hover:underline bg-danger/10 px-2 py-0.5 rounded border border-danger/25 shrink-0"
                              title="Apply manually to this job"
                            >
                              Apply Manually ↗
                            </a>
                          )}
                        </div>
                      </td>
                      <td className="px-6 py-4">
                        <span className="inline-flex items-center px-2 py-1 bg-bg-primary border border-bg-border text-text-secondary text-xs rounded-md">
                          {app.platform}
                        </span>
                      </td>
                      <td className="px-6 py-4">
                        <StatusBadge status={app.status} />
                      </td>
                      <td className="px-6 py-4">
                        {app.fit_score != null && app.ats_score != null ? (() => {
                          const avg = (app.fit_score + app.ats_score) / 2
                          const displayScore = avg <= 1 ? Math.round(avg * 100) : Math.round(avg)
                          return (
                            <div className="flex items-center gap-2">
                              <span className="font-medium text-text-primary">
                                {displayScore}%
                              </span>
                              <div className="w-16 h-1.5 bg-bg-primary rounded-full overflow-hidden">
                                <div 
                                  className="h-full bg-accent rounded-full" 
                                  style={{ width: `${displayScore}%` }}
                                />
                              </div>
                            </div>
                          )
                        })() : (
                          <span className="text-text-muted italic">Pending</span>
                        )}
                      </td>
                      <td className="px-6 py-4 text-right text-text-muted whitespace-nowrap">
                        {app.submitted_at ? formatDistanceToNow(app.submitted_at) + ' ago' : formatDistanceToNow(app.created_at) + ' ago'}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Drawer */}
      <ApplicationDrawer 
        isOpen={isDrawerOpen} 
        application={selectedApp} 
        onClose={() => setIsDrawerOpen(false)}
        onRetry={handleRetry}
      />
    </div>
  )
}
