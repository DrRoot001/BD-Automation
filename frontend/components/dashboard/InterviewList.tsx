'use client'

import { useQuery } from '@tanstack/react-query'
import { api, type InterviewSummary } from '@/lib/api'
import { formatScheduled, formatDistanceToNow } from '@/lib/utils'
import { clsx } from 'clsx'
import { Calendar, ExternalLink, Inbox } from 'lucide-react'

function TypeBadge({ type }: { type: string }) {
  const map: Record<string, string> = {
    phone:      'bg-info/10    text-info    border-info/20',
    video:      'bg-purple/10  text-purple  border-purple/20',
    onsite:     'bg-success/10 text-success border-success/20',
    assessment: 'bg-warning/10 text-warning border-warning/20',
  }
  return (
    <span className={clsx('px-2 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider border', map[type] ?? 'bg-bg-secondary text-text-muted border-bg-border')}>
      {type}
    </span>
  )
}

function addToCalendarLink(interview: InterviewSummary): string {
  if (!interview.scheduled_at) return '#'
  const start = new Date(interview.scheduled_at)
  const end = new Date(start.getTime() + 60 * 60 * 1000)
  const fmt = (d: Date) => d.toISOString().replace(/[-:]/g, '').split('.')[0] + 'Z'
  const params = new URLSearchParams({
    action: 'TEMPLATE',
    text: `${interview.position || 'Job'} Interview @ ${interview.company} (Round ${interview.round})`,
    dates: `${fmt(start)}/${fmt(end)}`,
    details: interview.meeting_url ?? '',
  })
  return `https://calendar.google.com/calendar/r/eventedit?${params.toString()}`
}

export function InterviewList({ candidateId }: { candidateId?: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['interviews', candidateId],
    queryFn: () => api.getInterviews(true, candidateId),
    refetchInterval: 60_000,
  })

  const interviews = data ?? []

  return (
    <div className="card bg-bg-card border border-bg-border rounded-xl shadow-sm overflow-hidden">
      <div className="px-5 py-4 border-b border-bg-border flex items-center justify-between">
        <div className="flex items-center gap-2">
          <h2 className="font-semibold text-text-primary text-sm">Upcoming Interviews</h2>
          {!isLoading && (
            <span className="text-xs text-text-muted bg-bg-secondary px-2 py-0.5 rounded-full font-medium">{interviews.length}</span>
          )}
        </div>
      </div>

      <div className="divide-y divide-bg-border">
        {isLoading ? (
          <div className="p-4 space-y-3">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="skeleton h-16 rounded-xl" />
            ))}
          </div>
        ) : isError ? (
          <div className="p-8 text-center text-danger text-sm">Failed to load interviews</div>
        ) : interviews.length === 0 ? (
          <div className="p-8 text-center">
            <Inbox className="w-8 h-8 text-text-muted mx-auto mb-2 opacity-40" />
            <div className="text-sm text-text-muted">No interviews tracked yet</div>
          </div>
        ) : (
          interviews.map((iv) => (
            <div key={iv.interview_id} className="px-5 py-4 hover:bg-bg-hover transition-colors">
              <div className="flex items-start justify-between gap-3">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm font-semibold text-text-primary">{iv.company}</span>
                    <TypeBadge type={iv.type} />
                    {iv.status && (
                      <span className={clsx(
                        "text-[9px] font-semibold px-2 py-0.5 rounded-full border uppercase tracking-wider",
                        iv.status === 'PENDING' ? 'bg-warning/10 text-warning border-warning/20' : 'bg-success/10 text-success border-success/20'
                      )}>
                        {iv.status}
                      </span>
                    )}
                  </div>
                  <div className="text-xs text-text-secondary mt-1 truncate">{iv.position || 'Software Engineer'}</div>
                  <div className="flex items-center gap-1.5 mt-2 text-xs text-text-muted">
                    <Calendar className="w-3.5 h-3.5 text-text-muted" />
                    <span>
                      Received {iv.received_at ? formatDistanceToNow(iv.received_at) + ' ago' : formatScheduled(iv.scheduled_at)}
                    </span>
                  </div>
                </div>
                <div className="flex flex-col gap-1.5 shrink-0">
                  {iv.meeting_url && (
                    <a
                      href={iv.meeting_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="btn-primary !py-1 !px-2.5 !text-xs"
                    >
                      <ExternalLink className="w-3 h-3" /> Join
                    </a>
                  )}
                  <a
                    href={addToCalendarLink(iv)}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="btn-secondary !py-1 !px-2.5 !text-xs"
                  >
                    <Calendar className="w-3 h-3" /> Calendar
                  </a>
                </div>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
