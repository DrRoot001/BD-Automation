'use client'

import { useState, useEffect } from 'react'
import { useAdminJobs, useAdminJobsCount } from '@/hooks/useAdminJobs'
import { Search, ChevronLeft, ChevronRight, Eye, Briefcase, MapPin, Building2, Calendar, FileText, ExternalLink } from 'lucide-react'
import { formatDistanceToNow, formatSalary } from '@/lib/utils'
import { Modal } from '@/components/ui/Modal'
import { StatusBadge } from '@/components/shared/StatusBadge'
import { JobSummary } from '@/lib/api'

export default function JobManagementPage() {
  const [search, setSearch] = useState('')
  const [sourceFilter, setSourceFilter] = useState('')
  const [typeFilter, setTypeFilter] = useState('')
  const [timeFilter, setTimeFilter] = useState('')
  const [page, setPage] = useState(0)
  const limit = 50
  
  const activeFilters = {
    search: search.trim() || undefined,
    source: sourceFilter || undefined,
    jobType: typeFilter || undefined,
    timeFilter: timeFilter || undefined,
  }

  const { data: jobs, isLoading, isError } = useAdminJobs(page * limit, limit, activeFilters)
  const { data: countData } = useAdminJobsCount(activeFilters)
  const totalCount = countData?.total_count ?? 0
  const totalPages = Math.ceil(totalCount / limit)

  const [selectedJob, setSelectedJob] = useState<JobSummary | null>(null)

  // Reset page to 0 on filter change
  useEffect(() => {
    setPage(0)
  }, [search, sourceFilter, typeFilter, timeFilter])

  const filteredJobs = jobs

  const handleNextPage = () => setPage(p => p + 1)
  const handlePrevPage = () => setPage(p => Math.max(0, p - 1))

  return (
    <div className="space-y-6 max-w-7xl mx-auto animate-fade-in">
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 border-b border-bg-border pb-6">
        <div>
          <h1 className="page-title">Job Management</h1>
          <p className="page-subtitle">Manage scraped jobs and review deduplication status.</p>
        </div>
        
        <div className="flex flex-wrap items-center gap-3">
          <div className="relative">
            <Search className="w-4 h-4 text-text-muted absolute left-3 top-1/2 -translate-y-1/2" />
            <input 
              type="text" 
              placeholder="Search company or title..." 
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="input pl-9 w-60"
            />
          </div>
          
          <select 
            value={sourceFilter}
            onChange={(e) => setSourceFilter(e.target.value)}
            className="input !w-auto"
          >
            <option value="">All Sources</option>
            <option value="greenhouse">Greenhouse</option>
            <option value="lever">Lever</option>
            <option value="dice">Dice</option>
            <option value="remote100k">Remote100k</option>
            <option value="remoterocketship">Remote Rocketship</option>
            <option value="rss_generic">RSS Feeds</option>
            <option value="linkedin">LinkedIn</option>
          </select>
          
          <select 
            value={typeFilter}
            onChange={(e) => setTypeFilter(e.target.value)}
            className="input !w-auto"
          >
            <option value="">All Types</option>
            <option value="full">Full-time</option>
            <option value="contract">Contract</option>
            <option value="part">Part-time</option>
          </select>
          
          <select 
            value={timeFilter}
            onChange={(e) => setTimeFilter(e.target.value)}
            className="input !w-auto"
          >
            <option value="">Any Time</option>
            <option value="24h">Last 24 Hours</option>
            <option value="3d">Last 3 Days</option>
            <option value="7d">Last 7 Days</option>
            <option value="30d">Last 30 Days</option>
          </select>
        </div>
      </div>

      {/* Error State */}
      {isError && (
        <div className="bg-danger/10 border border-danger/20 rounded-xl p-6 text-center">
          <h3 className="text-base font-semibold text-danger mb-1">Failed to load jobs</h3>
          <p className="text-danger/80 text-xs">There was an error communicating with the server. Please try again.</p>
        </div>
      )}

      {/* Table Area */}
      {!isError && (
        <div className="card bg-bg-card border border-bg-border rounded-xl shadow-sm flex flex-col">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm whitespace-nowrap">
              <thead className="bg-bg-primary text-text-muted uppercase text-xs tracking-wider border-b border-bg-border">
                <tr>
                  <th className="px-6 py-4 font-medium">Job Title & Company</th>
                  <th className="px-6 py-4 font-medium">Location</th>
                  <th className="px-6 py-4 font-medium">Type & Source</th>
                  <th className="px-6 py-4 font-medium">Salary</th>
                  <th className="px-6 py-4 font-medium">Posted</th>
                  <th className="px-6 py-4 font-medium text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-bg-border">
                {isLoading ? (
                  Array.from({ length: 10 }).map((_, i) => (
                    <tr key={i}>
                      <td className="px-6 py-4">
                        <div className="h-4 bg-bg-border rounded w-48 animate-pulse mb-2"></div>
                        <div className="h-3 bg-bg-border rounded w-32 animate-pulse"></div>
                      </td>
                      <td className="px-6 py-4"><div className="h-4 bg-bg-border rounded w-24 animate-pulse"></div></td>
                      <td className="px-6 py-4">
                        <div className="h-4 bg-bg-border rounded w-20 animate-pulse mb-2"></div>
                        <div className="h-3 bg-bg-border rounded w-16 animate-pulse"></div>
                      </td>
                      <td className="px-6 py-4"><div className="h-4 bg-bg-border rounded w-24 animate-pulse"></div></td>
                      <td className="px-6 py-4"><div className="h-4 bg-bg-border rounded w-20 animate-pulse"></div></td>
                      <td className="px-6 py-4 text-right"><div className="h-8 bg-bg-border rounded w-20 animate-pulse ml-auto"></div></td>
                    </tr>
                  ))
                ) : !filteredJobs || filteredJobs.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="px-6 py-16 text-center">
                      <div className="flex flex-col items-center justify-center">
                        <Briefcase className="w-12 h-12 text-bg-border mb-4 opacity-50" />
                        <h3 className="text-base font-semibold text-text-primary mb-1">No jobs found</h3>
                        <p className="text-text-muted text-xs max-w-sm mb-4">
                          {search || sourceFilter || typeFilter || timeFilter ? "No jobs match your current filters." : "No jobs have been scraped or imported yet."}
                        </p>
                      </div>
                    </td>
                  </tr>
                ) : (
                  filteredJobs.map((job) => (
                    <tr key={job.id} className={`hover:bg-bg-hover transition-colors ${job.is_duplicate ? 'opacity-60' : ''}`}>
                      <td className="px-6 py-4">
                        <div className="flex items-center gap-2">
                          <span className="font-medium text-text-primary">{job.title}</span>
                          {job.is_duplicate && (
                            <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-bg-secondary text-text-muted border border-bg-border">Duplicate</span>
                          )}
                        </div>
                        <div className="text-text-muted text-xs mt-0.5 flex items-center gap-1">
                          <Building2 className="w-3 h-3" />
                          {job.company}
                        </div>
                      </td>
                      <td className="px-6 py-4 text-text-muted text-xs">
                        <div className="flex items-center gap-1">
                          <MapPin className="w-3 h-3" />
                          <span className="truncate max-w-[150px]">{job.location || 'Remote'}</span>
                        </div>
                      </td>
                      <td className="px-6 py-4 text-xs">
                        <div className="capitalize font-medium text-text-primary mb-0.5">{job.job_type?.replace('-', ' ') || 'Unknown'}</div>
                        <div className="text-text-muted capitalize">{job.source}</div>
                      </td>
                      <td className="px-6 py-4 text-text-muted text-xs">
                        {formatSalary(job.salary_min, job.salary_max, job.pay_period)}
                      </td>
                      <td className="px-6 py-4 text-text-muted whitespace-nowrap text-xs">
                        <div className="flex items-center gap-1">
                          <Calendar className="w-3 h-3" />
                          {job.posted_at ? formatDistanceToNow(job.posted_at) + ' ago' : 'Unknown'}
                        </div>
                      </td>
                      <td className="px-6 py-4 text-right">
                        <button 
                          onClick={() => setSelectedJob(job)}
                          className="btn-secondary !py-1.5 !px-3 !text-xs"
                        >
                          <Eye className="w-3.5 h-3.5" />
                          Details
                        </button>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
          
          {/* Pagination */}
          <div className="px-6 py-4 border-t border-bg-border flex items-center justify-between">
            <div className="text-xs text-text-muted">
              Showing page {page + 1} of {Math.max(1, totalPages)} ({totalCount} total jobs)
            </div>
            <div className="flex items-center gap-2">
              <button 
                onClick={handlePrevPage}
                disabled={page === 0 || isLoading}
                className="btn-secondary !p-1.5"
                aria-label="Previous page"
              >
                <ChevronLeft className="w-4 h-4" />
              </button>
              <button 
                onClick={handleNextPage}
                disabled={page + 1 >= totalPages || isLoading}
                className="btn-secondary !p-1.5"
                aria-label="Next page"
              >
                <ChevronRight className="w-4 h-4" />
              </button>
            </div>
          </div>
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
                View Original <ExternalLink className="w-3.5 h-3.5" />
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
              <Building2 className="w-4 h-4" />
              <span className="font-semibold text-text-primary">{selectedJob.company}</span>
              <span>•</span>
              <MapPin className="w-4 h-4" />
              <span>{selectedJob.location || 'Remote'}</span>
            </div>

            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <div className="bg-bg-primary p-3 rounded-xl border border-bg-border">
                <div className="text-[10px] text-text-muted mb-1 uppercase tracking-wider font-semibold">Job Type</div>
                <div className="text-xs font-bold text-text-primary capitalize">{selectedJob.job_type?.replace('-', ' ') || 'Not specified'}</div>
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
                  {selectedJob.is_duplicate ? <span className="text-amber-500 font-semibold">Duplicate</span> : <StatusBadge status="FOUND" />}
                </div>
              </div>
            </div>

            {selectedJob.skills && selectedJob.skills.length > 0 && (
              <div>
                <h3 className="text-xs font-semibold text-text-primary uppercase tracking-wider mb-2">Required Skills</h3>
                <div className="flex flex-wrap gap-1.5">
                  {selectedJob.skills.map((skill: string, i: number) => (
                    <span key={i} className="px-2.5 py-1 bg-bg-secondary text-text-secondary border border-bg-border rounded-lg text-xs font-medium">
                      {skill}
                    </span>
                  ))}
                </div>
              </div>
            )}

            <div>
              <h3 className="flex items-center gap-2 text-xs font-semibold text-text-primary uppercase tracking-wider mb-2">
                <FileText className="w-4 h-4" />
                Job Description
              </h3>
              <div className="bg-bg-primary p-4 rounded-xl border border-bg-border text-xs text-text-secondary whitespace-pre-wrap leading-relaxed max-h-80 overflow-y-auto">
                {selectedJob.description || 'No description provided.'}
              </div>
            </div>
          </div>
        )}
      </Modal>
    </div>
  )
}
