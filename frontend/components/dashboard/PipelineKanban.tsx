'use client'

import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api, type ApplicationSummary } from '@/lib/api'
import { clsx } from 'clsx'
import { StatusBadge } from '../shared/StatusBadge'
import { Skeleton } from '../shared/Skeleton'
import { useRouter } from 'next/navigation'
import { MoreHorizontal, Pause, Play, XCircle, Trash2, Loader2 } from 'lucide-react'
import { useConfirm } from '../ui/ConfirmDialog'
import { useToast } from '../ui/Toast'

const PIPELINE_STAGES = [
  { id: 'SOURCING', statuses: ['FOUND', 'ANALYZED', 'MATCHED'] },
  { id: 'APPLYING', statuses: ['RESUME_UPDATED', 'COVER_LETTER_CREATED', 'QUEUED', 'APPLICATION_STARTED', 'FORM_COMPLETED', 'SUBMITTED', 'CONFIRMED'] },
  { id: 'INTERVIEWING', statuses: ['INTERVIEW_R1', 'INTERVIEW_R2', 'ASSESSMENT'] },
  { id: 'OFFER', statuses: ['OFFER'] },
  { id: 'REJECTED', statuses: ['REJECTED', 'FAILED', 'BLOCKED'] },
]

// Mirrors PAUSABLE_STATUSES in the applications router.
const PAUSABLE_STATUSES = [
  'FOUND', 'ANALYZED', 'MATCHED', 'RESUME_UPDATED',
  'COVER_LETTER_CREATED', 'QUEUED', 'FAILED', 'BLOCKED',
]
const CANCELLABLE_STATUSES = [
  ...PAUSABLE_STATUSES, 'APPLICATION_STARTED', 'FORM_COMPLETED', 'GHOSTED',
]

function canPause(status: string) {
  return PAUSABLE_STATUSES.includes(status?.toUpperCase())
}
function canCancel(status: string) {
  return CANCELLABLE_STATUSES.includes(status?.toUpperCase())
}

// ── Per-card action menu ──────────────────────────────────────────────────────
function CardActions({ app }: { app: ApplicationSummary }) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['applications'] })
    queryClient.invalidateQueries({ queryKey: ['kpis'] })
  }

  const confirm = useConfirm()
  const toast = useToast()

  const mutation = useMutation({
    mutationFn: ({ fn }: { fn: () => Promise<unknown> }) => fn(),
    onSuccess: () => { refresh(); setOpen(false) },
    onError: (err: unknown) => {
      setOpen(false)
      const msg = err instanceof Error ? err.message : 'Unknown error'
      toast.error(`Action failed: ${msg}`)
    },
  })

  const run = async (fn: () => Promise<unknown>, confirmMsg?: string) => {
    if (confirmMsg) {
      const ok = await confirm({
        title: 'Are you sure?',
        message: confirmMsg,
        variant: 'danger',
      })
      if (!ok) return
    }
    mutation.mutate({ fn })
  }

  const paused = !!app.paused
  const id = app.application_id
  const showPause = !paused && canPause(app.status)
  const showCancel = canCancel(app.status)

  const itemClass = 'w-full flex items-center gap-2 px-3 py-1.5 text-xs text-text-primary hover:bg-bg-hover transition-colors text-left'

  return (
    <div className="relative" onClick={(e) => e.stopPropagation()}>
      <button
        onClick={(e) => { e.stopPropagation(); setOpen((o) => !o) }}
        disabled={mutation.isPending}
        className="text-text-muted hover:text-text-primary transition-colors p-0.5 rounded disabled:opacity-50"
        aria-label="Application actions"
        title="Actions"
      >
        {mutation.isPending
          ? <Loader2 className="w-4 h-4 animate-spin" />
          : <MoreHorizontal className="w-4 h-4" />}
      </button>

      {open && (
        <>
          {/* click-outside backdrop */}
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div className="absolute right-0 z-20 mt-1 w-44 rounded-md border border-bg-border bg-bg-secondary shadow-lg py-1">
            {paused && (
              <button className={itemClass} onClick={() => run(() => api.resumeApplication(id))}>
                <Play className="w-3.5 h-3.5 text-success" /> Resume
              </button>
            )}
            {showPause && (
              <button className={itemClass} onClick={() => run(() => api.pauseApplication(id))}>
                <Pause className="w-3.5 h-3.5 text-warning" /> Pause
              </button>
            )}
            {showCancel && (
              <button
                className={itemClass}
                onClick={() => run(
                  () => api.cancelApplication(id),
                  'Cancel this application? It will be withdrawn but kept for the record.',
                )}
              >
                <XCircle className="w-3.5 h-3.5 text-text-muted" /> Cancel
              </button>
            )}
            <button
              className={clsx(itemClass, 'text-danger hover:bg-danger/10')}
              onClick={() => run(
                () => api.deleteApplication(id),
                'Permanently delete this application? This cannot be undone.',
              )}
            >
              <Trash2 className="w-3.5 h-3.5" /> Delete
            </button>
          </div>
        </>
      )}
    </div>
  )
}

