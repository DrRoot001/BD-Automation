'use client'

import { useState } from 'react'
import { Search, Filter, AlertCircle, RefreshCw, Layers } from 'lucide-react'
import { useApplications } from '@/hooks/useApplications'
import { useCurrentUser } from '@/hooks/useCurrentUser'
import { ApplicationSummary } from '@/lib/api'
import { StatusBadge } from '@/components/shared/StatusBadge'
import { Skeleton } from '@/components/shared/Skeleton'
import { formatDistanceToNow } from '@/lib/utils'
import { ApplicationDrawer } from '@/components/dashboard/ApplicationDrawer'

export default function ApplicationsPage() {
  const { data: user } = useCurrentUser()
  const candidateId = user?.id || null

  const { data: applications, isLoading, isError, refetch } = useApplications({
    candidateId: candidateId || undefined,
    limit: 50
  })

  const [selectedApp, setSelectedApp] = useState<ApplicationSummary | null>(null)
  const [isDrawerOpen, setIsDrawerOpen] = useState(false)

  const openDrawer = (app: ApplicationSummary) => {
    setSelectedApp(app)
    setIsDrawerOpen(true)
  }

  const handleRetry = (appId: string) => {
    console.log("Retry application:", appId)
    // Here we would call an API to retry the application
  }

  return (
    <div className="space-y-6 animate-in fade-in duration-500 max-w-7xl mx-auto">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-text-primary tracking-tight">Applications Tracker</h1>
          <p className="text-sm text-text-muted mt-1">Monitor the status of your automated job applications.</p>
        </div>
        
        <div className="flex items-center gap-3">
          <div className="relative">
            <Search className="w-4 h-4 text-text-muted absolute left-3 top-1/2 -translate-y-1/2" />
            <input 
              type="text" 
              placeholder="Search companies..." 
              className="pl-9 pr-4 py-2 bg-bg-primary border border-bg-border rounded-lg text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent transition-colors w-64"
            />
          </div>
          <button className="p-2 border border-bg-border text-text-muted hover:text-text-primary hover:bg-bg-hover rounded-lg transition-colors bg-bg-primary">
            <Filter className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Main Content Area */}
      {isError ? (
        <div className="flex flex-col items-center justify-center min-h-[400px] bg-bg-secondary rounded-xl border border-bg-border">
          <AlertCircle className="w-12 h-12 text-danger mb-4" />
          <h2 className="text-lg font-semibold text-text-primary">Failed to load applications</h2>
          <p className="text-text-muted text-sm mt-1 mb-6">There was an error communicating with the API.</p>
          <button 
            onClick={() => refetch()}
            className="flex items-center gap-2 px-4 py-2 bg-bg-primary border border-bg-border hover:bg-bg-hover text-text-primary rounded-md transition-colors"
          >
            <RefreshCw className="w-4 h-4" />
            Retry
          </button>
        </div>
      ) : (
        <div className="bg-bg-secondary border border-bg-border rounded-xl overflow-hidden shadow-sm">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="bg-bg-primary/50 text-text-muted uppercase text-xs tracking-wider border-b border-bg-border">
                <tr>
                  <th className="px-6 py-4 font-medium">Company & Role</th>
                  <th className="px-6 py-4 font-medium">Platform</th>
                  <th className="px-6 py-4 font-medium">Status</th>
                  <th className="px-6 py-4 font-medium">Combined Score</th>
                  <th className="px-6 py-4 font-medium text-right">Applied</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-bg-border">
                {isLoading ? (
                  Array.from({ length: 5 }).map((_, i) => (
                    <tr key={i}>
                      <td className="px-6 py-4"><Skeleton className="h-5 w-48" /><Skeleton className="h-4 w-32 mt-2" /></td>
                      <td className="px-6 py-4"><Skeleton className="h-5 w-20" /></td>
                      <td className="px-6 py-4"><Skeleton className="h-6 w-24 rounded-full" /></td>
                      <td className="px-6 py-4"><Skeleton className="h-5 w-12" /></td>
                      <td className="px-6 py-4 text-right flex justify-end"><Skeleton className="h-5 w-16" /></td>
                    </tr>
                  ))
                ) : !applications || applications.length === 0 ? (
                  <tr>
                    <td colSpan={5} className="px-6 py-16 text-center">
                      <div className="flex flex-col items-center justify-center">
                        <div className="w-16 h-16 bg-bg-primary rounded-full flex items-center justify-center mb-4 border border-bg-border">
                          <Layers className="w-8 h-8 text-text-muted" />
                        </div>
                        <h3 className="text-lg font-medium text-text-primary">No applications found</h3>
                        <p className="text-text-muted text-sm mt-1 max-w-sm">
                          Once the BD Automator starts applying to jobs on your behalf, they will appear here.
                        </p>
                      </div>
                    </td>
                  </tr>
                ) : (
                  applications.map((app) => (
                    <tr 
                      key={app.application_id}
                      onClick={() => openDrawer(app)}
                      className="group hover:bg-bg-hover cursor-pointer transition-colors"
                    >
                      <td className="px-6 py-4">
                        <div className="font-semibold text-text-primary group-hover:text-accent transition-colors">{app.job_title}</div>
                        <div className="text-text-muted mt-0.5">{app.company}</div>
                      </td>
                      <td className="px-6 py-4">
                        <span className="inline-flex items-center px-2 py-1 bg-bg-primary border border-bg-border text-text-secondary text-xs rounded-md">
                          {app.platform}
                        </span>
                      </td>
                      <td className="px-6 py-4">
                        <StatusBadge status={app.status} />
                      </td>
                      <td className="px-6 py-4">
                        {app.fit_score != null && app.ats_score != null ? (
                          <div className="flex items-center gap-2">
                            <span className="font-medium text-text-primary">
                              {Math.round(((app.fit_score + app.ats_score) / 2) * 100)}%
                            </span>
                            <div className="w-16 h-1.5 bg-bg-primary rounded-full overflow-hidden">
                              <div 
                                className="h-full bg-accent rounded-full" 
                                style={{ width: `${((app.fit_score + app.ats_score) / 2) * 100}%` }}
                              />
                            </div>
                          </div>
                        ) : (
                          <span className="text-text-muted italic">Pending</span>
                        )}
                      </td>
                      <td className="px-6 py-4 text-right text-text-muted whitespace-nowrap">
                        {app.submitted_at ? formatDistanceToNow(app.submitted_at) + ' ago' : formatDistanceToNow(app.created_at) + ' ago'}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Drawer */}
      <ApplicationDrawer 
        isOpen={isDrawerOpen} 
        application={selectedApp} 
        onClose={() => setIsDrawerOpen(false)}
        onRetry={handleRetry}
      />
    </div>
  )
}
