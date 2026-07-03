'use client'

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type ApplicationSummary } from '@/lib/api'
import { clsx } from 'clsx'
import { formatDistanceToNow, resolveFileUrl } from '../utils'
import { useRouter } from 'next/navigation'
import { ExternalLink, FileText, ChevronLeft, ChevronRight, Loader2 } from 'lucide-react'
import { useWebSocket } from '@/hooks/useWebSocket'
import { useQueryClient } from '@tanstack/react-query'

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

const PAGE_SIZE = 15

function StatusBadge({ status }: { status: string }) {
  const s = status.toLowerCase()
  const map: Record<string, string> = {
    queued:         'badge-queued',
    found:          'badge-found',
    analyzed:       'badge-found',
    matched:        'badge-found',
    submitted:      'badge-submitted',
    confirmed:      'badge-confirmed',
    interview_r1:   'badge-interview_r1',
    interview_r2:   'badge-interview_r2',
    interview_r3:   'badge-interview_r1',
    interview_r4:   'badge-interview_r1',
    failed:         'badge-failed',
    blocked:        'badge-blocked',
    rejected:       'badge-rejected',
    offer:          'badge-offer',
    assessment:     'badge-interview_r1',
  }
  return (
    <span className={clsx('badge', map[s] ?? 'badge-found')}>
      {status.replace(/_/g, ' ')}
    </span>
  )
}

function TableSkeleton() {
  return (
    <div className="p-4 space-y-2">
      {Array.from({ length: 6 }).map((_, i) => (
        <div key={i} className="skeleton h-12 rounded" />
      ))}
    </div>
  )
}

interface ApplicationsQueueProps {
  candidateId?: string
  statusFilter?: string[]
  emptyMessage?: string
}

