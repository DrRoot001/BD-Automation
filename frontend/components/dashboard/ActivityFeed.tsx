'use client'

import { useQuery } from '@tanstack/react-query'
import { api, type ActivityEvent } from '@/lib/api'
import { formatDistanceToNow } from '../utils'
import { clsx } from 'clsx'
import { RefreshCcw, Send, XCircle, Mail, Target, Search, FileText, Inbox } from 'lucide-react'
import { ReactNode } from 'react'

function getEventConfig(eventType: string): { icon: ReactNode; color: string; label: string } {
  switch (eventType) {
    case 'application.status_changed': return { icon: <RefreshCcw className="w-3.5 h-3.5" />, color: 'text-accent',  label: 'Status Update' }
    case 'application.submitted':      return { icon: <Send className="w-3.5 h-3.5" />,        color: 'text-info',    label: 'Submitted'     }
    case 'application.failed':         return { icon: <XCircle className="w-3.5 h-3.5" />,     color: 'text-danger',  label: 'Failed'        }
    case 'email.classified':           return { icon: <Mail className="w-3.5 h-3.5" />,         color: 'text-purple',  label: 'Email'         }
    case 'interview.detected':         return { icon: <Target className="w-3.5 h-3.5" />,       color: 'text-success', label: 'Interview'     }
    case 'job.discovered':             return { icon: <Search className="w-3.5 h-3.5" />,       color: 'text-warning', label: 'Job Found'     }
    default:                           return { icon: <FileText className="w-3.5 h-3.5" />,     color: 'text-text-muted', label: eventType   }
  }
}

function EventItem({ event }: { event: ActivityEvent }) {
  const cfg = getEventConfig(event.event_type)
  return (
    <div className="flex items-start gap-3 px-4 py-3 table-row-hover">
      <div className={clsx('mt-0.5 shrink-0', cfg.color)}>{cfg.icon}</div>
      <div className="flex-1 min-w-0">
        <div className={clsx('text-xs font-medium', cfg.color)}>{cfg.label}</div>
        <p className="text-xs text-text-secondary mt-0.5 leading-relaxed line-clamp-2">{event.summary}</p>
      </div>
      <div className="text-xs text-text-muted whitespace-nowrap shrink-0 pt-0.5">
        {formatDistanceToNow(event.timestamp)}
      </div>
    </div>
  )
}

export function ActivityFeed({ candidateId }: { candidateId?: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['activity-feed', candidateId],
    queryFn: () => api.getActivityFeed(25, candidateId),
    refetchInterval: 30_000,
  })

  const events = data ?? []

  return (
    <div className="card flex flex-col max-h-[480px]">
      <div className="card-header shrink-0">
        <div className="flex items-center gap-2">
          <h2 className="card-title">Activity</h2>
          {!isLoading && events.length > 0 && (
            <span className="text-xs text-text-muted">{events.length}</span>
          )}
        </div>
        <span className="flex items-center gap-1.5 text-xs text-text-muted">
          <span className="live-dot" />
          Real-time
        </span>
      </div>

      <div className="overflow-y-auto flex-1 divide-y divide-bg-border/40" style={{ scrollbarWidth: 'none' }}>
        {isLoading ? (
          <div className="p-4 space-y-3">
            {Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="flex gap-2.5">
                <div className="skeleton w-3.5 h-3.5 rounded shrink-0 mt-0.5" />
                <div className="flex-1 space-y-1.5">
                  <div className="skeleton h-3 w-14 rounded" />
                  <div className="skeleton h-3 w-full rounded" />
                </div>
              </div>
            ))}
          </div>
        ) : isError ? (
          <div className="p-8 text-center text-danger text-sm">Failed to load activity</div>
        ) : events.length === 0 ? (
          <div className="p-8 text-center">
            <Inbox className="w-8 h-8 text-text-muted mx-auto mb-2" />
            <div className="text-sm text-text-muted">No activity yet</div>
          </div>
        ) : (
          events.map((ev, i) => <EventItem key={i} event={ev} />)
        )}
      </div>
    </div>
  )
}
