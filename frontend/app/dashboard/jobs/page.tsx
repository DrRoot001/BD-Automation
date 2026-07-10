'use client'

import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api, JobSummary } from '@/lib/api'
import { useJobs } from '@/hooks/useJobs'
import { JobCard } from '@/components/dashboard/JobCard'
import { Skeleton } from '@/components/shared/Skeleton'
import { Modal } from '@/components/ui/Modal'
import { useToast } from '@/components/ui/Toast'
import { StatusBadge } from '@/components/shared/StatusBadge'
import { Briefcase, AlertCircle, RefreshCw, ChevronLeft, ChevronRight, Eye, EyeOff, Search, ExternalLink, Building2, MapPin, FileText } from 'lucide-react'
import { useWebSocket } from '@/hooks/useWebSocket'
import { formatSalary } from '@/lib/utils'

const PAGE_SIZE = 12

// Jobs carry a category (ml / data / salesforce / …) used for scoped matching.
// JobSummary doesn't declare it yet, so extend the row type locally.
type JobRow = JobSummary & { job_category?: string | null }

function categoryChipClass(active: boolean) {
  return `px-3 py-1 rounded-full border text-xs font-medium capitalize transition-colors ${
    active
      ? 'bg-accent text-white border-accent'
      : 'bg-bg-card text-text-secondary border-bg-border hover:bg-bg-hover hover:text-text-primary'
  }`
}

