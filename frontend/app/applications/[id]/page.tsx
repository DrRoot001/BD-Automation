'use client'

import { useQuery } from '@tanstack/react-query'
import { useParams } from 'next/navigation'
import Link from 'next/link'
import { api, type ApplicationHistoryEntry } from '@/lib/api'
import { clsx } from 'clsx'
import { formatDistanceToNow, resolveFileUrl } from '@/components/utils'
import { ExternalLink, AlertCircle } from 'lucide-react'

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

function StatusBadge({ status }: { status: string }) {
  const s = status?.toLowerCase() || ''
  const map: Record<string, string> = {
    queued:       'badge-queued',
    found:        'badge-found',
    submitted:    'badge-submitted',
    confirmed:    'badge-confirmed',
    interview_r1:   'badge-interview_r1',
    interview_r2:   'badge-interview_r2',
    interview_r3:   'badge-interview_r1',
    interview_r4:   'badge-interview_r1',
    failed:         'badge-failed',
    blocked:      'badge-blocked',
    rejected:     'badge-rejected',
    offer:        'badge-offer',
    assessment:   'badge-interview_r1',
  }
  return (
    <span className={clsx('badge text-sm px-3 py-1', map[s] ?? 'badge-found')}>
      {status?.replace('_', ' ')}
    </span>
  )
}

