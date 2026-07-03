'use client'

import { useQuery } from '@tanstack/react-query'
import { useParams } from 'next/navigation'
import Link from 'next/link'
import { api, type ApplicationHistoryEntry } from '@/lib/api'
import { StatusBadge } from '@/components/shared/StatusBadge'
import { formatDistanceToNow } from '@/lib/utils'
import { resolveFileUrl } from '@/components/utils'
import { ExternalLink, AlertCircle, ArrowLeft, FileText } from 'lucide-react'

export default function ApplicationDetailPage() {
  const { id } = useParams()

  const { data: application, isLoading: isAppLoading, isError: isAppError } = useQuery({
    queryKey: ['application', id],
    queryFn: () => api.getApplication(id as string),
    retry: false
  })

  const { data: job, isLoading: isJobLoading } = useQuery({
    queryKey: ['job', application?.job_id],
    queryFn: () => {
      if (!application?.job_id) throw new Error('No job ID')
      return api.getJob(application.job_id)
    },
    enabled: !!application?.job_id
  })

  const { data: candidate, isLoading: isCandLoading } = useQuery({
    queryKey: ['candidate', application?.candidate_id],
    queryFn: () => {
      if (!application?.candidate_id) throw new Error('No candidate ID')
      return api.getCandidate(application.candidate_id)
    },
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
      <div className="space-y-6 max-w-7xl mx-auto animate-pulse">
        <div className="h-8 bg-bg-border rounded w-1/3"></div>
        <div className="h-4 bg-bg-border rounded w-1/4"></div>
        <div className="h-64 bg-bg-border rounded-xl w-full mt-8"></div>
      </div>
    )
  }

  if (isAppError || !application) {
    return (
      <div className="max-w-7xl mx-auto flex flex-col items-center justify-center h-64 text-center">
        <div className="text-danger text-base font-semibold mb-2">Application not found or failed to load.</div>
        <Link href="/dashboard" className="btn-secondary text-xs inline-flex items-center gap-1.5">
          <ArrowLeft className="w-3.5 h-3.5" /> Return to Dashboard
        </Link>
      </div>
    )
  }

  return (
    <div className="space-y-6 max-w-7xl mx-auto animate-fade-in">
      <div className="border-b border-bg-border pb-6">
        <Link href={`/candidates/${application.candidate_id}`} className="text-xs text-text-muted hover:text-text-primary transition-colors inline-flex items-center gap-1 mb-3 font-medium">
          <ArrowLeft className="w-3 h-3" /> Back to Candidate: {candidate?.name || 'Loading...'}
        </Link>
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
          <div>
            <h1 className="page-title">
              {job?.canonical_url || job?.source_url ? (
                <a 
                  href={job.canonical_url || job.source_url} 
                  target="_blank" 
                  rel="noopener noreferrer" 
                  className="hover:text-accent hover:underline inline-flex items-center gap-2"
                >
                  {job.title}
                  <ExternalLink className="w-4 h-4 text-text-muted" />
                </a>
              ) : (
                job?.title || 'Unknown Position'
              )}
            </h1>
            <p className="page-subtitle">
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
          <div className="card p-6 bg-bg-card border border-bg-border rounded-xl">
            <h2 className="text-sm font-semibold text-text-primary border-b border-bg-border pb-3 mb-4">
              Application Details
            </h2>
            <div className="grid grid-cols-2 gap-4 text-sm">
              <div>
                <div className="text-text-muted mb-1 text-xs font-medium">Platform</div>
                <div className="capitalize font-semibold text-text-primary">{job?.source || '—'}</div>
              </div>
              <div>
                <div className="text-text-muted mb-1 text-xs font-medium">Job ID</div>
                <div className="truncate font-mono text-xs text-text-secondary">{job?.id || '—'}</div>
              </div>
              <div>
                <div className="text-text-muted mb-1 text-xs font-medium">Fit Score</div>
                <div className={`font-bold ${application.fit_score != null && application.fit_score >= 70 ? 'text-success' : 'text-text-primary'}`}>
                  {application.fit_score ? `${Math.round(application.fit_score)}%` : '—'}
                </div>
              </div>
              <div>
                <div className="text-text-muted mb-1 text-xs font-medium">Submitted At</div>
                <div className="text-xs text-text-secondary">{application.submitted_at ? new Date(application.submitted_at).toLocaleString() : '—'}</div>
              </div>
              <div className="col-span-2">
                <div className="text-text-muted mb-1 text-xs font-medium">Job Posting Link</div>
                <div>
                  {job?.canonical_url || job?.source_url ? (
                    <a 
                      href={job.canonical_url || job.source_url} 
                      target="_blank" 
                      rel="noopener noreferrer" 
                      className="text-accent hover:underline inline-flex items-center gap-1 font-semibold text-xs"
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
                  <p className="text-xs text-danger/90 mt-1">
                    {application.error_message || "The automated application process could not be completed. You can submit the application manually to prevent losing this opportunity."}
                  </p>
                </div>
              </div>
              {job?.source_url && (
                <a
                  href={job.source_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="btn-danger !py-2 !px-4 !text-xs"
                >
                  <ExternalLink className="w-4 h-4" />
                  Apply Manually Now ↗
                </a>
              )}
            </div>
          )}

          {(application.resume_url || application.cover_letter_url) && (
            <div className="card p-6 bg-bg-card border border-bg-border rounded-xl">
              <h2 className="text-sm font-semibold text-text-primary border-b border-bg-border pb-3 mb-4">
                Application Documents
              </h2>
              <div className="flex flex-col gap-3">
                {application.resume_url && (
                  <div className="flex items-center justify-between p-4 bg-bg-primary rounded-xl border border-bg-border">
                    <div className="flex items-center gap-3">
                      <div className="p-2.5 bg-blue-500/10 text-blue-400 rounded-lg">
                        <FileText className="w-5 h-5" />
                      </div>
                      <div>
                        <div className="text-sm font-semibold text-text-primary">Tailored Resume</div>
                        <div className="text-xs text-text-muted mt-0.5">Optimized for this specific job posting</div>
                      </div>
                    </div>
                    <a
                      href={resolveFileUrl(application.resume_url) ?? application.resume_url}
                      target="_blank"
                      rel="noreferrer"
                      className="btn-primary !py-1.5 !px-3.5 !text-xs inline-flex items-center gap-1.5"
                    >
                      View Resume ↗
                    </a>
                  </div>
                )}

                {application.cover_letter_url && (
                  <div className="flex items-center justify-between p-4 bg-bg-primary rounded-xl border border-bg-border">
                    <div className="flex items-center gap-3">
                      <div className="p-2.5 bg-purple-500/10 text-purple-400 rounded-lg">
                        <FileText className="w-5 h-5" />
                      </div>
                      <div>
                        <div className="text-sm font-semibold text-text-primary">Tailored Cover Letter</div>
                        <div className="text-xs text-text-muted mt-0.5">Custom letter generated for this job</div>
                      </div>
                    </div>
                    <a
                      href={resolveFileUrl(application.cover_letter_url) ?? application.cover_letter_url}
                      target="_blank"
                      rel="noreferrer"
                      className="btn-secondary !py-1.5 !px-3.5 !text-xs inline-flex items-center gap-1.5"
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
          <div className="card p-6 bg-bg-card border border-bg-border rounded-xl">
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
                    <div className="absolute -left-1.5 mt-1.5 w-3 h-3 rounded-full bg-accent border-2 border-bg-card" />
                    <div className="flex items-center gap-2 flex-wrap">
                      {entry.from_status && (
                        <>
                          <span className="text-xs text-text-muted">{entry.from_status.replace(/_/g, ' ')}</span>
                          <span className="text-text-muted text-xs">→</span>
                        </>
                      )}
                      <span className="text-xs font-semibold text-text-primary">{entry.to_status.replace(/_/g, ' ')}</span>
                    </div>
                    <time className="text-[10px] text-text-muted mt-0.5 block">
                      {formatDistanceToNow(entry.created_at)} ago
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