export function PipelineKanban({ candidateId }: { candidateId?: string }) {
  const router = useRouter()

  const { data: applications, isLoading, isError } = useQuery({
    queryKey: ['applications', 'pipeline', candidateId],
    queryFn: () => api.getApplications({ candidateId, limit: 100 }),
    refetchInterval: 30_000,
  })

  if (isError) {
    return <div className="p-8 text-center text-red-500">Failed to load pipeline data</div>
  }

  const getAppsForStage = (statuses: string[]) => {
    return applications?.filter(app => statuses.includes(app.status.toUpperCase())) || []
  }

  return (
    <div className="flex gap-4 overflow-x-auto pb-4 snap-x">
      {PIPELINE_STAGES.map((stage) => {
        const stageApps = getAppsForStage(stage.statuses)
        return (
          <div key={stage.id} className="flex-none w-80 bg-bg-secondary rounded-xl border border-bg-border p-4 flex flex-col snap-start max-h-[600px]">
            <div className="flex items-center justify-between mb-4 shrink-0">
              <h3 className="font-semibold text-text-primary">{stage.id}</h3>
              <span className="text-xs font-medium text-text-secondary bg-bg-border px-2 py-0.5 rounded-full">
                {isLoading ? '-' : stageApps.length}
              </span>
            </div>

            <div className="flex-1 overflow-y-auto space-y-3 pr-2 scrollbar-thin">
              {isLoading ? (
                Array.from({ length: 3 }).map((_, i) => (
                  <Skeleton key={i} className="h-24 w-full" />
                ))
              ) : stageApps.length === 0 ? (
                <div className="text-center py-8 text-text-muted text-sm">
                  No applications
                </div>
              ) : (
                stageApps.map(app => (
                  <div
                    key={app.application_id}
                    onClick={() => router.push(`/applications/${app.application_id}`)}
                    className="bg-bg-card border border-bg-border rounded-lg p-3 hover:bg-bg-hover hover:border-text-muted cursor-pointer transition-all group"
                  >
                    <div className="flex items-start justify-between mb-2">
                      <StatusBadge status={app.status} className="scale-90 origin-top-left" />
                      <CardActions app={app} />
                    </div>
                    <h4 className="text-sm font-medium text-text-primary truncate">{app.job_title}</h4>
                    <p className="text-xs text-text-secondary truncate mt-0.5">{app.company}</p>

                    {app.paused && (
                      <span className="mt-1.5 inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-warning/10 text-warning border border-warning/20 font-medium">
                        <Pause className="w-2.5 h-2.5" /> Paused
                      </span>
                    )}

                    {app.fit_score != null && (
                      <div className="mt-3 flex items-center gap-2">
                        <div className="flex-1 h-1.5 bg-bg-border rounded-full overflow-hidden">
                          <div
                            className={clsx(
                              "h-full rounded-full",
                              app.fit_score >= 80 ? "bg-success" :
                              app.fit_score >= 60 ? "bg-warning" : "bg-danger"
                            )}
                            style={{ width: `${app.fit_score}%` }}
                          />
                        </div>
                        <span className="text-[10px] text-text-muted font-medium">{Math.round(app.fit_score)}% Fit</span>
                      </div>
                    )}
                  </div>
                ))
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}
