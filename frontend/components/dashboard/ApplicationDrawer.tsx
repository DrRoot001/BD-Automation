import { useState, useEffect } from 'react'
import { createPortal } from 'react-dom'
import { X, FileText, ExternalLink, RefreshCw, AlertCircle, Clock, CheckCircle, FileSignature } from 'lucide-react'
import { ApplicationSummary } from '@/lib/api'
import { useApplicationHistory } from '@/hooks/useApplicationHistory'
import { useJob } from '@/hooks/useJob'
import { StatusBadge } from '@/components/shared/StatusBadge'
import { formatDistanceToNow } from '@/lib/utils'
import { formatJobUrl } from '@/components/utils'
import { FAILURE_INFO } from '@/lib/failureReasons'

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

// Timeline entries carry machine-oriented meta_data (screening answers, raw
// errors, scores). Surface only the parts that read as plain English.
function describeMeta(meta: Record<string, unknown> | null | undefined): string | null {
  if (!meta || Object.keys(meta).length === 0) return null
  const parts: string[] = []
  if (typeof meta.info === 'string') parts.push(meta.info)
  if (typeof meta.ats_score_before === 'number' && typeof meta.ats_score_after === 'number') {
    const before = Math.round(meta.ats_score_before)
    const after = Math.round(meta.ats_score_after)
    parts.push(before === after ? `ATS score: ${after}` : `ATS score improved ${before} → ${after}`)
  }
  if (typeof meta.explanation === 'string') {
    const text = meta.explanation.length > 220 ? meta.explanation.slice(0, 220) + '…' : meta.explanation
    parts.push(text)
  }
  if (parts.length === 0 && typeof meta.error_message === 'string') {
    const text = meta.error_message.length > 160 ? meta.error_message.slice(0, 160) + '…' : meta.error_message
    parts.push(text)
  }
  return parts.length > 0 ? parts.join(' · ') : null
}

interface ApplicationDrawerProps {
  application: ApplicationSummary | null
  isOpen: boolean
  onClose: () => void
  onRetry: (applicationId: string) => void
}

