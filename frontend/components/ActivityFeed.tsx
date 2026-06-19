'use client'

import { useQuery } from '@tanstack/react-query'
import { api, type ActivityEvent } from '@/lib/api'
import { formatDistanceToNow } from './utils'
import { clsx } from 'clsx'

const EVENT_CONFIG: Record<
  string,
  { icon: string; color: string; label: string }
> = {
  'application.status_changed': { icon: '🔄', color: 'text-accent',  label: 'Status Update' },
  'application.submitted':      { icon: '📤', color: 'text-info',    label: 'Submitted' },
  'application.failed':         { icon: '❌', color: 'text-danger',  label: 'Failed' },
  'email.classified':           { icon: '📧', color: 'text-purple',  label: 'Email' },
  'interview.detected':         { icon: '🎯', color: 'text-success', label: 'Interview' },
  'job.discovered':             { icon: '🔍', color: 'text-warning', label: 'Job Found' },
}

function EventItem({ event }: { event: ActivityEvent }) {
  const cfg = EVENT_CONFIG[event.event_type] ?? {
    icon: '📋',
    color: 'text-text-muted',
    label: event.event_type,
  }

  return (
    <div className="flex items-start gap-3 px-5 py-3 table-row-hover">
      {/* Icon bubble */}
      <div
        className={clsx(
          'w-8 h-8 rounded-full bg-bg-secondary border border-bg-border',
          'flex items-center justify-center text-base shrink-0 mt-0.5',
        )}
      >
        {cfg.icon}
      </div>

      {/* Content */}
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className={clsx('text-xs font-medium', cfg.color)}>
            {cfg.label}
          </span>
        </div>
        <p className="text-sm text-text-secondary mt-0.5 leading-snug line-clamp-2">
          {event.summary}
        </p>
      </div>

      {/* Timestamp */}
      <div className="text-xs text-text-muted whitespace-nowrap shrink-0 pt-0.5">
        {formatDistanceToNow(event.timestamp)}
      </div>
    </div>
  )
}

export function ActivityFeed() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['activity-feed'],
    queryFn: () => api.getActivityFeed(25),
    refetchInterval: 30_000,
  })

  const events = data ?? []

  return (
    <div className="card animate-slide-up flex flex-col h-full">
      <div className="flex items-center justify-between px-5 py-4 border-b border-bg-border">
        <div className="flex items-center gap-3">
          <h2 className="font-semibold text-text-primary">Activity Feed</h2>
          {!isLoading && events.length > 0 && (
            <span className="badge bg-bg-hover text-text-muted text-xs border border-bg-border">
              {events.length}
            </span>
          )}
        </div>
        <span className="flex items-center gap-1.5 text-xs text-text-muted">
          <span className="live-dot" />
          Real-time
        </span>
      </div>

      <div className="overflow-y-auto flex-1 divide-y divide-bg-border/50 no-scrollbar">
        {isLoading ? (
          <div className="p-5 space-y-3">
            {Array.from({ length: 8 }).map((_, i) => (
              <div key={i} className="flex gap-3">
                <div className="skeleton w-8 h-8 rounded-full shrink-0" />
                <div className="flex-1 space-y-1.5">
                  <div className="skeleton h-3 w-16 rounded" />
                  <div className="skeleton h-3 w-full rounded" />
                </div>
              </div>
            ))}
          </div>
        ) : isError ? (
          <div className="p-8 text-center text-danger text-sm">
            Failed to load activity
          </div>
        ) : events.length === 0 ? (
          <div className="p-10 text-center">
            <div className="text-4xl mb-3">📭</div>
            <div className="text-text-muted text-sm">No activity yet.</div>
          </div>
        ) : (
          events.map((ev, i) => <EventItem key={i} event={ev} />)
        )}
      </div>
    </div>
  )
}