export function ApplicationsQueue({ candidateId, statusFilter, emptyMessage }: ApplicationsQueueProps = {}) {
  const router = useRouter()
  const queryClient = useQueryClient()
  const [page, setPage] = useState(0)
  const [pipelineState, setPipelineState] = useState<{ step: string; message: string; ts: Date } | null>(null)

  useWebSocket((evt) => {
    if (evt.event === 'pipeline.progress') {
      const step = String(evt.data?.step)
      const message = String(evt.data?.message)
      
      // Only show progress for our candidate's pipeline
      const evtCandidateId = evt.data?.candidate_id || evt.data?.candidateId
      if (evtCandidateId && candidateId && evtCandidateId !== candidateId) return
      
      setPipelineState({ step, message, ts: new Date() })
      
      // If it's a terminal step for the background matcher, hide the status after a delay
      if (step === 'matches_found' || step === 'no_matches' || step === 'done') {
        if (step !== 'matches_found') {
          setTimeout(() => setPipelineState(null), 5000)
        }
        queryClient.invalidateQueries({ queryKey: ['applications'] })
        queryClient.invalidateQueries({ queryKey: ['kpis'] })
      }
    } else if (evt.event === 'application.created') {
      // Only refetch if the event is for our candidate
      const evtCandidateId = evt.data?.candidate_id || evt.data?.candidateId
      if (!evtCandidateId || !candidateId || evtCandidateId === candidateId) {
        queryClient.invalidateQueries({ queryKey: ['applications'] })
        queryClient.invalidateQueries({ queryKey: ['kpis'] })
      }
    } else if (evt.event === 'application.status_changed') {
      // Only refetch if the event is for our candidate
      const evtCandidateId = evt.data?.candidate_id || evt.data?.candidateId
      if (!evtCandidateId || !candidateId || evtCandidateId === candidateId) {
        queryClient.invalidateQueries({ queryKey: ['applications'] })
        queryClient.invalidateQueries({ queryKey: ['kpis'] })
      }
    }
  })

  const { data, isLoading, isError } = useQuery({
    queryKey: ['applications', candidateId, page],
    queryFn: () => api.getApplications({ limit: PAGE_SIZE, offset: page * PAGE_SIZE, candidateId }),
    refetchInterval: 30_000,
  })

  const all = data ?? []
  const apps = statusFilter
    ? all.filter((a) => statusFilter.includes(a.status.toUpperCase()))
    : all

  const hasPrev = page > 0
  const hasNext = all.length === PAGE_SIZE

  return (
    <div className="card">
      <div className="card-header">
        <div className="flex items-center gap-2.5">
          <h2 className="card-title">Application Queue</h2>
          {!isLoading && (
            <span className="text-xs text-text-muted bg-bg-secondary px-2.5 py-0.5 rounded-full border border-bg-border font-medium">
              {apps.length} shown
            </span>
          )}
        </div>
        <div className="flex items-center gap-3">
          {pipelineState && (
            <div className="flex items-center gap-2 text-xs font-medium text-accent animate-pulse bg-accent/10 px-3 py-1 rounded-full border border-accent/20">
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
              <span>{pipelineState.message}</span>
            </div>
          )}
          <div className="flex items-center gap-1.5 text-xs text-text-muted font-medium" title="Auto-refreshes every 30 seconds">
            <span className="live-dot" />
            Live
          </div>
        </div>
      </div>

      <div className="overflow-x-auto">
        {isLoading ? (
          <TableSkeleton />
        ) : isError ? (
          <div className="p-8 text-center text-danger text-sm">Failed to load applications</div>
        ) : apps.length === 0 ? (
          <div className="p-10 text-center text-text-muted text-sm">
            {emptyMessage ?? 'No applications yet. Start by running the job discovery pipeline.'}
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-bg-border text-text-muted text-xs">
                <th className="text-left px-5 py-2.5 font-medium">Position</th>
                <th className="text-left px-3 py-2.5 font-medium hidden sm:table-cell">Platform</th>
                <th className="text-left px-3 py-2.5 font-medium">Status</th>
                <th className="text-left px-3 py-2.5 font-medium hidden lg:table-cell">Resume</th>
                <th className="text-left px-3 py-2.5 font-medium hidden lg:table-cell">JD</th>
                <th className="text-right px-3 py-2.5 font-medium hidden md:table-cell">Fit</th>
                <th className="text-right px-3 py-2.5 font-medium hidden md:table-cell">Submitted</th>
                <th className="text-right px-5 py-2.5 font-medium">Added</th>
              </tr>
            </thead>
            <tbody>
              {apps.map((app) => (
                <tr
                  key={app.application_id}
                  onClick={() => router.push(`/applications/${app.application_id}`)}
                  className="table-row-hover border-b border-bg-border/40 last:border-0 cursor-pointer"
                >
                  <td className="px-5 py-3">
                    <div className="font-medium text-text-primary truncate max-w-[180px] text-sm">
                      {app.job_title}
                    </div>
                    <div className="text-text-muted text-xs truncate max-w-[180px] mt-0.5">
                      {app.company}
                    </div>
                    {app.error_message && (
                      <div className="text-danger text-[10px] mt-1 max-w-[220px] truncate" title={app.error_message}>
                        {app.error_message}
                      </div>
                    )}
                  </td>

                  <td className="px-3 py-3 hidden sm:table-cell">
                    <span className="text-xs text-text-secondary capitalize">{app.platform}</span>
                  </td>

                  <td className="px-3 py-3">
                    <StatusBadge status={app.status} />
                  </td>

                  {/* Resume link + base/tailored badge */}
                  <td className="px-3 py-3 hidden lg:table-cell" onClick={(e) => e.stopPropagation()}>
                    {resolveFileUrl(app.resume_url) ? (
                      <div className="flex flex-col items-start gap-1">
                        <a
                          href={resolveFileUrl(app.resume_url)!}
                          target="_blank"
                          rel="noreferrer"
                          className="inline-flex items-center gap-1 text-xs text-accent hover:underline"
                          title="View resume"
                        >
                          <FileText className="w-3.5 h-3.5" />
                          Resume
                        </a>
                        {app.resume_is_base === false ? (
                          <span
                            className="text-[10px] px-1.5 py-0.5 rounded bg-accent/10 text-accent font-medium"
                            title="A resume tailored to this job was used"
                          >
                            Tailored
                          </span>
                        ) : app.resume_is_base === true ? (
                          <span
                            className="text-[10px] px-1.5 py-0.5 rounded bg-bg-secondary text-text-muted border border-bg-border font-medium"
                            title="The candidate's base resume was used (no tailoring)"
                          >
                            Base
                          </span>
                        ) : null}
                      </div>
                    ) : (
                      <span className="text-text-muted text-xs">—</span>
                    )}
                  </td>

                  {/* JD link */}
                  <td className="px-3 py-3 hidden lg:table-cell" onClick={(e) => e.stopPropagation()}>
                    {!isCompleted(app.status) && app.job_url ? (
                      <a
                        href={app.job_url}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex items-center gap-1 text-xs text-danger font-semibold hover:underline"
                        title="Apply manually to this job"
                      >
                        <ExternalLink className="w-3.5 h-3.5" />
                        Apply Manually ↗
                      </a>
                    ) : app.job_url ? (
                      <a
                        href={app.job_url}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex items-center gap-1 text-xs text-accent hover:underline"
                        title="View job description"
                      >
                        <ExternalLink className="w-3.5 h-3.5" />
                        View JD
                      </a>
                    ) : (
                      <span className="text-text-muted text-xs">—</span>
                    )}
                  </td>

                  <td className="px-3 py-3 text-right hidden md:table-cell">
                    {app.ats_score_after != null
                      && app.ats_score_before != null
                      && app.ats_score_after !== app.ats_score_before ? (
                      // Tailoring changed the ATS score — show base → tailored.
                      <span
                        className="inline-flex items-center gap-1 text-xs font-medium tabular-nums whitespace-nowrap"
                        title="ATS score vs job description: base resume → tailored resume"
                      >
                        <span className="text-text-muted line-through decoration-text-muted/40">
                          {app.ats_score_before.toFixed(0)}
                        </span>
                        <span className="text-text-muted">→</span>
                        <span
                          className={clsx(
                            app.ats_score_after >= 80 ? 'text-success'
                            : app.ats_score_after >= 60 ? 'text-warning'
                            : 'text-text-muted',
                          )}
                        >
                          {app.ats_score_after.toFixed(0)}%
                        </span>
                      </span>
                    ) : (app.fit_score != null || app.ats_score != null) ? (() => {
                      const displayScore = app.fit_score ?? app.ats_score!
                      return (
                        <span
                          className={clsx(
                            'text-xs font-medium tabular-nums',
                            displayScore >= 80 ? 'text-success'
                            : displayScore >= 60 ? 'text-warning'
                            : 'text-text-muted',
                          )}
                        >
                          {displayScore.toFixed(0)}%
                        </span>
                      )
                    })() : (
                      <span className="text-text-muted text-xs">—</span>
                    )}
                  </td>

                  <td className="px-3 py-3 text-right hidden md:table-cell text-xs text-text-muted whitespace-nowrap">
                    {app.submitted_at
                      ? new Date(app.submitted_at).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' })
                      : '—'}
                  </td>

                  <td className="px-5 py-3 text-right text-xs text-text-muted whitespace-nowrap">
                    {formatDistanceToNow(app.created_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Pagination */}
      {!isLoading && !isError && (hasPrev || hasNext) && (
        <div className="flex items-center justify-between px-5 py-3 border-t border-bg-border text-xs text-text-muted">
          <span>Page {page + 1}</span>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              disabled={!hasPrev}
              className="p-1.5 rounded hover:bg-bg-hover disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
            >
              <ChevronLeft className="w-4 h-4" />
            </button>
            <button
              onClick={() => setPage((p) => p + 1)}
              disabled={!hasNext}
              className="p-1.5 rounded hover:bg-bg-hover disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
            >
              <ChevronRight className="w-4 h-4" />
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