export default function JobsFeedPage() {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [page, setPage] = useState(0)
  const [selectedCandidateId, setSelectedCandidateId] = useState<string>('')
  const [search, setSearch] = useState('')
  const [selectedCategory, setSelectedCategory] = useState('')
  const [selectedJob, setSelectedJob] = useState<JobSummary | null>(null)

  // Dismissed jobs are persisted per candidate in localStorage.
  const dismissKey = `dismissed-jobs:${selectedCandidateId || 'all'}`
  const [dismissedIds, setDismissedIds] = useState<string[]>([])
  const [showDismissed, setShowDismissed] = useState(false)

  // Connect WebSocket for real-time invalidations
  useWebSocket()

  // Reset to page 0 when candidate, search or category changes
  useEffect(() => {
    setPage(0)
  }, [selectedCandidateId, search, selectedCategory])

  // Load the dismissed set whenever the storage key (candidate) changes.
  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(dismissKey)
      const parsed = raw ? (JSON.parse(raw) as unknown) : []
      setDismissedIds(Array.isArray(parsed) ? parsed.filter((v): v is string => typeof v === 'string') : [])
    } catch {
      setDismissedIds([])
    }
    setShowDismissed(false)
  }, [dismissKey])

  const persistDismissed = (ids: string[]) => {
    setDismissedIds(ids)
    try {
      window.localStorage.setItem(dismissKey, JSON.stringify(ids))
    } catch {
      // localStorage unavailable (private mode / quota) — keep in-memory state only
    }
  }

  // Fetch candidates managed by the logged-in BD User
  const { data: candidates = [], isLoading: candidatesLoading } = useQuery({
    queryKey: ['candidates'],
    queryFn: () => api.getCandidates(),
  })
  const selectedCandidate = candidates.find((c) => c.id === selectedCandidateId)

  // Job categories for the filter chip-row
  const { data: categories = [] } = useQuery({
    queryKey: ['job-categories'],
    queryFn: () => api.getJobCategories(),
    staleTime: 5 * 60 * 1000,
  })

  // Fetch Jobs
  const {
    data: jobs,
    isLoading: jobsLoading,
    isError: jobsError,
    refetch: refetchJobs,
    isFetching
  } = useJobs({ skip: page * PAGE_SIZE, limit: PAGE_SIZE, candidateId: selectedCandidateId || undefined })

  // Fetch selected candidate's applications to cross-reference
  const { data: applications } = useQuery({
    queryKey: ['applications', 'jobs-cross-ref', selectedCandidateId],
    queryFn: () => api.getApplications({ candidateId: selectedCandidateId, limit: 500 }),
    enabled: !!selectedCandidateId,
    staleTime: 60 * 1000,
  })

  // Build a set of job IDs this candidate has already applied to
  const appliedJobIds = new Set<string>(
    applications?.map(app => app.job_id) ?? []
  )

  // Cross-reference map: Job ID -> Status
  const appliedJobsMap = new Map<string, string>(
    applications?.map(app => [app.job_id, app.status]) ?? []
  )

  // Filter jobs locally: search → category → hide already-applied → dismissed split
  const rows = (jobs ?? []) as JobRow[]
  const filteredRows = rows
    .filter(job => !search || job.title.toLowerCase().includes(search.toLowerCase()) || job.company.toLowerCase().includes(search.toLowerCase()))
    .filter(job => !selectedCategory || (job.job_category ?? '').toLowerCase() === selectedCategory)

  const notApplied = filteredRows.filter(job => !selectedCandidateId || !appliedJobIds.has(job.id))
  const appliedHiddenCount = filteredRows.length - notApplied.length

  const dismissedSet = new Set(dismissedIds)
  const activeJobs = notApplied.filter(job => !dismissedSet.has(job.id))
  const dismissedJobs = notApplied.filter(job => dismissedSet.has(job.id))
  const visibleJobs = showDismissed ? notApplied : activeJobs

  // Real per-job apply: runs the match→tailor→browser pipeline scoped to this job.
  const applyMutation = useMutation({
    mutationFn: (vars: { jobId: string; jobTitle: string }) =>
      api.applyToJob(selectedCandidateId, vars.jobId),
    onSuccess: (_res, vars) => {
      toast.success(`Queued ${vars.jobTitle} for ${selectedCandidate?.name ?? 'candidate'}`)
      queryClient.invalidateQueries({ queryKey: ['applications'] })
    },
    onError: (err: unknown) => {
      toast.error(err instanceof Error ? err.message : 'Failed to queue the application.')
    },
  })
  const pendingJobId = applyMutation.isPending ? applyMutation.variables?.jobId : undefined

  const handleApply = (jobId: string) => {
    if (!selectedCandidateId) {
      toast.warning('Select a candidate first')
      return
    }
    if (applyMutation.isPending) return
    const job = rows.find(j => j.id === jobId)
    applyMutation.mutate({ jobId, jobTitle: job?.title ?? 'job' })
  }

  const handleDismiss = (jobId: string) => {
    if (dismissedSet.has(jobId)) return
    persistDismissed([...dismissedIds, jobId])
    toast.info('Job dismissed. Use "Show dismissed" below to undo.')
  }

  const handleRestore = (jobId: string) => {
    persistDismissed(dismissedIds.filter(id => id !== jobId))
    toast.success('Job restored to the feed.')
  }

  const handleCardClick = (job: JobSummary) => {
    setSelectedJob(job)
  }

  const hasPrev = page > 0
  const hasNext = jobs && jobs.length === PAGE_SIZE

  if (jobsError) {
    return (
      <div className="flex flex-col items-center justify-center min-h-[300px] bg-bg-card rounded-xl border border-bg-border p-8">
        <AlertCircle className="w-12 h-12 text-danger mb-3" />
        <h2 className="text-base font-semibold text-text-primary">Failed to load jobs</h2>
        <p className="text-text-muted text-xs mt-1 mb-6">There was an error communicating with the API.</p>
        <button
          onClick={() => refetchJobs()}
          className="btn-secondary !text-xs inline-flex items-center gap-1.5"
        >
          <RefreshCw className="w-3.5 h-3.5" />
          Retry
        </button>
      </div>
    )
  }

  return (
    <div className="space-y-6 animate-fade-in max-w-7xl mx-auto">
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 border-b border-bg-border pb-6">
        <div>
          <h1 className="page-title">Jobs Feed</h1>
          <p className="page-subtitle">
            {selectedCandidateId ? (
              <span className="flex items-center gap-1.5">
                <Eye className="w-3.5 h-3.5 text-text-muted" />
                {activeJobs.length} jobs visible
                {appliedHiddenCount > 0 && (
                  <span className="text-text-muted">
                    · {appliedHiddenCount} applied jobs hidden
                  </span>
                )}
              </span>
            ) : (
              'Discover and apply to new job opportunities.'
            )}
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          {/* Candidate Selector */}
          <select
            value={selectedCandidateId}
            onChange={(e) => setSelectedCandidateId(e.target.value)}
            disabled={candidatesLoading}
            className="input !w-auto min-w-[180px]"
          >
            <option value="">Select Candidate...</option>
            {candidates.map((cand) => (
              <option key={cand.id} value={cand.id}>
                {cand.name}
              </option>
            ))}
          </select>

          {/* Search Input */}
          <div className="relative">
            <Search className="w-4 h-4 text-text-muted absolute left-3 top-1/2 -translate-y-1/2" />
            <input
              type="text"
              placeholder="Search title or company..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="input pl-9 w-56"
            />
          </div>

          {/* Pagination Top Controls */}
          <div className="flex items-center gap-2">
            <span className="text-xs text-text-muted font-medium">Page {page + 1}</span>
            <div className="flex items-center gap-1">
              <button
                onClick={() => setPage(p => Math.max(0, p - 1))}
                disabled={!hasPrev || isFetching}
                className="btn-secondary !p-1.5"
                aria-label="Previous page"
              >
                <ChevronLeft className="w-4 h-4" />
              </button>
              <button
                onClick={() => setPage(p => p + 1)}
                disabled={!hasNext || isFetching}
                className="btn-secondary !p-1.5"
                aria-label="Next page"
              >
                <ChevronRight className="w-4 h-4" />
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* Category filter chips */}
      {categories.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <button
            onClick={() => setSelectedCategory('')}
            className={categoryChipClass(!selectedCategory)}
          >
            All
          </button>
          {categories.map((cat) => (
            <button
              key={cat.category}
              onClick={() => setSelectedCategory(cur => cur === cat.category ? '' : cat.category)}
              className={categoryChipClass(selectedCategory === cat.category)}
            >
              {cat.category}
              <span className={`ml-1.5 tabular-nums ${selectedCategory === cat.category ? 'text-white/70' : 'text-text-muted'}`}>
                {cat.count}
              </span>
            </button>
          ))}
        </div>
      )}

      {/* Grid */}
      {jobsLoading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-5">
          {Array.from({ length: PAGE_SIZE }).map((_, i) => (
            <Skeleton key={i} className="h-48 w-full rounded-xl" />
          ))}
        </div>
      ) : !visibleJobs || visibleJobs.length === 0 ? (
        <div className="flex flex-col items-center justify-center min-h-[300px] bg-bg-card rounded-2xl border border-bg-border p-12 text-center shadow-sm">
          <Briefcase className="w-12 h-12 text-text-muted mb-3 opacity-40" />
          <h2 className="text-base font-semibold text-text-primary">
            {selectedCandidateId && appliedHiddenCount > 0 ? 'All jobs applied on this page' : 'No jobs found'}
          </h2>
          <p className="text-text-muted text-xs mt-1 max-w-sm">
            {selectedCandidateId && appliedHiddenCount > 0
              ? `This candidate has applied to all ${appliedHiddenCount} jobs on this page. Try navigating to the next page.`
              : search || selectedCategory
              ? 'No jobs match your search filters.'
              : 'Check back later for new opportunities from the discovery pipeline.'}
          </p>
          {selectedCandidateId && appliedHiddenCount > 0 && hasNext && (
            <button
              onClick={() => setPage(p => p + 1)}
              disabled={isFetching}
              className="btn-primary mt-4 !text-xs"
            >
              Next Page <ChevronRight className="w-4 h-4" />
            </button>
          )}
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-5">
          {visibleJobs.map((job) => {
            const applicationStatus = selectedCandidateId ? appliedJobsMap.get(job.id) : undefined
            const isDismissed = dismissedSet.has(job.id)
            return (
              <JobCard
                key={job.id}
                job={job}
                onApply={handleApply}
                onDismiss={handleDismiss}
                onClick={handleCardClick}
                applicationStatus={applicationStatus}
                applyPending={pendingJobId === job.id}
                isDismissed={isDismissed}
                onRestore={handleRestore}
              />
            )
          })}
        </div>
      )}

      {/* Show / hide dismissed toggle */}
      {!jobsLoading && dismissedJobs.length > 0 && (
        <div className="flex justify-center">
          <button
            onClick={() => setShowDismissed(s => !s)}
            className="inline-flex items-center gap-1.5 text-xs font-medium text-text-muted hover:text-text-primary transition-colors px-3 py-1.5 rounded-full border border-bg-border bg-bg-card hover:bg-bg-hover"
          >
            {showDismissed ? (
              <><EyeOff className="w-3.5 h-3.5" /> Hide dismissed ({dismissedJobs.length})</>
            ) : (
              <><Eye className="w-3.5 h-3.5" /> Show dismissed ({dismissedJobs.length})</>
            )}
          </button>
        </div>
      )}

      {/* Job Details Modal */}
      <Modal
        open={!!selectedJob}
        onClose={() => setSelectedJob(null)}
        title={selectedJob?.title || 'Job Details'}
        size="2xl"
        footer={
          <div className="flex gap-2">
            {selectedJob?.source_url && (
              <a
                href={selectedJob.source_url}
                target="_blank"
                rel="noopener noreferrer"
                className="btn-primary !text-xs inline-flex items-center gap-1.5"
              >
                View Original Posting <ExternalLink className="w-3.5 h-3.5" />
              </a>
            )}
            <button
              onClick={() => setSelectedJob(null)}
              className="btn-secondary !text-xs"
            >
              Close
            </button>
          </div>
        }
      >
        {selectedJob && (
          <div className="space-y-6">
            <div className="text-text-muted text-xs flex items-center gap-2">
              <Building2 className="w-4 h-4 text-text-muted" />
              <span className="font-semibold text-text-primary">{selectedJob.company}</span>
              <span>•</span>
              <MapPin className="w-4 h-4 text-text-muted" />
              <span>{selectedJob.location || 'Remote'}</span>
            </div>

            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <div className="bg-bg-primary p-3 rounded-xl border border-bg-border">
                <div className="text-[10px] text-text-muted mb-1 uppercase tracking-wider font-semibold">Job Type</div>
                <div className="text-xs font-bold text-text-primary capitalize">{selectedJob.job_type?.replace('-', ' ') || 'full-time'}</div>
              </div>
              <div className="bg-bg-primary p-3 rounded-xl border border-bg-border">
                <div className="text-[10px] text-text-muted mb-1 uppercase tracking-wider font-semibold">Salary</div>
                <div className="text-xs font-bold text-text-primary">{formatSalary(selectedJob.salary_min, selectedJob.salary_max, selectedJob.pay_period)}</div>
              </div>
              <div className="bg-bg-primary p-3 rounded-xl border border-bg-border">
                <div className="text-[10px] text-text-muted mb-1 uppercase tracking-wider font-semibold">Source</div>
                <div className="text-xs font-bold text-text-primary capitalize">{selectedJob.source}</div>
              </div>
              <div className="bg-bg-primary p-3 rounded-xl border border-bg-border">
                <div className="text-[10px] text-text-muted mb-1 uppercase tracking-wider font-semibold">Status</div>
                <div className="text-xs font-bold">
                  {selectedCandidateId && appliedJobsMap.has(selectedJob.id) ? (
                    <StatusBadge status={appliedJobsMap.get(selectedJob.id)!} />
                  ) : (
                    <span className="text-text-muted">Available</span>
                  )}
                </div>
              </div>
            </div>

            {selectedJob.skills && selectedJob.skills.length > 0 && (
              <div className="space-y-2">
                <h3 className="text-xs font-semibold text-text-primary uppercase tracking-wider">Required Skills</h3>
                <div className="flex flex-wrap gap-1.5">
                  {selectedJob.skills.map((skill: string, i: number) => (
                    <span key={i} className="px-2.5 py-1 bg-bg-secondary text-text-secondary border border-bg-border rounded-lg text-xs font-medium">
                      {skill}
                    </span>
                  ))}
                </div>
              </div>
            )}

            <div className="space-y-2">
              <h3 className="flex items-center gap-2 text-xs font-semibold text-text-primary uppercase tracking-wider">
                <FileText className="w-4 h-4" />
                Job Description
              </h3>
              <div className="bg-bg-primary p-4 rounded-xl border border-bg-border text-xs text-text-secondary whitespace-pre-wrap leading-relaxed max-h-80 overflow-y-auto">
                {selectedJob.description || 'No description provided for this posting.'}
              </div>
            </div>
          </div>
        )}
      </Modal>
    </div>
  )
}