export default function ApplicationDetailPage() {
  const { id } = useParams()

  const { data: application, isLoading: isAppLoading, isError: isAppError } = useQuery({
    queryKey: ['application', id],
    queryFn: () => api.getApplication(id as string),
    retry: false
  })

  const { data: job, isLoading: isJobLoading } = useQuery({
    queryKey: ['job', application?.job_id],
    queryFn: () => api.getJob(application.job_id),
    enabled: !!application?.job_id
  })

  const { data: candidate, isLoading: isCandLoading } = useQuery({
    queryKey: ['candidate', application?.candidate_id],
    queryFn: () => api.getCandidate(application.candidate_id),
    enabled: !!application?.candidate_id
  })

  const { data: history = [] } = useQuery({
    queryKey: ['application-history', id],
    queryFn: () => api.getApplicationHistory(id as string),
    enabled: !!application,
  })

  const isLoading = isAppLoading || isJobLoading || isCandLoading

  if (isLoading) {
    return (
      <div className="p-8 animate-pulse max-w-screen-xl mx-auto space-y-6">
        <div className="h-8 bg-bg-secondary rounded w-1/3"></div>
        <div className="h-4 bg-bg-secondary rounded w-1/4"></div>
        <div className="h-64 bg-bg-secondary rounded w-full mt-8"></div>
      </div>
    )
  }

  if (isAppError || !application) {
    return (
      <div className="p-8 max-w-screen-xl mx-auto flex flex-col items-center justify-center h-64 text-center">
        <div className="text-danger text-lg mb-2">Application not found or failed to load.</div>
        <Link href="/dashboard" className="text-accent hover:underline">
          Return to Dashboard
        </Link>
      </div>
    )
  }

  return (
    <div className="p-8 max-w-screen-xl mx-auto animate-fade-in">
      <div className="mb-6">
        <Link href={`/candidates/${application.candidate_id}`} className="text-xs text-text-muted hover:text-text-primary transition-colors inline-block mb-4">
          ← Back to Candidate: {candidate?.name || 'Loading...'}
        </Link>
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold text-text-primary">
              {job?.canonical_url || job?.source_url ? (
                <a 
                  href={job.canonical_url || job.source_url} 
                  target="_blank" 
                  rel="noopener noreferrer" 
                  className="hover:text-accent hover:underline inline-flex items-center gap-2"
                >
                  {job.title}
                  <ExternalLink className="w-5 h-5 text-text-muted" />
                </a>
              ) : (
                job?.title || 'Unknown Position'
              )}
            </h1>
            <p className="text-text-secondary text-base mt-1">
              {job?.company || 'Unknown Company'}
            </p>
          </div>
          <div className="flex items-center gap-4">
            <StatusBadge status={application.status} />
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Left Column: Details & Assets */}
        <div className="lg:col-span-2 space-y-6">
          <div className="card p-6">
            <h2 className="text-sm font-semibold text-text-primary border-b border-bg-border pb-3 mb-4">
              Application Details
            </h2>
            <div className="grid grid-cols-2 gap-4 text-sm">
              <div>
                <div className="text-text-muted mb-1 text-xs">Platform</div>
                <div className="capitalize">{job?.source || '—'}</div>
              </div>
              <div>
                <div className="text-text-muted mb-1 text-xs">Job ID</div>
                <div className="truncate font-mono text-xs">{job?.id || '—'}</div>
              </div>
              <div>
                <div className="text-text-muted mb-1 text-xs">Fit Score</div>
                <div className={clsx(
                  "font-medium",
                  application.fit_score >= 80 ? 'text-success' : application.fit_score >= 60 ? 'text-warning' : 'text-text-primary'
                )}>
                  {application.fit_score ? `${application.fit_score.toFixed(0)}%` : '—'}
                </div>
              </div>
              <div>
                <div className="text-text-muted mb-1 text-xs">Submitted At</div>
                <div>{application.submitted_at ? new Date(application.submitted_at).toLocaleString() : '—'}</div>
              </div>
              <div className="col-span-2">
                <div className="text-text-muted mb-1 text-xs">Job Posting Link</div>
                <div>
                  {job?.canonical_url || job?.source_url ? (
                    <a 
                      href={job.canonical_url || job.source_url} 
                      target="_blank" 
                      rel="noopener noreferrer" 
                      className="text-accent hover:underline inline-flex items-center gap-1 font-semibold"
                    >
                      View Original Job Description ↗
                    </a>
                  ) : (
                    '—'
                  )}
                </div>
              </div>
            </div>
          </div>

          {(application.status === 'FAILED' || application.status === 'BLOCKED') && (
            <div className="bg-danger/10 border border-danger/20 rounded-xl p-6 space-y-4">
              <div className="flex items-start gap-2">
                <AlertCircle className="w-5 h-5 text-danger shrink-0 mt-0.5" />
                <div>
                  <h2 className="text-sm font-semibold text-danger">Application Incomplete / Failed</h2>
                  <p className="text-xs text-danger/80 mt-1">
                    {application.error_message || "The automated application process could not be completed. You can submit the application manually to prevent losing this opportunity."}
                  </p>
                </div>
              </div>
              {job?.source_url && (
                <a
                  href={job.source_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1.5 text-xs font-semibold text-white bg-danger hover:bg-danger/90 px-4 py-2 rounded-lg transition-colors shadow-sm"
                >
                  <ExternalLink className="w-4 h-4" />
                  Apply Manually Now ↗
                </a>
              )}
            </div>
          )}

          {application.screenshot_url && (
            <div className="card p-6">
              <h2 className="text-sm font-semibold text-text-primary border-b border-bg-border pb-3 mb-4">
                Submission Evidence
              </h2>
              <div className="border border-bg-border rounded-lg overflow-hidden bg-bg-primary">
                <img src={application.screenshot_url} alt="Application Confirmation" className="w-full h-auto" />
              </div>
            </div>
          )}
          
          {(application.resume_id || application.cover_letter_url) && (
            <div className="card p-6">
              <h2 className="text-sm font-semibold text-text-primary border-b border-bg-border pb-3 mb-4">
                Application Documents
              </h2>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                {application.resume_id && (
                  <div className="flex items-center justify-between p-4 bg-bg-primary rounded-xl border border-bg-border">
                    <div>
                      <div className="text-sm font-semibold text-text-primary">Tailored Resume</div>
                      <div className="text-xs text-text-muted mt-0.5">Optimized for this job</div>
                    </div>
                    <a
                      href={`/api/resumes/${application.resume_id}/view`}
                      target="_blank"
                      rel="noreferrer"
                      className="text-xs bg-text-primary text-bg-card px-3.5 py-2 rounded-lg font-semibold hover:opacity-90 transition-opacity"
                    >
                      View Resume ↗
                    </a>
                  </div>
                )}
                {application.cover_letter_url && (
                  <div className="flex items-center justify-between p-4 bg-bg-primary rounded-xl border border-bg-border">
                    <div>
                      <div className="text-sm font-semibold text-text-primary">Cover Letter</div>
                      <div className="text-xs text-text-muted mt-0.5">AI custom cover letter</div>
                    </div>
                    <a
                      href={resolveFileUrl(application.cover_letter_url) ?? application.cover_letter_url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-xs bg-text-primary text-bg-card px-3.5 py-2 rounded-lg font-semibold hover:opacity-90 transition-opacity"
                    >
                      View Cover Letter ↗
                    </a>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>

        {/* Right Column: Timeline / History */}
        <div className="space-y-6">
          <div className="card p-6">
            <h2 className="text-sm font-semibold text-text-primary border-b border-bg-border pb-3 mb-4">
              Status Timeline
            </h2>
            {history.length === 0 ? (
              <div className="text-xs text-text-muted text-center py-8">
                No status history recorded yet.
              </div>
            ) : (
              <ol className="relative border-l border-bg-border ml-2 space-y-4">
                {history.map((entry: ApplicationHistoryEntry) => (
                  <li key={entry.id} className="ml-4">
                    <div className="absolute -left-1.5 mt-1.5 w-3 h-3 rounded-full bg-accent border-2 border-bg-secondary" />
                    <div className="flex items-center gap-2 flex-wrap">
                      {entry.from_status && (
                        <>
                          <span className="text-xs text-text-muted">{entry.from_status.replace('_', ' ')}</span>
                          <span className="text-text-muted text-xs">→</span>
                        </>
                      )}
                      <span className="text-xs font-medium text-text-primary">{entry.to_status.replace('_', ' ')}</span>
                    </div>
                    <time className="text-[10px] text-text-muted mt-0.5 block">
                      {formatDistanceToNow(entry.created_at)}
                    </time>
                  </li>
                ))}
              </ol>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
