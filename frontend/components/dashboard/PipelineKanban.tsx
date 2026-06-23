'use client'

import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { clsx } from 'clsx'
import { StatusBadge } from '../shared/StatusBadge'
import { Skeleton } from '../shared/Skeleton'
import { useRouter } from 'next/navigation'
import { MoreHorizontal } from 'lucide-react'

const PIPELINE_STAGES = [
  { id: 'SOURCING', statuses: ['FOUND', 'ANALYZED', 'MATCHED'] },
  { id: 'APPLYING', statuses: ['QUEUED', 'SUBMITTED', 'CONFIRMED'] },
  { id: 'INTERVIEWING', statuses: ['INTERVIEW_R1', 'INTERVIEW_R2', 'ASSESSMENT'] },
  { id: 'OFFER', statuses: ['OFFER'] },
  { id: 'REJECTED', statuses: ['REJECTED', 'FAILED', 'BLOCKED'] },
]

export function PipelineKanban() {
  const router = useRouter()
  
  const { data: applications, isLoading, isError } = useQuery({
    queryKey: ['applications', 'pipeline'],
    queryFn: () => api.getApplications({ limit: 100 }), // Fetch more for kanban
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
                    onClick={() => router.push(`/dashboard/applications/${app.application_id}`)}
                    className="bg-bg-card border border-bg-border rounded-lg p-3 hover:bg-bg-hover hover:border-text-muted cursor-pointer transition-all group"
                  >
                    <div className="flex items-start justify-between mb-2">
                      <StatusBadge status={app.status} className="scale-90 origin-top-left" />
                      <button className="text-text-muted opacity-0 group-hover:opacity-100 hover:text-text-primary transition-opacity">
                        <MoreHorizontal className="w-4 h-4" />
                      </button>
                    </div>
                    <h4 className="text-sm font-medium text-text-primary truncate">{app.job_title}</h4>
                    <p className="text-xs text-text-secondary truncate mt-0.5">{app.company}</p>
                    
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
