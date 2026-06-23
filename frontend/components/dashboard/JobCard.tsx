import { type JobSummary } from '@/lib/api'
import { formatSalary } from '@/lib/utils'
import { formatDistanceToNow } from '@/components/utils'
import { ExternalLink, Building2, MapPin, DollarSign, Clock, Send, XCircle } from 'lucide-react'

export interface JobCardProps {
  job: JobSummary
  onApply: (jobId: string) => void
  onDismiss: (jobId: string) => void
  onClick: (jobId: string) => void
}

export function JobCard({ job, onApply, onDismiss, onClick }: JobCardProps) {
  const salaryString = formatSalary(job.salary_min, job.salary_max, job.pay_period)

  return (
    <div 
      className="bg-zinc-900/40 border border-zinc-800/50 rounded-xl p-5 hover:bg-zinc-900/80 hover:border-zinc-700 transition-all cursor-pointer group flex flex-col"
      onClick={() => onClick(job.id)}
    >
      {/* Header */}
      <div className="flex items-start justify-between mb-3">
        <div>
          <h3 className="text-base font-semibold text-zinc-100 group-hover:text-blue-400 transition-colors line-clamp-2">
            {job.title}
          </h3>
          <div className="flex items-center gap-2 mt-1 text-sm text-zinc-400">
            <Building2 className="w-4 h-4" />
            <span className="truncate max-w-[200px]">{job.company}</span>
          </div>
        </div>
        <span className="shrink-0 text-[10px] font-medium uppercase tracking-wider text-zinc-500 bg-zinc-800/50 px-2.5 py-1 rounded-md">
          {job.source}
        </span>
      </div>

      {/* Meta Info */}
      <div className="flex flex-wrap gap-3 mt-auto mb-5 text-xs text-zinc-400">
        <div className="flex items-center gap-1.5">
          <MapPin className="w-3.5 h-3.5 text-zinc-500" />
          <span className="truncate max-w-[120px]">{job.location || 'Remote'}</span>
        </div>
        <div className="flex items-center gap-1.5">
          <DollarSign className="w-3.5 h-3.5 text-zinc-500" />
          <span>{salaryString}</span>
        </div>
        {job.job_type && (
          <div className="flex items-center gap-1.5">
            <Clock className="w-3.5 h-3.5 text-zinc-500" />
            <span className="capitalize">{job.job_type.replace('-', ' ')}</span>
          </div>
        )}
      </div>

      {/* Footer / Actions */}
      <div className="flex items-center justify-between pt-4 border-t border-zinc-800/50 mt-auto">
        <div className="text-xs text-zinc-500">
          {job.posted_at ? formatDistanceToNow(job.posted_at) : 'Recently discovered'}
        </div>
        <div className="flex items-center gap-2" onClick={e => e.stopPropagation()}>
          <button 
            onClick={() => onDismiss(job.id)}
            className="p-2 text-zinc-500 hover:text-red-400 hover:bg-red-400/10 rounded-md transition-colors"
            title="Dismiss Job"
          >
            <XCircle className="w-4 h-4" />
          </button>
          <button 
            onClick={() => onApply(job.id)}
            className="flex items-center gap-1.5 px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium rounded-md transition-colors shadow-sm"
          >
            <Send className="w-4 h-4" />
            Apply
          </button>
        </div>
      </div>
    </div>
  )
}
