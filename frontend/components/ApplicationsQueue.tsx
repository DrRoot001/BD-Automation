'use client'

import { useQuery } from '@tanstack/react-query'
import { api, type ApplicationSummary } from '@/lib/api'
import { clsx } from 'clsx'
import { formatDistanceToNow } from './utils'

function StatusBadge({ status }: { status: string }) {
  const s = status.toLowerCase()
  const map: Record<string, string> = {
    queued:       'badge-queued',
    found:        'badge-found',
    submitted:    'badge-submitted',
    confirmed:    'badge-confirmed',
    interview_r1: 'badge-interview_r1',
    interview_r2: 'badge-interview_r2',
    rejected:     'badge-rejected',
    offer:        'badge-offer',
    assessment:   'badge-interview_r1',
  }
  return (
    <span className={clsx('badge', map[s] ?? 'badge-found')}>
      <span className="w-1.5 h-1.5 rounded-full bg-current opacity-80" />
      {status.replace('_', ' ')}
    </span>
  )
}

const PLATFORM_EMOJI: Record<string, string> = {
  linkedin:          '💼',
  indeed:            '🔍',
  greenhouse:        '🌱',
  lever:             '⚡',
  workday:           '🏢',
  ashby:             '🎯',
  glassdoor:         '🔮',
  angellist:         '🚀',
  wellfound:         '🚀',
  smartrecruiters:   '🎯',
}

function getPlatformEmoji(platform: string): string {
  const key = platform.toLowerCase()
  for (const [k, v] of Object.entries(PLATFORM_EMOJI)) {
    if (key.includes(k)) return v
  }
  return '📋'
}

export function ApplicationsQueue() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['applications'],
    queryFn: () => api.getApplications({ limit: 50 }),
    refetchInterval: 60_000,
  })

  const apps = data ?? []

  return (
    <div className="card animate-slide-up">
      {/* Header */}
      <div className="flex items-center justify-between px-5 py-4 border-b border-bg-border">
        <div className="flex items-center gap-3">
          <h2 className="font-semibold text-text-primary">Application Queue</h2>
          {!isLoading && (
            <span className="badge bg-accent/10 text-accent border border-accent/20 text-xs">
              {apps.length} total
            </span>
          )}
        </div>
        <div className="flex items-center gap-2 text-xs text-text-muted">
          <span className="live-dot" />
          Live
        </div>
      </div>

      {/* Table */}
      <div className="overflow-x-auto">
        {isLoading ? (
          <div className="p-5 space-y-3">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="skeleton h-12 rounded-lg" />
            ))}
          </div>
        ) : isError ? (
          <div className="p-8 text-center text-danger text-sm">
            Failed to load applications
          </div>
        ) : apps.length === 0 ? (
          <div className="p-10 text-center text-text-muted text-sm">
            No applications yet. Start by running the job discovery pipeline.
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-bg-border text-text-muted text-xs uppercase tracking-wider">
                <th className="text-left px-5 py-3">Position</th>
                <th className="text-left px-3 py-3">Platform</th>
                <th className="text-left px-3 py-3">Status</th>
                <th className="text-right px-3 py-3">Fit</th>
                <th className="text-right px-5 py-3">Added</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-bg-border/50">
              {apps.map((app) => (
                <tr key={app.application_id} className="table-row-hover">
                  <td className="px-5 py-3">
                    <div className="font-medium text-text-primary truncate max-w-[200px]">
                      {app.job_title}
                    </div>
                    <div className="text-text-muted text-xs truncate max-w-[200px]">
                      {app.company}
                    </div>
                  </td>
                  <td className="px-3 py-3">
                    <span className="flex items-center gap-1.5 text-text-secondary">
                      {getPlatformEmoji(app.platform)}
                      <span className="capitalize text-xs">{app.platform}</span>
                    </span>
                  </td>
                  <td className="px-3 py-3">
                    <StatusBadge status={app.status} />
                  </td>
                  <td className="px-3 py-3 text-right">
                    {app.fit_score != null ? (
                      <span
                        className={clsx(
                          'text-xs font-mono font-medium',
                          app.fit_score >= 80
                            ? 'text-success'
                            : app.fit_score >= 60
                            ? 'text-warning'
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
