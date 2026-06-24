import { type JobSummary } from '@/lib/api'
import { formatSalary } from '@/lib/utils'
import { formatDistanceToNow } from '@/components/utils'
import { ExternalLink, Building2, MapPin, DollarSign, Clock, Send, XCircle } from 'lucide-react'

export interface JobCardProps {
  job: JobSummary
  onApply: (jobId: string) => void
  onDismiss: (jobId: string) => void
  onClick: (jobId: string) => void
  isApplied?: boolean
}

export function JobCard({ job, onApply, onDismiss, onClick, isApplied }: JobCardProps) {
  const salaryString = formatSalary(job.salary_min, job.salary_max, job.pay_period)

  return (
    <div 
      className="bg-bg-card border border-bg-border rounded-xl p-5 hover:bg-bg-hover hover:border-text-secondary transition-all cursor-pointer group flex flex-col shadow-sm"
      onClick={() => onClick(job.id)}
    >
      {/* Header */}
      <div className="flex items-start justify-between mb-3 gap-2">
        <div>
          <h3 className="text-base font-semibold text-text-primary group-hover:text-accent transition-colors line-clamp-2">
            {job.title}
          </h3>
          <div className="flex items-center gap-2 mt-1.5 text-xs text-text-secondary">
            <Building2 className="w-3.5 h-3.5 text-text-muted" />
            <span className="truncate max-w-[200px]">{job.company}</span>
          </div>
        </div>
        <span className="shrink-0 text-[10px] font-semibold uppercase tracking-wider text-text-muted bg-bg-secondary px-2 py-0.5 rounded border border-bg-border">
          {job.source}
        </span>
      </div>

      {/* Meta Info */}
      <div className="flex flex-wrap gap-3 mt-auto mb-5 text-xs text-text-secondary">
        <div className="flex items-center gap-1.5">
          <MapPin className="w-3.5 h-3.5 text-text-muted" />
          <span className="truncate max-w-[120px]">{job.location || 'Remote'}</span>
        </div>
        <div className="flex items-center gap-1.5">
          <DollarSign className="w-3.5 h-3.5 text-text-muted" />
          <span>{salaryString}</span>
        </div>
        {job.job_type && (
          <div className="flex items-center gap-1.5">
            <Clock className="w-3.5 h-3.5 text-text-muted" />
            <span className="capitalize">{job.job_type.replace('-', ' ')}</span>
          </div>
        )}
      </div>

      {/* Footer / Actions */}
      <div className="flex items-center justify-between pt-4 border-t border-bg-border mt-auto">
        <div className="text-xs text-text-muted">
          {job.posted_at ? formatDistanceToNow(job.posted_at) : 'Recently discovered'}
        </div>
        <div className="flex items-center gap-2" onClick={e => e.stopPropagation()}>
          <button 
            onClick={() => onDismiss(job.id)}
            className="p-2 text-text-muted hover:text-danger hover:bg-danger/10 rounded-md transition-colors"
            title="Dismiss Job"
          >
            <XCircle className="w-4 h-4" />
          </button>
          {isApplied ? (
            <span className="flex items-center gap-1.5 px-3 py-1.5 bg-success/10 border border-success/20 text-success text-xs font-semibold rounded-md">
              Applied
            </span>
          ) : (
            <button 
              onClick={() => onApply(job.id)}
              className="flex items-center gap-1.5 px-4 py-2 bg-text-primary hover:opacity-85 text-bg-card text-sm font-semibold rounded-md transition-colors shadow-sm"
            >
              <Send className="w-4 h-4" />
              Apply
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
