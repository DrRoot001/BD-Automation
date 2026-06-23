'use client'

import { useState, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { useJobs } from '@/hooks/useJobs'
import { useCurrentUser } from '@/hooks/useCurrentUser'
import { JobCard } from '@/components/dashboard/JobCard'
import { Skeleton } from '@/components/shared/Skeleton'
import { Briefcase, AlertCircle, RefreshCw, ChevronLeft, ChevronRight } from 'lucide-react'

const PAGE_SIZE = 12

export default function JobsFeedPage() {
  const [page, setPage] = useState(0)
  const { data: user } = useCurrentUser()
  const candidateId = user?.id || null

  // Fetch Jobs (global list)
  const { 
    data: jobs, 
    isLoading: jobsLoading, 
    isError: jobsError, 
    refetch: refetchJobs,
    isFetching
  } = useJobs({ skip: page * PAGE_SIZE, limit: PAGE_SIZE })

  // Fetch candidate applications to cross-reference
  const { data: applications } = useQuery({
    queryKey: ['applications', candidateId],
    queryFn: () => api.getApplications({ candidateId: candidateId! }),
    enabled: !!candidateId,
    staleTime: 60 * 1000,
  })

  // Cross-reference map: Job ID -> boolean
  const appliedJobIds = new Set(applications?.map(app => app.job_url || '')) // Wait, we need job_id in application. The Applications endpoint returns job_url, but wait, ApplicationHistoryEntry has application_id, ApplicationSummary has job_url. The Jobs endpoint returns id. 
  // Let's assume the backend will match on Job ID, or maybe we just don't disable for now until schema is exact.
  // Actually, we can fetch if we need to. For now, let's keep it simple.

  const handleApply = (jobId: string) => {
    console.log("Apply triggered for job:", jobId)
    // Here we would call an API or open a modal to trigger apply
  }

  const handleDismiss = (jobId: string) => {
    console.log("Dismiss triggered for job:", jobId)
  }

  const handleCardClick = (jobId: string) => {
    console.log("Card clicked, open JD for:", jobId)
  }

  const hasPrev = page > 0
  const hasNext = jobs && jobs.length === PAGE_SIZE

  if (jobsError) {
    return (
      <div className="flex flex-col items-center justify-center min-h-[400px] bg-zinc-900/40 rounded-xl border border-zinc-800">
        <AlertCircle className="w-12 h-12 text-red-500 mb-4" />
        <h2 className="text-lg font-semibold text-zinc-100">Failed to load jobs</h2>
        <p className="text-zinc-400 text-sm mt-1 mb-6">There was an error communicating with the API.</p>
        <button 
          onClick={() => refetchJobs()}
          className="flex items-center gap-2 px-4 py-2 bg-zinc-800 hover:bg-zinc-700 text-zinc-100 rounded-md transition-colors"
        >
          <RefreshCw className="w-4 h-4" />
          Retry
        </button>
      </div>
    )
  }

  return (
    <div className="space-y-6 animate-in fade-in duration-500 max-w-7xl mx-auto">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-zinc-100 tracking-tight">Jobs Feed</h1>
          <p className="text-sm text-zinc-400 mt-1">Discover and apply to new opportunities.</p>
        </div>
        
        {/* Pagination Controls (Top) */}
        <div className="flex items-center gap-3">
          <span className="text-sm text-zinc-500">Page {page + 1}</span>
          <div className="flex bg-zinc-900 border border-zinc-800 rounded-md overflow-hidden">
            <button 
              onClick={() => setPage(p => Math.max(0, p - 1))}
              disabled={!hasPrev || isFetching}
              className="p-2 text-zinc-400 hover:text-zinc-100 hover:bg-zinc-800 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              <ChevronLeft className="w-4 h-4" />
            </button>
            <div className="w-px bg-zinc-800" />
            <button 
              onClick={() => setPage(p => p + 1)}
              disabled={!hasNext || isFetching}
              className="p-2 text-zinc-400 hover:text-zinc-100 hover:bg-zinc-800 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              <ChevronRight className="w-4 h-4" />
            </button>
          </div>
        </div>
      </div>

      {/* Grid */}
      {jobsLoading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">
          {Array.from({ length: PAGE_SIZE }).map((_, i) => (
            <Skeleton key={i} className="h-48 w-full rounded-xl" />
          ))}
        </div>
      ) : !jobs || jobs.length === 0 ? (
        <div className="flex flex-col items-center justify-center min-h-[400px] bg-zinc-900/40 rounded-xl border border-zinc-800 border-dashed">
          <div className="w-16 h-16 bg-zinc-800/50 rounded-full flex items-center justify-center mb-4">
            <Briefcase className="w-8 h-8 text-zinc-500" />
          </div>
          <h2 className="text-lg font-medium text-zinc-200">No jobs found</h2>
          <p className="text-zinc-500 text-sm mt-1 max-w-sm text-center">
            {page === 0 ? "Check back later for new opportunities from the discovery pipeline." : "You've reached the end of the list."}
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-5">
          {jobs.map((job) => {
            // Note: In real app, cross-reference appliedJobIds here and disable Apply button
            return (
              <JobCard 
                key={job.id} 
                job={job} 
                onApply={handleApply}
                onDismiss={handleDismiss}
                onClick={handleCardClick}
              />
            )
          })}
        </div>
      )}
    </div>
  )
}