export function ApplicationDrawer({ application, isOpen, onClose, onRetry }: ApplicationDrawerProps) {
  const { data: history, isLoading: historyLoading } = useApplicationHistory(application?.application_id || null)
  const { data: job, isLoading: jobLoading } = useJob(application?.job_id || null)

  // Prevent background scroll and support Escape key when open
  useEffect(() => {
    if (isOpen) document.body.style.overflow = 'hidden'
    else document.body.style.overflow = 'unset'

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && isOpen) onClose()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.body.style.overflow = 'unset'
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [isOpen, onClose])

  if (!isOpen || !application) return null

  return createPortal(
    <div
      className="fixed inset-0 z-[9999] flex justify-end bg-black/60 backdrop-blur-sm"
      onClick={onClose}
    >
      <div 
        className="w-full max-w-2xl bg-bg-secondary h-full shadow-2xl flex flex-col animate-in slide-in-from-right duration-300 border-l border-bg-border"
        onClick={e => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between p-6 border-b border-bg-border shrink-0">
          <div>
            <h2 className="text-xl font-bold text-text-primary line-clamp-1">{application.job_title}</h2>
            <div className="text-sm text-text-muted mt-1">{application.company} • {application.platform}</div>
          </div>
          <button 
            onClick={onClose}
            className="p-2 bg-bg-primary hover:bg-bg-hover text-text-muted hover:text-text-primary rounded-full transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-6 space-y-8">
          
          {/* Status & Actions */}
          <div className="flex items-start justify-between bg-bg-primary p-5 rounded-xl border border-bg-border">
            <div>
              <div className="text-xs text-text-muted uppercase tracking-wider font-semibold mb-2">Current Status</div>
              <StatusBadge status={application.status} />
              
              {(application.status === 'FAILED' || application.status === 'BLOCKED') && (
                <div className="mt-3 text-sm text-danger flex flex-col gap-2 bg-danger/10 p-3 rounded-lg border border-danger/20">
                  <div className="flex items-start gap-2">
                    <AlertCircle className="w-4.5 h-4.5 shrink-0 mt-0.5" />
                    <span className="font-semibold">
                      {FAILURE_INFO[application.failure_reason ?? '']?.title ?? 'Application Incomplete / Failed'}
                    </span>
                  </div>
                  <span className="text-xs text-danger/80">
                    {FAILURE_INFO[application.failure_reason ?? '']?.description
                      ?? (application.error_message || 'The automated application process could not be completed. You can submit the application manually to prevent losing this opportunity.')
                    }
                  </span>
                  {application.failure_reason !== 'JOB_EXPIRED' && application.job_url && (
                    <a
                      href={formatJobUrl(application.job_url)}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1.5 text-xs font-semibold text-white bg-danger hover:bg-danger/90 px-3 py-1.5 rounded-lg w-max transition-colors mt-1 shadow-sm"
                    >
                      <ExternalLink className="w-3.5 h-3.5" />
                      Apply Manually ↗
                    </a>
                  )}
                </div>
              )}
            </div>
            
            {(application.status === 'FAILED' || application.status === 'BLOCKED') && application.failure_reason !== 'ROBOTS_BLOCKED' && (
              <button 
                onClick={() => onRetry(application.application_id)}
                className="flex items-center gap-2 px-4 py-2 bg-accent hover:bg-accent-hover text-white rounded-lg text-sm font-medium transition-colors"
              >
                <RefreshCw className="w-4 h-4" />
                Retry
              </button>
            )}
          </div>

          {/* Scores Breakdown */}
          <div>
            <h3 className="text-sm font-semibold text-text-primary uppercase tracking-wider mb-4 flex items-center gap-2">
              Match Scores
            </h3>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
              <ScoreCard label="Fit Score" score={application.fit_score} color="bg-blue-500" />
              <ScoreCard label="ATS Score" score={application.ats_score} color="bg-purple-500" />
              <ScoreCard label="Combined" score={application.fit_score && application.ats_score ? (application.fit_score + application.ats_score)/2 : null} color="bg-accent" />
            </div>
          </div>

          {/* Documents */}
          {(application.resume_url || application.cover_letter_url || application.job_url) && (
            <div>
              <h3 className="text-sm font-semibold text-text-primary uppercase tracking-wider mb-4">Documents & Links</h3>
              <div className="flex flex-col gap-3">
                {application.resume_url && (
                  <a href={application.resume_url} target="_blank" rel="noreferrer" className="flex items-center justify-between p-3 bg-bg-primary border border-bg-border rounded-lg hover:border-accent/50 transition-colors group">
                    <div className="flex items-center gap-3">
                      <div className="p-2 bg-info/10 text-info rounded-md">
                        <FileText className="w-4 h-4" />
                      </div>
                      <span className="text-sm text-text-primary font-medium">Tailored Resume</span>
                    </div>
                    <ExternalLink className="w-4 h-4 text-text-muted group-hover:text-accent transition-colors" />
                  </a>
                )}
                
                {application.cover_letter_url && (
                  <a href={application.cover_letter_url} target="_blank" rel="noreferrer" className="flex items-center justify-between p-3 bg-bg-primary border border-bg-border rounded-lg hover:border-accent/50 transition-colors group">
                    <div className="flex items-center gap-3">
                      <div className="p-2 bg-purple/10 text-purple rounded-md">
                        <FileText className="w-4 h-4" />
                      </div>
                      <span className="text-sm text-text-primary font-medium">Tailored Cover Letter</span>
                    </div>
                    <ExternalLink className="w-4 h-4 text-text-muted group-hover:text-accent transition-colors" />
                  </a>
                )}
                
                {application.job_url && (
                  <a href={formatJobUrl(application.job_url)} target="_blank" rel="noreferrer" className="flex items-center justify-between p-3 bg-bg-primary border border-bg-border rounded-lg hover:border-accent/50 transition-colors group">
                    <div className="flex items-center gap-3">
                      <div className="p-2 bg-purple/10 text-purple rounded-md">
                        <ExternalLink className="w-4 h-4" />
                      </div>
                      <span className="text-sm text-text-primary font-medium">Original Job Posting</span>
                    </div>
                    <ExternalLink className="w-4 h-4 text-text-muted group-hover:text-accent transition-colors" />
                  </a>
                )}
              </div>
            </div>
          )}

          {/* Job Description */}
          <div>
            <h3 className="text-sm font-semibold text-text-primary uppercase tracking-wider mb-4">Job Description</h3>
            <div className="bg-bg-primary p-5 rounded-xl border border-bg-border max-h-64 overflow-y-auto">
              {jobLoading ? (
                <div className="space-y-2 animate-pulse">
                  <div className="h-4 bg-bg-border rounded w-full"></div>
                  <div className="h-4 bg-bg-border rounded w-5/6"></div>
                  <div className="h-4 bg-bg-border rounded w-4/6"></div>
                </div>
              ) : job ? (
                <div className="text-sm text-text-secondary whitespace-pre-wrap leading-relaxed">
                  {job.description || 'No description available.'}
                </div>
              ) : (
                <div className="text-sm text-text-muted">Failed to load job description.</div>
              )}
            </div>
          </div>

          {/* Timeline */}
          <div>
            <h3 className="text-sm font-semibold text-text-primary uppercase tracking-wider mb-4">Status History</h3>
            <div className="bg-bg-primary p-5 rounded-xl border border-bg-border">
              {historyLoading ? (
                <div className="space-y-4 animate-pulse">
                  <div className="h-4 bg-bg-border rounded w-1/3"></div>
                  <div className="h-4 bg-bg-border rounded w-1/4"></div>
                </div>
              ) : history && history.length > 0 ? (
                <div className="space-y-6">
                  {history.map((event, i) => (
                    <div key={event.id} className="relative flex gap-4">
                      {/* Line connecting nodes */}
                      {i !== history.length - 1 && (
                        <div className="absolute top-6 left-2.5 w-px h-full -ml-px bg-bg-border"></div>
                      )}
                      <div className="relative shrink-0 mt-1">
                        <div className="w-5 h-5 rounded-full bg-bg-secondary border-2 border-accent flex items-center justify-center">
                          <CheckCircle className="w-3 h-3 text-accent" />
                        </div>
                      </div>
                      <div>
                        <div className="text-sm font-medium text-text-primary">{event.to_status}</div>
                        <div className="text-xs text-text-muted mt-0.5 flex items-center gap-1.5">
                          <Clock className="w-3 h-3" />
                          {formatDistanceToNow(event.created_at)} ago
                        </div>
                        {describeMeta(event.meta_data) && (
                          <div className="mt-2 text-xs text-text-secondary bg-bg-secondary p-2 rounded border border-bg-border">
                            {describeMeta(event.meta_data)}
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-sm text-text-muted">No history found.</div>
              )}
            </div>
          </div>

        </div>
      </div>
    </div>,
    document.body
  )
}

function ScoreCard({ label, score, color }: { label: string, score: number | null, color: string }) {
  if (score == null) return null
  
  // Assuming score is 0-100. If 0-1, multiply by 100.
  const displayScore = score <= 1 ? Math.round(score * 100) : Math.round(score)
  
  return (
    <div className="bg-bg-primary p-4 rounded-xl border border-bg-border">
      <div className="text-xs text-text-muted font-medium mb-2">{label}</div>
      <div className="flex items-end gap-2">
        <span className="text-2xl font-bold text-text-primary">{displayScore}%</span>
      </div>
      <div className="w-full h-1.5 bg-bg-secondary rounded-full mt-3 overflow-hidden">
        <div className={`h-full ${color} rounded-full`} style={{ width: `${displayScore}%` }}></div>
      </div>
    </div>
  )
}
