'use client'

import { useQuery } from '@tanstack/react-query'
import { api, type InterviewSummary } from '@/lib/api'
import { formatScheduled } from '../utils'
import { clsx } from 'clsx'
import { Calendar, Link, Target } from 'lucide-react'

function TypeBadge({ type }: { type: string }) {
  const map: Record<string, string> = {
    phone:      'bg-info/10    text-info    border-info/20',
    video:      'bg-purple/10  text-purple  border-purple/20',
    onsite:     'bg-success/10 text-success border-success/20',
    assessment: 'bg-warning/10 text-warning border-warning/20',
  }
  return (
    <span className={clsx('badge border', map[type] ?? 'badge-found')}>
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
    text: `${interview.position} Interview @ ${interview.company} (Round ${interview.round})`,
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
    <div className="card">
      <div className="card-header">
        <div className="flex items-center gap-2">
          <h2 className="card-title">Upcoming Interviews</h2>
          {!isLoading && (
            <span className="text-xs text-text-muted">{interviews.length}</span>
          )}
        </div>
      </div>

      <div className="divide-y divide-bg-border/40">
        {isLoading ? (
          <div className="p-4 space-y-3">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="skeleton h-16 rounded" />
            ))}
          </div>
        ) : isError ? (
          <div className="p-8 text-center text-danger text-sm">Failed to load interviews</div>
        ) : interviews.length === 0 ? (
          <div className="p-8 text-center">
            <Target className="w-8 h-8 text-text-muted mx-auto mb-2" />
            <div className="text-sm text-text-muted">No upcoming interviews</div>
          </div>
        ) : (
          interviews.map((iv) => (
            <div key={iv.interview_id} className="px-5 py-3.5 table-row-hover">
              <div className="flex items-start justify-between gap-3">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm font-medium text-text-primary">{iv.company}</span>
                    <span className="text-xs text-text-muted">R{iv.round}</span>
                    <TypeBadge type={iv.type} />
                  </div>
                  <div className="text-xs text-text-secondary mt-0.5 truncate">{iv.position}</div>
                  <div className="flex items-center gap-1.5 mt-1.5 text-xs text-text-muted">
                    <Calendar className="w-3 h-3" />
                    <span className={iv.scheduled_at ? 'text-warning' : ''}>
                      {formatScheduled(iv.scheduled_at)}
                    </span>
                  </div>
                </div>
                <div className="flex flex-col gap-1.5 shrink-0">
                  {iv.meeting_url && (
                    <a
                      href={iv.meeting_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="btn-success text-xs py-1 px-2.5"
                    >
                      <Link className="w-3 h-3" /> Join
                    </a>
                  )}
                  <a
                    href={addToCalendarLink(iv)}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="btn-secondary text-xs py-1 px-2.5"
                  >
                    <Calendar className="w-3 h-3" /> Add
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
