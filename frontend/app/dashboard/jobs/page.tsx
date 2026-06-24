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
  const [selectedCandidateId, setSelectedCandidateId] = useState<string>('')

  // Fetch candidates managed by the logged-in BD User
  const { data: candidates = [], isLoading: candidatesLoading } = useQuery({
    queryKey: ['candidates'],
    queryFn: () => api.getCandidates(),
  })

  // Fetch Jobs (global list)
  const { 
    data: jobs, 
    isLoading: jobsLoading, 
    isError: jobsError, 
    refetch: refetchJobs,
    isFetching
  } = useJobs({ skip: page * PAGE_SIZE, limit: PAGE_SIZE })

  // Fetch selected candidate's applications to cross-reference
  const { data: applications } = useQuery({
    queryKey: ['applications', 'jobs-cross-ref', selectedCandidateId],
    queryFn: () => api.getApplications({ candidateId: selectedCandidateId }),
    enabled: !!selectedCandidateId,
    staleTime: 60 * 1000,
  })

  // Cross-reference map: Job ID -> boolean
  const appliedJobIds = new Set(applications?.map(app => app.job_id)) 

  const handleApply = (jobId: string) => {
    console.log("Apply triggered for job:", jobId)
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
      <div className="flex flex-col items-center justify-center min-h-[400px] bg-bg-secondary rounded-xl border border-bg-border">
        <AlertCircle className="w-12 h-12 text-danger mb-4" />
        <h2 className="text-lg font-semibold text-text-primary">Failed to load jobs</h2>
        <p className="text-text-secondary text-sm mt-1 mb-6">There was an error communicating with the API.</p>
        <button 
          onClick={() => refetchJobs()}
          className="flex items-center gap-2 px-4 py-2 bg-bg-card border border-bg-border hover:bg-bg-hover text-text-primary rounded-md transition-colors font-medium text-sm"
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
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div className="flex flex-col sm:flex-row sm:items-center gap-4">
          <div>
            <h1 className="text-2xl font-bold text-text-primary tracking-tight">Jobs Feed</h1>
            <p className="text-sm text-text-secondary mt-1">Discover and apply to new opportunities.</p>
          </div>

          {/* Candidate selector */}
          <div className="flex items-center gap-2">
            <span className="text-xs text-text-muted font-medium">Candidate:</span>
            <select
              value={selectedCandidateId}
              onChange={(e) => setSelectedCandidateId(e.target.value)}
              disabled={candidatesLoading}
              className="bg-bg-secondary border border-bg-border text-text-primary rounded-lg text-xs px-3 py-1.5 focus:outline-none focus:border-accent transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed min-w-[180px]"
            >
              <option value="">Select Candidate...</option>
              {candidates.map((cand) => (
                <option key={cand.id} value={cand.id}>
                  {cand.name}
                </option>
              ))}
            </select>
          </div>
        </div>
        
        {/* Pagination Controls (Top) */}
        <div className="flex items-center gap-3">
          <span className="text-sm text-text-muted">Page {page + 1}</span>
          <div className="flex bg-bg-secondary border border-bg-border rounded-md overflow-hidden">
            <button 
              onClick={() => setPage(p => Math.max(0, p - 1))}
              disabled={!hasPrev || isFetching}
              className="p-2 text-text-secondary hover:text-text-primary hover:bg-bg-hover disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              <ChevronLeft className="w-4 h-4" />
            </button>
            <div className="w-px bg-bg-border" />
            <button 
              onClick={() => setPage(p => p + 1)}
              disabled={!hasNext || isFetching}
              className="p-2 text-text-secondary hover:text-text-primary hover:bg-bg-hover disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
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
        <div className="flex flex-col items-center justify-center min-h-[400px] bg-bg-secondary rounded-xl border border-bg-border border-dashed">
          <div className="w-16 h-16 bg-bg-hover rounded-full flex items-center justify-center mb-4 border border-bg-border">
            <Briefcase className="w-8 h-8 text-text-muted" />
          </div>
          <h2 className="text-lg font-semibold text-text-primary">No jobs found</h2>
          <p className="text-text-muted text-sm mt-1 max-w-sm text-center">
            {page === 0 ? "Check back later for new opportunities from the discovery pipeline." : "You've reached the end of the list."}
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-5">
          {jobs.map((job) => {
            const isApplied = selectedCandidateId ? appliedJobIds.has(job.id) : false
            return (
              <JobCard 
                key={job.id} 
                job={job} 
                onApply={handleApply}
                onDismiss={handleDismiss}
                onClick={handleCardClick}
                isApplied={isApplied}
              />
            )
          })}
        </div>
      )}
    </div>
  )
}
