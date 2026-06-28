'use client'

import { useState, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, JobSummary } from '@/lib/api'
import { 
  Search, RefreshCw, CheckCircle, AlertCircle, X, 
  Briefcase, MapPin, Building2, Calendar, FileText, 
  ExternalLink, Play, Eye, Sparkles, Terminal, ShieldAlert
} from 'lucide-react'
import { formatDistanceToNow } from '@/lib/utils'

export default function JobDiscoveryPage() {
  const [isDiscoveryRunning, setIsDiscoveryRunning] = useState(false)
  const [discoveryStatus, setDiscoveryStatus] = useState<any>(null)
  const [errorMsg, setErrorMsg] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [sourceFilter, setSourceFilter] = useState('')
  const [selectedJob, setSelectedJob] = useState<JobSummary | null>(null)

  // Query for the 25 most recently scraped jobs
  const { data: jobs, isLoading: jobsLoading, refetch: refetchJobs } = useQuery({
    queryKey: ['admin-scraped-jobs'],
    queryFn: () => api.getJobs({ limit: 25 }),
    refetchInterval: isDiscoveryRunning ? 5000 : 30000,
  })

  // Poll discovery status
  useQuery({
    queryKey: ['admin-discovery-status'],
    queryFn: async () => {
      const res = await api.getDiscoveryStatus()
      if (res.running) {
        setIsDiscoveryRunning(true)
      } else {
        setIsDiscoveryRunning(false)
        if (res.last_result && isDiscoveryRunning) {
          setDiscoveryStatus(res.last_result)
          refetchJobs() // Refresh the list of jobs when discovery finishes
        }
      }
      return res
    },
    refetchInterval: isDiscoveryRunning ? 3000 : 15000,
  })

  const handleRunDiscovery = async () => {
    setIsDiscoveryRunning(true)
    setDiscoveryStatus(null)
    setErrorMsg(null)
    try {
      await api.triggerJobDiscovery()
    } catch (err: any) {
      console.error(err)
      setIsDiscoveryRunning(false)
      const detail = err.response?.data?.detail
      setErrorMsg(typeof detail === 'string' ? detail : 'Failed to start job discovery')
    }
  }

  // Filter jobs locally
  const filteredJobs = jobs?.filter(job => {
    const matchesSearch = job.title.toLowerCase().includes(search.toLowerCase()) || 
                          job.company.toLowerCase().includes(search.toLowerCase())
    const matchesSource = sourceFilter ? job.source.toLowerCase() === sourceFilter.toLowerCase() : true
    return matchesSearch && matchesSource
  })

  // Format salary utility
  const formatSalary = (min: number | null, max: number | null, period: string | null) => {
    if (!min && !max) return 'Not specified'
    const formatter = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 })
    if (min && max) return `${formatter.format(min)} - ${formatter.format(max)}${period ? `/${period}` : ''}`
    if (min) return `${formatter.format(min)}+${period ? `/${period}` : ''}`
    if (max) return `Up to ${formatter.format(max)}${period ? `/${period}` : ''}`
    return 'Not specified'
  }

  return (
    <div className="space-y-8 max-w-7xl mx-auto animate-in fade-in duration-500">
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 border-b border-bg-border pb-6">
        <div>
          <h1 className="text-2xl font-bold text-text-primary tracking-tight">Scraper Control & Discovery Console</h1>
          <p className="text-sm text-text-muted mt-1">
            Trigger the Node.js scraper pipeline, monitor active execution states, and review ingested jobs in real-time.
          </p>
        </div>
      </div>

      {/* Main Control Panel and Logs */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Scraper Trigger & Status Column */}
        <div className="lg:col-span-1 space-y-6">
          <div className="card p-6 bg-bg-secondary border border-bg-border rounded-2xl shadow-sm relative overflow-hidden flex flex-col justify-between min-h-[280px]">
            <div className="absolute top-0 right-0 p-6 opacity-5 pointer-events-none">
              <Terminal className="w-36 h-36" />
            </div>
            
            <div className="space-y-4">
              <div className="flex items-center gap-2">
                <Sparkles className="w-5 h-5 text-accent animate-pulse" />
                <h2 className="text-lg font-bold text-text-primary">Pipeline Trigger</h2>
              </div>
              <p className="text-xs text-text-muted leading-relaxed">
                Initiates the headless Puppeteer and Gemini scraper. The crawler parses configured job feeds (Dice, RemoteRocketship, custom companies), runs clearances & remote filters, and saves jobs.
              </p>
            </div>

            <div className="mt-6 space-y-3">
              <button
                onClick={handleRunDiscovery}
                disabled={isDiscoveryRunning}
                className="w-full flex items-center justify-center gap-2.5 px-5 py-3 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed text-white font-semibold text-sm rounded-xl transition-all duration-200 shadow-md shadow-blue-500/10 active:scale-[0.98]"
              >
                {isDiscoveryRunning ? (
                  <>
                    <RefreshCw className="w-4 h-4 animate-spin" />
                    Ingestion in Progress...
                  </>
                ) : (
                  <>
                    <Play className="w-4 h-4 fill-current" />
                    Start Scraper Pipeline
                  </>
                )}
              </button>
              
              {isDiscoveryRunning && (
                <div className="flex items-center justify-center gap-2 py-2 bg-blue-500/5 rounded-lg border border-blue-500/10 text-xs text-blue-500 font-medium animate-pulse">
                  <span className="w-1.5 h-1.5 rounded-full bg-blue-500"></span>
                  Active run may take 5–15 minutes
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Live Execution Summary / Statistics Column */}
        <div className="lg:col-span-2 space-y-6">
          <div className="card p-6 bg-bg-secondary border border-bg-border rounded-2xl shadow-sm min-h-[280px] flex flex-col justify-between">
            <div className="space-y-4">
              <h2 className="text-lg font-bold text-text-primary flex items-center gap-2">
                <Terminal className="w-5 h-5 text-text-muted" />
                Run Status & Summary
              </h2>
              
              {errorMsg && (
                <div className="flex items-center gap-3 bg-red-500/10 border border-red-500/20 p-4 rounded-xl text-sm text-red-500">
                  <AlertCircle className="w-5 h-5 shrink-0" />
                  <div className="flex-1 font-medium">{errorMsg}</div>
                  <button onClick={() => setErrorMsg(null)} className="hover:text-red-400">
                    <X className="w-4 h-4" />
                  </button>
                </div>
              )}

              {!isDiscoveryRunning && !discoveryStatus && !errorMsg && (
                <div className="flex flex-col items-center justify-center py-10 text-center text-text-muted border border-dashed border-bg-border rounded-xl">
                  <Briefcase className="w-8 h-8 opacity-40 mb-2" />
                  <p className="text-xs">No active or recently completed scraper run logged in this session.</p>
                  <p className="text-[10px] opacity-70 mt-1">Click "Start Scraper Pipeline" to run the discovery process.</p>
                </div>
              )}

              {isDiscoveryRunning && (
                <div className="flex flex-col items-center justify-center py-10 text-center border border-blue-500/10 bg-blue-500/5 rounded-xl animate-pulse">
                  <RefreshCw className="w-8 h-8 text-blue-500 animate-spin mb-3" />
                  <h3 className="font-semibold text-sm text-blue-500">Headless Crawler Active</h3>
                  <p className="text-xs text-text-muted max-w-md mt-1">
                    Node subprocesses are running live. Jobs are currently being scanned, parsed, normalized, and saved to the database.
                  </p>
                </div>
              )}

              {discoveryStatus && (
                <div className={`p-5 rounded-xl border animate-in slide-in-from-top duration-300 ${
                  discoveryStatus.status === 'completed' 
                    ? 'bg-green-500/5 border-green-500/10 text-green-500' 
                    : 'bg-red-500/5 border-red-500/10 text-red-500'
                }`}>
                  <div className="flex items-start gap-3">
                    {discoveryStatus.status === 'completed' ? (
                      <CheckCircle className="w-5 h-5 shrink-0 mt-0.5" />
                    ) : (
                      <ShieldAlert className="w-5 h-5 shrink-0 mt-0.5" />
                    )}
                    <div className="flex-1 space-y-3">
                      <div>
                        <div className="font-bold text-sm">Discovery {discoveryStatus.status}</div>
                        <div className="text-xs text-text-muted mt-0.5">Summary from the last run:</div>
                      </div>
                      
                      {discoveryStatus.status === 'completed' && (
                        <div className="grid grid-cols-2 gap-4">
                          <div className="bg-bg-primary/50 border border-bg-border p-3 rounded-lg text-center">
                            <div className="text-xl font-bold text-text-primary">{discoveryStatus.total_discovered}</div>
                            <div className="text-[9px] uppercase tracking-wider text-text-muted font-medium mt-1">Scraped / Scanned</div>
                          </div>
                          <div className="bg-bg-primary/50 border border-bg-border p-3 rounded-lg text-center">
                            <div className="text-xl font-bold text-green-500">{discoveryStatus.total_saved}</div>
                            <div className="text-[9px] uppercase tracking-wider text-text-muted font-medium mt-1">Ingested / Unique</div>
                          </div>
                        </div>
                      )}

                      {discoveryStatus.errors && discoveryStatus.errors.length > 0 && (
                        <div className="space-y-1.5 pt-2 border-t border-bg-border">
                          <div className="text-[10px] font-bold uppercase tracking-wider text-text-muted">Encountered Issues:</div>
                          <div className="text-xs text-red-400 bg-red-500/5 border border-red-500/10 p-2.5 rounded-lg max-h-[90px] overflow-y-auto font-mono">
                            {discoveryStatus.errors.map((err: string, i: number) => (
                              <div key={i}>• {err}</div>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              )}
            </div>
            
            {discoveryStatus && (
              <div className="flex justify-end mt-4">
                <button 
                  onClick={() => setDiscoveryStatus(null)} 
                  className="flex items-center gap-1.5 text-xs text-text-muted hover:text-text-primary px-3 py-1.5 bg-bg-primary hover:bg-bg-hover border border-bg-border rounded-lg transition-colors"
                >
                  <X className="w-3.5 h-3.5" /> Clear Report
                </button>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Recently Ingested Jobs */}
      <div className="card p-6 bg-bg-secondary border border-bg-border rounded-2xl shadow-sm">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-bg-border pb-5 mb-6">
          <div>
            <h2 className="text-lg font-bold text-text-primary">Recently Ingested Jobs</h2>
            <p className="text-xs text-text-muted mt-1">Showing the 25 most recent job postings crawled by the scraper.</p>
          </div>
          
          <div className="flex flex-wrap items-center gap-3">
            <div className="relative">
              <Search className="w-4 h-4 text-text-muted absolute left-3 top-1/2 -translate-y-1/2" />
              <input 
                type="text" 
                placeholder="Filter by company or title..." 
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="pl-9 pr-4 py-2 bg-bg-primary border border-bg-border rounded-lg text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent transition-colors w-60"
              />
            </div>

            <select 
              value={sourceFilter}
              onChange={(e) => setSourceFilter(e.target.value)}
              className="px-3 py-2 bg-bg-primary border border-bg-border rounded-lg text-sm text-text-primary focus:outline-none focus:border-accent capitalize"
            >
              <option value="">All Sources</option>
              <option value="dice">Dice</option>
              <option value="greenhouse">Greenhouse</option>
              <option value="lever">Lever</option>
              <option value="weworkremotely">WeWorkRemotely</option>
              <option value="remotive">Remotive</option>
              <option value="rss_generic">RSS Feeds</option>
            </select>

            <button 
              onClick={() => refetchJobs()}
              className="p-2 bg-bg-primary border border-bg-border hover:bg-bg-hover text-text-primary rounded-lg transition-colors"
              title="Refresh Listing"
            >
              <RefreshCw className={`w-4 h-4 ${jobsLoading ? 'animate-spin' : ''}`} />
            </button>
          </div>
        </div>

        {/* Jobs Table */}
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm whitespace-nowrap">
            <thead className="bg-bg-primary/50 text-text-muted uppercase text-xs tracking-wider border-b border-bg-border">
              <tr>
                <th className="px-6 py-4 font-medium">Job Title & Company</th>
                <th className="px-6 py-4 font-medium">Location</th>
                <th className="px-6 py-4 font-medium">Source & Type</th>
                <th className="px-6 py-4 font-medium">Salary Range</th>
                <th className="px-6 py-4 font-medium">Scraped At</th>
                <th className="px-6 py-4 font-medium text-right">Details</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-bg-border">
              {jobsLoading ? (
                Array.from({ length: 5 }).map((_, i) => (
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
                      <h3 className="text-base font-semibold text-text-primary mb-1">No jobs available</h3>
                      <p className="text-text-muted text-sm max-w-sm">
                        {search || sourceFilter ? "No jobs match your current search parameters." : "No jobs have been scraped yet."}
                      </p>
                    </div>
                  </td>
                </tr>
              ) : (
                filteredJobs.map((job) => (
                  <tr key={job.id} className={`hover:bg-bg-hover transition-colors ${job.is_duplicate ? 'opacity-65' : ''}`}>
                    <td className="px-6 py-4">
                      <div className="flex items-center gap-2">
                        <span className="font-semibold text-text-primary">{job.title}</span>
                        {job.is_duplicate && (
                          <span className="px-1.5 py-0.5 rounded text-[9px] font-bold bg-bg-border text-text-muted">DUPLICATE</span>
                        )}
                      </div>
                      <div className="text-text-muted text-xs mt-0.5 flex items-center gap-1.5">
                        <Building2 className="w-3.5 h-3.5" />
                        {job.company}
                      </div>
                    </td>
                    <td className="px-6 py-4 text-text-muted text-xs">
                      <div className="flex items-center gap-1.5">
                        <MapPin className="w-3.5 h-3.5" />
                        <span>{job.location || 'Remote'}</span>
                      </div>
                    </td>
                    <td className="px-6 py-4 text-xs">
                      <div className="capitalize font-semibold text-text-primary mb-0.5">
                        {job.job_type?.replace('-', ' ') || 'full-time'}
                      </div>
                      <div className="text-text-muted capitalize">{job.source}</div>
                    </td>
                    <td className="px-6 py-4 text-text-muted text-xs">
                      {formatSalary(job.salary_min, job.salary_max, job.pay_period)}
                    </td>
                    <td className="px-6 py-4 text-text-muted text-xs">
                      <div className="flex items-center gap-1.5">
                        <Calendar className="w-3.5 h-3.5" />
                        {formatDistanceToNow(new Date(job.created_at))} ago
                      </div>
                    </td>
                    <td className="px-6 py-4 text-right">
                      <button 
                        onClick={() => setSelectedJob(job)}
                        className="inline-flex items-center gap-1 px-3 py-1.5 bg-bg-primary border border-bg-border hover:bg-bg-hover text-text-primary rounded-lg text-xs font-semibold transition-colors active:scale-95"
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
      </div>

      {/* Job Details Modal */}
      {selectedJob && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4 overflow-y-auto animate-in fade-in duration-200">
          <div className="w-full max-w-2xl max-h-[85vh] bg-bg-secondary rounded-2xl shadow-2xl border border-bg-border flex flex-col animate-in zoom-in-95 duration-200">
            <div className="p-6 border-b border-bg-border flex flex-col sm:flex-row sm:items-center justify-between gap-4 shrink-0">
              <div>
                <h2 className="text-lg font-bold text-text-primary">{selectedJob.title}</h2>
                <div className="text-text-muted text-xs flex items-center gap-2 mt-1.5">
                  <Building2 className="w-4 h-4" />
                  <span className="font-semibold">{selectedJob.company}</span>
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
                  className="inline-flex items-center gap-1.5 px-4 py-2 bg-accent hover:bg-accent-hover text-white rounded-xl text-xs font-semibold transition-colors"
                >
                  View Original <ExternalLink className="w-3.5 h-3.5" />
                </a>
                <button 
                  onClick={() => setSelectedJob(null)} 
                  className="px-4 py-2 bg-bg-primary border border-bg-border hover:bg-bg-hover text-text-primary rounded-xl text-xs font-semibold transition-colors"
                >
                  Close
                </button>
              </div>
            </div>
            
            <div className="p-6 overflow-y-auto space-y-6">
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
                  <div className="text-[10px] text-text-muted mb-1 uppercase tracking-wider font-semibold">Deduplication</div>
                  <div className="text-xs font-bold">
                    {selectedJob.is_duplicate ? (
                      <span className="text-amber-500">Duplicate (Ignored)</span>
                    ) : (
                      <span className="text-green-500">Unique (Active)</span>
                    )}
                  </div>
                </div>
              </div>

              {selectedJob.skills && selectedJob.skills.length > 0 && (
                <div className="space-y-2">
                  <h3 className="text-xs font-bold text-text-primary uppercase tracking-wider">Required Skills</h3>
                  <div className="flex flex-wrap gap-2">
                    {selectedJob.skills.map((skill: string, i: number) => (
                      <span key={i} className="px-2.5 py-1 bg-accent/10 text-accent border border-accent/20 rounded-lg text-xs font-medium">
                        {skill}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              <div className="space-y-2">
                <h3 className="flex items-center gap-2 text-xs font-bold text-text-primary uppercase tracking-wider">
                  <FileText className="w-4 h-4" />
                  Job Description
                </h3>
                <div className="bg-bg-primary p-4 rounded-xl border border-bg-border text-sm text-text-secondary whitespace-pre-wrap leading-relaxed max-h-80 overflow-y-auto font-sans">
                  {selectedJob.description || 'No description was scraped for this posting.'}
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
