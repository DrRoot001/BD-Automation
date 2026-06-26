'use client'

import { useState } from 'react'
import { useParams } from 'next/navigation'
import { useQuery } from '@tanstack/react-query'
import Link from 'next/link'
import CandidateProfileForm from '@/components/dashboard/CandidateProfileForm'
import { ApplicationsQueue } from '@/components/dashboard/ApplicationsQueue'
import { ToastContainer, useToast } from '@/components/Toast'
import { api } from '@/lib/api'
import { Mail, Check, Loader2, AlertTriangle, X, Activity, FileText, ExternalLink } from 'lucide-react'
import { useWebSocket } from '@/hooks/useWebSocket'
import { resolveFileUrl } from '@/components/utils'

/** Opens the resume via the backend view endpoint, which handles signed URLs for private Supabase buckets */
function ViewResumeButton({ resumeId, fileUrl }: { resumeId: string; fileUrl?: string }) {
  const [loading, setLoading] = useState(false)

  const handleView = () => {
    // Open the backend view endpoint directly in a new tab.
    // The browser handles the 302 redirect transparently, 
    // bypassing CORS and async popup-blocker restrictions.
    window.open(`/api/resumes/${resumeId}/view`, '_blank', 'noopener,noreferrer')
  }

  return (
    <button
      onClick={handleView}
      disabled={loading}
      className="btn-secondary text-xs py-1.5 px-3 disabled:opacity-50"
    >
      {loading
        ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
        : <><ExternalLink className="w-3.5 h-3.5" /> View</>
      }
    </button>
  )
}

