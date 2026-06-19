'use client'

import { useQuery } from '@tanstack/react-query'
import { api, type InterviewSummary } from '@/lib/api'
import { formatScheduled } from './utils'
import { clsx } from 'clsx'

const TYPE_ICON: Record<string, string> = {
  phone:      '📞',
  video:      '🎥',
  onsite:     '🏢',
  assessment: '📝',
}

function TypeBadge({ type }: { type: string }) {
  const map: Record<string, string> = {
    phone:      'bg-info/10    text-info    border-info/20',
    video:      'bg-purple/10  text-purple  border-purple/20',
    onsite:     'bg-success/10 text-success border-success/20',
    assessment: 'bg-warning/10 text-warning border-warning/20',
  }
  return (
    <span className={clsx('badge border', map[type] ?? 'badge-found')}>
      {TYPE_ICON[type] ?? '📋'} {type}
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

export function InterviewList() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['interviews'],
    queryFn: () => api.getInterviews(true),
    refetchInterval: 60_000,
  })

  const interviews = data ?? []

  return (
    <div className="card animate-slide-up">
      <div className="flex items-center justify-between px-5 py-4 border-b border-bg-border">
        <div className="flex items-center gap-3">
          <h2 className="font-semibold text-text-primary">Upcoming Interviews</h2>
          {!isLoading && (
            <span className="badge bg-purple/10 text-purple border border-purple/20 text-xs">
              {interviews.length} upcoming
            </span>
          )}
        </div>
      </div>

      <div className="divide-y divide-bg-border/50">
        {isLoading ? (
          <div className="p-5 space-y-4">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="skeleton h-20 rounded-xl" />
            ))}
          </div>
        ) : isError ? (
          <div className="p-8 text-center text-danger text-sm">
            Failed to load interviews
          </div>
        ) : interviews.length === 0 ? (
          <div className="p-10 text-center">
            <div className="text-4xl mb-3">🎯</div>
            <div className="text-text-muted text-sm">No upcoming interviews yet.</div>
            <div className="text-text-muted text-xs mt-1">
              Emails with interview invitations will appear here automatically.
            </div>
          </div>
        ) : (
          interviews.map((iv) => (
            <div
              key={iv.interview_id}
              className="px-5 py-4 table-row-hover"
            >
              <div className="flex items-start justify-between gap-4">
                <div className="flex-1 min-w-0">
                  {/* Company + Round */}
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-semibold text-text-primary">
                      {iv.company}
                    </span>
                    <span className="text-text-muted text-xs">
                      Round {iv.round}
                    </span>
                    <TypeBadge type={iv.type} />
                  </div>

                  {/* Position */}
                  <div className="text-sm text-text-secondary mt-0.5 truncate">
                    {iv.position}
                  </div>

                  {/* Scheduled time */}
                  <div className="flex items-center gap-1.5 mt-2 text-xs text-text-muted">
                    <span>📅</span>
                    <span
                      className={clsx(
                        iv.scheduled_at ? 'text-warning' : 'text-text-muted',
                      )}
                    >
                      {formatScheduled(iv.scheduled_at)}
                    </span>
                  </div>
                </div>

                {/* Action buttons */}
                <div className="flex flex-col gap-2 shrink-0">
                  {iv.meeting_url && (
                    <a
                      href={iv.meeting_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="btn-success text-xs py-1.5 px-3"
                    >
                      🔗 Join
                    </a>
                  )}
                  <a
                    href={addToCalendarLink(iv)}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="btn-secondary text-xs py-1.5 px-3"
                  >
                    📅 Calendar
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
