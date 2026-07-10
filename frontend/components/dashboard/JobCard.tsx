import { type JobSummary } from '@/lib/api'
import { formatSalary, formatDistanceToNow } from '@/lib/utils'
import { Building2, MapPin, DollarSign, Clock, Send, XCircle, Loader2, RotateCcw } from 'lucide-react'
import { StatusBadge } from '@/components/shared/StatusBadge'

export interface JobCardProps {
  job: JobSummary
  onApply: (jobId: string) => void
  onDismiss: (jobId: string) => void
  onClick: (job: JobSummary) => void
  applicationStatus?: string
  /** True while the apply pipeline is being queued for this job */
  applyPending?: boolean
  /** Card is rendered inside the "show dismissed" reveal — offer Undo instead */
  isDismissed?: boolean
  onRestore?: (jobId: string) => void
}

export function JobCard({ job, onApply, onDismiss, onClick, applicationStatus, applyPending, isDismissed, onRestore }: JobCardProps) {
  const salaryString = formatSalary(job.salary_min, job.salary_max, job.pay_period)

  return (
    <div
      className={`card p-5 hover:border-accent/50 transition-all cursor-pointer group flex flex-col bg-bg-card border border-bg-border rounded-xl ${isDismissed ? 'opacity-60 hover:opacity-100' : ''}`}
      onClick={() => onClick(job)}
    >
      {/* Header */}
      <div className="flex items-start justify-between mb-3 gap-2">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-text-primary group-hover:text-accent transition-colors line-clamp-2">
            {job.title}
          </h3>
          <div className="flex items-center gap-1.5 mt-1 text-xs text-text-secondary">
            <Building2 className="w-3.5 h-3.5 text-text-muted shrink-0" />
            <span className="truncate">{job.company}</span>
          </div>
        </div>
        <span className="shrink-0 text-[10px] font-semibold uppercase tracking-wider text-text-muted bg-bg-secondary px-2 py-0.5 rounded border border-bg-border">
          {job.source}
        </span>
      </div>

      {/* Meta Info */}
      <div className="flex flex-wrap gap-3 mt-auto mb-4 text-xs text-text-secondary">
        <div className="flex items-center gap-1">
          <MapPin className="w-3.5 h-3.5 text-text-muted" />
          <span className="truncate max-w-[120px]">{job.location || 'Remote'}</span>
        </div>
        <div className="flex items-center gap-1">
          <DollarSign className="w-3.5 h-3.5 text-text-muted" />
          <span>{salaryString}</span>
        </div>
        {job.job_type && (
          <div className="flex items-center gap-1">
            <Clock className="w-3.5 h-3.5 text-text-muted" />
            <span className="capitalize">{job.job_type.replace('-', ' ')}</span>
          </div>
        )}
      </div>

      {/* Footer / Actions */}
      <div className="flex items-center justify-between pt-3 border-t border-bg-border mt-auto">
        <div className="text-[11px] text-text-muted">
          {job.posted_at ? formatDistanceToNow(job.posted_at) + ' ago' : 'Recently discovered'}
        </div>
        <div className="flex items-center gap-1.5" onClick={e => e.stopPropagation()}>
          {isDismissed ? (
            <button
              onClick={() => onRestore?.(job.id)}
              className="btn-secondary !py-1 !px-3 !text-xs"
              title="Restore this job to the feed"
              aria-label="Undo dismiss"
            >
              <RotateCcw className="w-3 h-3" />
              Undo
            </button>
          ) : (
            <>
              <button
                onClick={() => onDismiss(job.id)}
                className="p-1.5 text-text-muted hover:text-danger hover:bg-danger/10 rounded-lg transition-colors"
                title="Dismiss Job"
                aria-label="Dismiss job"
              >
                <XCircle className="w-4 h-4" />
              </button>
              {applicationStatus ? (
                <StatusBadge status={applicationStatus} />
              ) : (
                <button
                  onClick={() => onApply(job.id)}
                  disabled={applyPending}
                  className="btn-primary !py-1 !px-3 !text-xs disabled:opacity-60 disabled:cursor-not-allowed"
                  aria-busy={applyPending}
                >
                  {applyPending ? <Loader2 className="w-3 h-3 animate-spin" /> : <Send className="w-3 h-3" />}
                  {applyPending ? 'Queuing…' : 'Apply'}
                </button>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