export default function CandidateDetailPage() {
  const { id } = useParams()
  const [activeTab, setActiveTab] = useState<'history' | 'profile'>('history')
  const [isApplying, setIsApplying] = useState(false)
  const [showAutoApply, setShowAutoApply] = useState(false)
  const [maxApps, setMaxApps] = useState(10)
  const [maxAppsError, setMaxAppsError] = useState<string | null>(null)
  const [progressLogs, setProgressLogs] = useState<{timestamp: string, message: string}[]>([])

  const { toasts, dismiss, success: toastSuccess, error: toastError } = useToast()

  useWebSocket((evt) => {
    if (evt.event === 'pipeline.progress' && evt.data) {
      setProgressLogs((prev) => [...prev, {
        timestamp: evt.timestamp || new Date().toISOString(),
        message: evt.data?.message as string
      }])
    }
  })

  const { data: candidate, isLoading, isError, refetch } = useQuery({
    queryKey: ['candidate', id],
    queryFn: () => api.getCandidate(id as string),
    retry: false,
  })

  // Fetch base resume for profile view
  const { data: resumes } = useQuery({
    queryKey: ['resumes', id],
    queryFn: () => fetch(`/api/resumes/${id}?is_base=true`).then((r) => r.json()),
    enabled: !!id,
  })
  const baseResume = Array.isArray(resumes) ? resumes[0] : null

  const [showSimulateModal, setShowSimulateModal] = useState(false)
  const [connectingGmail, setConnectingGmail] = useState(false)
  const [gmailError, setGmailError] = useState<string | null>(null)

  const handleConnectGmail = async () => {
    setGmailError(null)
    try {
      const data = await api.getGoogleAuthUrl(id as string)
      if (data.is_mock) setShowSimulateModal(true)
      else window.location.href = data.auth_url
    } catch (err: any) {
      setGmailError(err.message || 'Failed to fetch Google auth URL')
    }
  }

  const handleSimulateConnection = async () => {
    setConnectingGmail(true)
    setGmailError(null)
    try {
      await api.exchangeGoogleCode(id as string, 'mock_auth_code')
      await refetch()
      setShowSimulateModal(false)
    } catch (err: any) {
      setGmailError(err.message || 'Failed to connect simulated Gmail')
    } finally {
      setConnectingGmail(false)
    }
  }

  const validateMaxApps = (val: number): string | null => {
    if (!Number.isInteger(val) || val < 1) return 'Must be at least 1'
    if (val > 100) return 'Maximum is 100'
    return null
  }

  const triggerApply = async () => {
    const err = validateMaxApps(maxApps)
    if (err) { setMaxAppsError(err); return }
    setMaxAppsError(null)
    setIsApplying(true)
    setProgressLogs([])
    try {
      await api.triggerApply(id as string, maxApps)
      toastSuccess('Auto-Apply started', `Running up to ${maxApps} applications in the background.`)
    } catch (e: any) {
      toastError('Auto-Apply failed', e.message || 'An unexpected error occurred.')
    } finally {
      setIsApplying(false)
    }
  }

  if (isLoading) {
    return (
      <div className="min-h-screen bg-bg-primary p-6 md:p-8">
        <div className="max-w-screen-xl mx-auto space-y-6 animate-pulse">
          <div className="flex justify-between items-start mb-6">
            <div className="space-y-3">
              <div className="h-8 bg-bg-secondary rounded w-64" />
              <div className="h-4 bg-bg-secondary rounded w-32" />
            </div>
            <div className="h-10 bg-bg-secondary rounded w-32" />
          </div>
          <div className="h-12 bg-bg-secondary rounded w-1/3 mb-6" />
          <div className="h-64 bg-bg-secondary rounded w-full" />
        </div>
      </div>
    )
  }

  if (isError || !candidate) {
    return (
      <div className="min-h-screen bg-bg-primary p-6 md:p-8">
        <div className="max-w-screen-xl mx-auto text-center py-20">
          <p className="text-danger text-lg mb-4">Candidate not found.</p>
          <Link href="/candidates" className="text-accent hover:underline">← Back to List</Link>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-bg-primary p-6 md:p-8 animate-fade-in">
      <div className="max-w-screen-xl mx-auto">

        {/* Header */}
        <div className="mb-6 flex flex-col sm:flex-row sm:justify-between sm:items-start gap-4">
          <div>
            <div className="flex flex-wrap items-center gap-3">
              <h1 className="text-xl font-semibold text-text-primary">{candidate.name}</h1>
              <span className="text-xs text-text-muted px-2 py-1 rounded-full bg-bg-secondary border border-bg-border">
                {candidate.email}
              </span>
              {candidate.google_connected ? (
                <span className="text-xs text-success bg-success/8 border border-success/20 px-2.5 py-1 rounded-full flex items-center gap-1.5 font-medium">
                  <span className="w-1.5 h-1.5 rounded-full bg-success animate-pulse" />
                  Gmail Connected
                </span>
              ) : (
                <button
                  onClick={handleConnectGmail}
                  className="text-xs text-text-muted hover:text-text-primary bg-bg-secondary hover:bg-bg-hover border border-bg-border px-2.5 py-1 rounded-full flex items-center gap-1.5 transition-colors font-medium cursor-pointer"
                >
                  <span className="w-1.5 h-1.5 rounded-full bg-amber-500" />
                  Connect Gmail
                </button>
              )}
            </div>
            <Link href="/candidates" className="text-xs text-text-muted hover:text-text-primary transition-colors inline-block mt-2">
              ← Back to List
            </Link>
          </div>
          <button
            onClick={() => setShowAutoApply(!showAutoApply)}
            className="bg-accent text-white px-4 py-2 rounded-lg text-sm font-medium hover:bg-accent/90 transition-colors shadow-accent whitespace-nowrap self-start"
          >
            Start Auto-Apply
          </button>
        </div>

        {/* Auto Apply Panel */}
        {showAutoApply && (
          <div className="bg-bg-secondary border border-bg-border rounded-xl p-5 mb-6 shadow-card animate-fade-in flex flex-col gap-4">
            <div className="flex flex-col sm:flex-row sm:items-end gap-4">
              <div className="flex-1">
                <h2 className="text-sm font-semibold text-text-primary mb-1">Trigger Job Search & Apply</h2>
                <p className="text-xs text-text-muted">
                  Run the background pipeline to discover jobs and auto-submit applications for this candidate.
                </p>
              </div>
              <div className="flex items-end gap-3">
                <div className="w-32">
                  <label className="block text-xs font-medium text-text-secondary mb-1">Max Applications</label>
                  <input
                    type="number"
                    value={maxApps}
                    onChange={(e) => {
                      const v = parseInt(e.target.value)
                      setMaxApps(isNaN(v) ? 1 : v)
                      setMaxAppsError(validateMaxApps(isNaN(v) ? 1 : v))
                    }}
                    className={`w-full bg-bg-primary border rounded-lg px-3 py-2 text-sm text-text-primary focus:outline-none focus:border-accent ${maxAppsError ? 'border-danger' : 'border-bg-border'}`}
                    min={1} max={100}
                  />
                  {maxAppsError && <p className="text-danger text-[11px] mt-0.5">{maxAppsError}</p>}
                </div>
                <button
                  onClick={triggerApply}
                  disabled={isApplying || !!maxAppsError}
                  className="bg-accent text-white px-6 py-2 rounded-lg text-sm font-medium hover:bg-accent/90 disabled:opacity-50 h-[38px] flex items-center justify-center min-w-[100px]"
                >
                  {isApplying ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Run Now'}
                </button>
              </div>
            </div>

            {progressLogs.length > 0 && (
              <div className="mt-2 pt-4 border-t border-bg-border">
                <div className="flex items-center gap-2 mb-3">
                  <Activity className="w-4 h-4 text-accent" />
                  <h3 className="text-xs font-semibold text-text-secondary uppercase tracking-wider">Pipeline Progress</h3>
                </div>
                <div className="space-y-2 max-h-60 overflow-y-auto pr-2 custom-scrollbar">
                  {progressLogs.map((log, i) => (
                    <div key={i} className="flex items-start gap-3 animate-fade-in">
                      <div className="mt-1 flex-shrink-0">
                        {i === progressLogs.length - 1 && isApplying
                          ? <Loader2 className="w-3.5 h-3.5 text-accent animate-spin" />
                          : <Check className="w-3.5 h-3.5 text-success" />}
                      </div>
                      <div>
                        <p className="text-sm text-text-primary">{log.message}</p>
                        <p className="text-[10px] text-text-muted mt-0.5">{new Date(log.timestamp).toLocaleTimeString()}</p>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {/* Tabs */}
        <div className="border-b border-bg-border mb-6 flex gap-4">
          <button
            className={`pb-2 text-sm font-medium transition-colors ${activeTab === 'history' ? 'border-b-2 border-accent text-accent' : 'text-text-muted hover:text-text-primary'}`}
            onClick={() => setActiveTab('history')}
          >
            Apply History
          </button>
          <button
            className={`pb-2 text-sm font-medium transition-colors ${activeTab === 'profile' ? 'border-b-2 border-accent text-accent' : 'text-text-muted hover:text-text-primary'}`}
            onClick={() => setActiveTab('profile')}
          >
            Profile Details
          </button>
        </div>

        {activeTab === 'history' && (
          <ApplicationsQueue
            candidateId={id as string}
            emptyMessage="No applications yet for this candidate. Click 'Start Auto-Apply' above to begin."
          />
        )}

        {activeTab === 'profile' && (
          <div className="space-y-6">
            {/* Base Resume card */}
            {baseResume && (
              <div className="card p-5 flex items-center justify-between gap-4">
                <div className="flex items-center gap-3">
                  <div className="w-9 h-9 rounded-lg bg-accent/10 flex items-center justify-center">
                    <FileText className="w-5 h-5 text-accent" />
                  </div>
                  <div>
                    <p className="text-sm font-medium text-text-primary">Base Resume</p>
                    <p className="text-xs text-text-muted mt-0.5">
                      Version {baseResume.version} · uploaded {baseResume.created_at ? new Date(baseResume.created_at).toLocaleDateString() : ''}
                    </p>
                  </div>
                </div>
                {baseResume?.id && (
                  <ViewResumeButton resumeId={baseResume.id} fileUrl={baseResume.file_url} />
                )}
              </div>
            )}
            <CandidateProfileForm candidate={candidate} onSuccess={refetch} />
          </div>
        )}

        {/* Gmail error */}
        {gmailError && (
          <div className="mt-4 p-3 rounded-lg bg-danger/8 border border-danger/20 text-danger text-xs flex items-center gap-2 max-w-xl animate-fade-in">
            <AlertTriangle className="w-4 h-4" />
            <span>{gmailError}</span>
          </div>
        )}

        {/* Google OAuth Modal */}
        {showSimulateModal && (
          <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm animate-fade-in">
            <div className="bg-bg-secondary border border-bg-border w-full max-w-md rounded-xl p-6 shadow-card animate-scale-up relative">
              <button onClick={() => setShowSimulateModal(false)} className="absolute top-4 right-4 text-text-muted hover:text-text-primary">
                <X className="w-5 h-5" />
              </button>
              <div className="flex flex-col items-center text-center gap-4">
                <div className="w-12 h-12 rounded-full bg-accent/10 flex items-center justify-center text-accent">
                  <Mail className="w-6 h-6" />
                </div>
                <div>
                  <h3 className="text-base font-semibold text-text-primary">Connect Gmail (Simulation)</h3>
                  <p className="text-xs text-text-muted mt-2 leading-relaxed">
                    Real Google OAuth credentials are not set. Simulate a successful authorization flow for testing.
                  </p>
                </div>
                <div className="w-full flex gap-3 mt-2">
                  <button
                    onClick={() => setShowSimulateModal(false)}
                    className="flex-1 bg-bg-primary border border-bg-border hover:bg-bg-hover text-text-secondary text-sm font-medium py-2 rounded-lg transition-colors"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={handleSimulateConnection}
                    disabled={connectingGmail}
                    className="flex-1 bg-accent text-white text-sm font-medium py-2 rounded-lg hover:bg-accent/90 disabled:opacity-50 transition-colors flex items-center justify-center gap-2"
                  >
                    {connectingGmail ? <><Loader2 className="w-4 h-4 animate-spin" />Connecting...</> : 'Simulate Connection'}
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Toast notifications */}
      <ToastContainer toasts={toasts} onDismiss={dismiss} />
    </div>
  )
}
