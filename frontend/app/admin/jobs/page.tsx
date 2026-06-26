'use client'

import { useState } from 'react'
import { useAdminJobs } from '@/hooks/useAdminJobs'
import { Search, ChevronLeft, ChevronRight, Eye, Briefcase, MapPin, Building2, Calendar, FileText } from 'lucide-react'
import { formatDistanceToNow } from '@/lib/utils'

export default function JobManagementPage() {
  const [search, setSearch] = useState('')
  const [sourceFilter, setSourceFilter] = useState('')
  const [typeFilter, setTypeFilter] = useState('')
  const [page, setPage] = useState(0)
  const limit = 50
  
  const { data: jobs, isLoading, isError } = useAdminJobs(page * limit, limit)

  const [selectedJob, setSelectedJob] = useState<any | null>(null)

  const filteredJobs = jobs?.filter(job => {
    const matchesSearch = job.company.toLowerCase().includes(search.toLowerCase()) || 
                          job.title.toLowerCase().includes(search.toLowerCase());
    const matchesSource = sourceFilter ? job.source === sourceFilter : true;
    const matchesType = typeFilter ? job.job_type === typeFilter : true;
    return matchesSearch && matchesSource && matchesType;
  })

  // Format salary
  const formatSalary = (min: number | null, max: number | null, period: string | null) => {
    if (!min && !max) return 'Not specified'
    const formatter = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 })
    if (min && max) return `${formatter.format(min)} - ${formatter.format(max)}${period ? `/${period}` : ''}`
    if (min) return `${formatter.format(min)}+${period ? `/${period}` : ''}`
    if (max) return `Up to ${formatter.format(max)}${period ? `/${period}` : ''}`
    return 'Not specified'
  }

  const handleNextPage = () => setPage(p => p + 1)
  const handlePrevPage = () => setPage(p => Math.max(0, p - 1))

  return (
    <div className="space-y-6 max-w-7xl mx-auto animate-in fade-in duration-500">
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-text-primary tracking-tight">Job Management</h1>
          <p className="text-sm text-text-muted mt-1">Manage scraped jobs and their deduplication status.</p>
        </div>
        
        <div className="flex flex-wrap items-center gap-3">
          <div className="relative">
            <Search className="w-4 h-4 text-text-muted absolute left-3 top-1/2 -translate-y-1/2" />
            <input 
              type="text" 
              placeholder="Search company or title..." 
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="pl-9 pr-4 py-2 bg-bg-primary border border-bg-border rounded-lg text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent transition-colors w-60"
            />
          </div>
          
          <select 
            value={sourceFilter}
            onChange={(e) => setSourceFilter(e.target.value)}
            className="px-3 py-2 bg-bg-primary border border-bg-border rounded-lg text-sm text-text-primary focus:outline-none focus:border-accent"
          >
            <option value="">All Sources</option>
            <option value="ycombinator">Y Combinator</option>
            <option value="linkedin">LinkedIn</option>
            <option value="greenhouse">Greenhouse</option>
            <option value="lever">Lever</option>
          </select>
          
          <select 
            value={typeFilter}
            onChange={(e) => setTypeFilter(e.target.value)}
            className="px-3 py-2 bg-bg-primary border border-bg-border rounded-lg text-sm text-text-primary focus:outline-none focus:border-accent"
          >
            <option value="">All Types</option>
            <option value="full-time">Full-time</option>
            <option value="contract">Contract</option>
            <option value="part-time">Part-time</option>
          </select>
        </div>
      </div>

      {/* Error State */}
      {isError && (
        <div className="bg-danger/10 border border-danger/20 rounded-xl p-6 text-center">
          <h3 className="text-lg font-medium text-danger mb-2">Failed to load jobs</h3>
          <p className="text-danger/80 text-sm">There was an error communicating with the server. Please try again later.</p>
        </div>
      )}

      {/* Table Area */}
      {!isError && (
        <div className="bg-bg-secondary border border-bg-border rounded-xl shadow-sm flex flex-col">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm whitespace-nowrap">
              <thead className="bg-bg-primary/50 text-text-muted uppercase text-xs tracking-wider border-b border-bg-border">
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
                        <Briefcase className="w-12 h-12 text-bg-border mb-4" />
                        <h3 className="text-lg font-medium text-text-primary mb-1">No jobs found</h3>
                        <p className="text-text-muted text-sm max-w-sm mb-4">
                          {search || sourceFilter || typeFilter ? "No jobs match your current filters." : "No jobs have been scraped or imported yet."}
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
                            <span className="px-1.5 py-0.5 rounded text-[10px] font-medium bg-bg-border text-text-muted">Duplicate</span>
                          )}
                        </div>
                        <div className="text-text-muted mt-0.5 flex items-center gap-1">
                          <Building2 className="w-3 h-3" />
                          {job.company}
                        </div>
                      </td>
                      <td className="px-6 py-4 text-text-muted">
                        <div className="flex items-center gap-1">
                          <MapPin className="w-3 h-3" />
                          <span className="truncate max-w-[150px]">{job.location || 'Remote'}</span>
                        </div>
                      </td>
                      <td className="px-6 py-4">
                        <div className="capitalize text-text-primary mb-0.5">{job.job_type?.replace('-', ' ') || 'Unknown'}</div>
                        <div className="text-xs text-text-muted capitalize">{job.source}</div>
                      </td>
                      <td className="px-6 py-4 text-text-muted text-sm">
                        {formatSalary(job.salary_min, job.salary_max, job.pay_period)}
                      </td>
                      <td className="px-6 py-4 text-text-muted whitespace-nowrap text-sm">
                        <div className="flex items-center gap-1">
                          <Calendar className="w-3 h-3" />
                          {job.posted_at ? formatDistanceToNow(new Date(job.posted_at)) + ' ago' : 'Unknown'}
                        </div>
                      </td>
                      <td className="px-6 py-4 text-right">
                        <button 
                          onClick={() => setSelectedJob(job)}
                          className="inline-flex items-center gap-1 px-3 py-1.5 bg-bg-primary border border-bg-border hover:bg-bg-hover text-text-primary rounded-md text-xs font-medium transition-colors"
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
            <div className="text-sm text-text-muted">
              Showing page {page + 1}
            </div>
            <div className="flex items-center gap-2">
              <button 
                onClick={handlePrevPage}
                disabled={page === 0 || isLoading}
                className="p-1.5 rounded-md border border-bg-border text-text-primary hover:bg-bg-hover disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                <ChevronLeft className="w-4 h-4" />
              </button>
              <button 
                onClick={handleNextPage}
                disabled={!jobs || jobs.length < limit || isLoading}
                className="p-1.5 rounded-md border border-bg-border text-text-primary hover:bg-bg-hover disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                <ChevronRight className="w-4 h-4" />
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Job Details Modal */}
      {selectedJob && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4">
          <div className="w-full max-w-2xl max-h-[85vh] bg-bg-secondary rounded-2xl shadow-2xl border border-bg-border flex flex-col animate-in zoom-in-95 duration-200">
            <div className="p-6 border-b border-bg-border flex flex-col sm:flex-row sm:items-center justify-between gap-4 shrink-0">
              <div>
                <h2 className="text-xl font-bold text-text-primary">{selectedJob.title}</h2>
                <div className="text-text-muted flex items-center gap-2 mt-1">
                  <Building2 className="w-4 h-4" />
                  <span className="font-medium">{selectedJob.company}</span>
                  <span>•</span>
                  <MapPin className="w-4 h-4" />
                  <span>{selectedJob.location || 'Remote'}</span>
                </div>
              </div>
              <div className="flex gap-2">
                <a 
                  href={selectedJob.source_url} 
                  target="_blank" 
                  rel="noopener noreferrer"
                  className="px-4 py-2 bg-accent hover:bg-accent-hover text-white rounded-lg text-sm font-medium transition-colors"
                >
                  View Original
                </a>
                <button 
                  onClick={() => setSelectedJob(null)} 
                  className="px-4 py-2 bg-bg-primary border border-bg-border hover:bg-bg-hover text-text-primary rounded-lg text-sm font-medium transition-colors"
                >
                  Close
                </button>
              </div>
            </div>
            
            <div className="p-6 overflow-y-auto space-y-6">
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                <div className="bg-bg-primary p-3 rounded-lg border border-bg-border">
                  <div className="text-xs text-text-muted mb-1 uppercase tracking-wider">Job Type</div>
                  <div className="font-medium text-text-primary capitalize">{selectedJob.job_type?.replace('-', ' ') || 'Not specified'}</div>
                </div>
                <div className="bg-bg-primary p-3 rounded-lg border border-bg-border">
                  <div className="text-xs text-text-muted mb-1 uppercase tracking-wider">Salary</div>
                  <div className="font-medium text-text-primary">{formatSalary(selectedJob.salary_min, selectedJob.salary_max, selectedJob.pay_period)}</div>
                </div>
                <div className="bg-bg-primary p-3 rounded-lg border border-bg-border">
                  <div className="text-xs text-text-muted mb-1 uppercase tracking-wider">Source</div>
                  <div className="font-medium text-text-primary capitalize">{selectedJob.source}</div>
                </div>
                <div className="bg-bg-primary p-3 rounded-lg border border-bg-border">
                  <div className="text-xs text-text-muted mb-1 uppercase tracking-wider">Status</div>
                  <div className="font-medium text-text-primary">
                    {selectedJob.is_duplicate ? <span className="text-amber-500">Duplicate</span> : <span className="text-green-500">Active</span>}
                  </div>
                </div>
              </div>

              {selectedJob.skills && selectedJob.skills.length > 0 && (
                <div>
                  <h3 className="text-sm font-semibold text-text-primary uppercase tracking-wider mb-3">Required Skills</h3>
                  <div className="flex flex-wrap gap-2">
                    {selectedJob.skills.map((skill: string, i: number) => (
                      <span key={i} className="px-2.5 py-1 bg-accent/10 text-accent border border-accent/20 rounded-md text-xs font-medium">
                        {skill}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              <div>
                <h3 className="flex items-center gap-2 text-sm font-semibold text-text-primary uppercase tracking-wider mb-3">
                  <FileText className="w-4 h-4" />
                  Job Description
                </h3>
                <div className="bg-bg-primary p-4 rounded-lg border border-bg-border text-sm text-text-secondary whitespace-pre-wrap leading-relaxed max-h-96 overflow-y-auto">
                  {selectedJob.description || 'No description provided.'}
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
