'use client'

import { useQuery } from '@tanstack/react-query'
import { api, type ApplicationSummary } from '@/lib/api'
import { clsx } from 'clsx'
import { formatDistanceToNow } from './utils'
import { useRouter } from 'next/navigation'

function StatusBadge({ status }: { status: string }) {
  const s = status.toLowerCase()
  const map: Record<string, string> = {
    queued:       'badge-queued',
    found:        'badge-found',
    submitted:    'badge-submitted',
    confirmed:    'badge-confirmed',
    interview_r1: 'badge-interview_r1',
    interview_r2: 'badge-interview_r2',
    failed:       'badge-failed',
    blocked:      'badge-blocked',
    rejected:     'badge-rejected',
    offer:        'badge-offer',
    assessment:   'badge-interview_r1',
  }
  return (
    <span className={clsx('badge', map[s] ?? 'badge-found')}>
      {status.replace('_', ' ')}
    </span>
  )
}

interface ApplicationsQueueProps {
  candidateId?: string
  statusFilter?: string[]
  emptyMessage?: string
}

export function ApplicationsQueue({ candidateId, statusFilter, emptyMessage }: ApplicationsQueueProps = {}) {
  const router = useRouter()

  const { data, isLoading, isError } = useQuery({
    queryKey: ['applications', candidateId],
    queryFn: () => api.getApplications({ limit: 50, candidateId }),
    refetchInterval: 60_000,
  })

  const all = data ?? []
  const apps = statusFilter
    ? all.filter((a) => statusFilter.includes(a.status.toUpperCase()))
    : all

  return (
    <div className="card">
      <div className="card-header">
        <div className="flex items-center gap-2">
          <h2 className="card-title">Application Queue</h2>
          {!isLoading && (
            <span className="text-xs text-text-muted">{apps.length} total</span>
          )}
        </div>
        <div className="flex items-center gap-1.5 text-xs text-text-muted">
          <span className="live-dot" />
          Live
        </div>
      </div>

      <div className="overflow-x-auto">
        {isLoading ? (
          <div className="p-4 space-y-2">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="skeleton h-10 rounded" />
            ))}
          </div>
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
                <th className="text-left px-3 py-2.5 font-medium">Platform</th>
                <th className="text-left px-3 py-2.5 font-medium">Status</th>
                <th className="text-right px-3 py-2.5 font-medium">Fit</th>
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
                    <div className="font-medium text-text-primary truncate max-w-[200px] text-sm">
                      {app.job_title}
                    </div>
                    <div className="text-text-muted text-xs truncate max-w-[200px] mt-0.5">
                      {app.company}
                    </div>
                    {app.error_message && (
                      <div className="text-danger text-[10px] mt-1 max-w-[250px] truncate" title={app.error_message}>
                        Error: {app.error_message}
                      </div>
                    )}
                  </td>
                  <td className="px-3 py-3">
                    <span className="text-xs text-text-secondary capitalize">{app.platform}</span>
                  </td>
                  <td className="px-3 py-3">
                    <StatusBadge status={app.status} />
                  </td>
                  <td className="px-3 py-3 text-right">
                    {app.fit_score != null ? (
                      <span
                        className={clsx(
                          'text-xs font-medium tabular-nums',
                          app.fit_score >= 80 ? 'text-success'
                          : app.fit_score >= 60 ? 'text-warning'
                          : 'text-text-muted',
                        )}
                      >
                        {app.fit_score.toFixed(0)}%
                      </span>
                    ) : (
                      <span className="text-text-muted text-xs">—</span>
                    )}
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
    </div>
  )
}
