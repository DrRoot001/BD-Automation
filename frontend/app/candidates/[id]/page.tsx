'use client'

import { useState } from 'react'
import { useParams } from 'next/navigation'
import { useQuery } from '@tanstack/react-query'
import Link from 'next/link'
import CandidateProfileForm from '@/components/CandidateProfileForm'
import { ApplicationsQueue } from '@/components/ApplicationsQueue'
import { api } from '@/lib/api'
import { Mail, Check, Loader2, AlertTriangle, X, Activity } from 'lucide-react'
import { useWebSocket } from '@/hooks/useWebSocket'

export default function CandidateDetailPage() {
  const { id } = useParams()
  const [activeTab, setActiveTab] = useState<'history' | 'profile'>('history')
  const [isApplying, setIsApplying] = useState(false)
  const [applyResult, setApplyResult] = useState<any>(null)
  const [showAutoApply, setShowAutoApply] = useState(false)
  const [maxApps, setMaxApps] = useState(10)
  const [progressLogs, setProgressLogs] = useState<{timestamp: string, message: string}[]>([])

  useWebSocket((evt) => {
    if (evt.event === 'pipeline.progress' && evt.data) {
      // Check if it belongs to this candidate or an application for this candidate
      // For simplicity, we capture all pipeline progress while on this page
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

  const [showSimulateModal, setShowSimulateModal] = useState(false)
  const [connectingGmail, setConnectingGmail] = useState(false)
  const [gmailError, setGmailError] = useState<string | null>(null)

  const handleConnectGmail = async () => {
    setGmailError(null)
    try {
      const data = await api.getGoogleAuthUrl(id as string)
      if (data.is_mock) {
        setShowSimulateModal(true)
      } else {
        window.location.href = data.auth_url
      }
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

  const triggerApply = async () => {
    setIsApplying(true)
    setApplyResult(null)
    setProgressLogs([])
    try {
      const data = await api.triggerApply(id as string, maxApps)
      setApplyResult(data)
    } catch (err) {
      setApplyResult({ error: 'Failed to trigger application process' })
    } finally {
      setIsApplying(false)
    }
  }

  if (isLoading) {
    return (
      <div className="min-h-screen bg-bg-primary p-8">
        <div className="max-w-screen-xl mx-auto space-y-6 animate-pulse">
          <div className="flex justify-between items-start mb-6">
            <div className="space-y-3">
              <div className="h-8 bg-bg-secondary rounded w-64"></div>
              <div className="h-4 bg-bg-secondary rounded w-32"></div>
            </div>
            <div className="h-10 bg-bg-secondary rounded w-32"></div>
          </div>
          <div className="h-12 bg-bg-secondary rounded w-1/3 mb-6"></div>
          <div className="h-64 bg-bg-secondary rounded w-full"></div>
        </div>
      </div>
    )
  }

  if (isError || !candidate) {
    return (
      <div className="min-h-screen bg-bg-primary p-8">
        <div className="max-w-screen-xl mx-auto text-center py-20">
          <p className="text-danger text-lg mb-4">Candidate not found.</p>
          <Link href="/candidates" className="text-accent hover:underline">
            ← Back to List
          </Link>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-bg-primary p-8 animate-fade-in">
      <div className="max-w-screen-xl mx-auto">
        <div className="mb-6 flex justify-between items-start">
          <div>
            <div className="flex items-center gap-4">
              <h1 className="text-xl font-semibold text-text-primary">
                {candidate.name}
              </h1>
              <span className="text-xs text-text-muted px-2 py-1 rounded-full bg-bg-secondary border border-bg-border">
                {candidate.email}
              </span>
              {candidate.google_connected ? (
                <span className="text-xs text-success bg-success/8 border border-success/20 px-2.5 py-1 rounded-full flex items-center gap-1.5 font-medium">
                  <span className="w-1.5 h-1.5 rounded-full bg-success animate-pulse"></span>
                  Gmail Connected
                </span>
              ) : (
                <button
                  onClick={handleConnectGmail}
                  className="text-xs text-text-muted hover:text-text-primary bg-bg-secondary hover:bg-bg-hover border border-bg-border px-2.5 py-1 rounded-full flex items-center gap-1.5 transition-colors font-medium cursor-pointer"
                >
                  <span className="w-1.5 h-1.5 rounded-full bg-amber-500"></span>
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
            className="bg-accent text-white px-4 py-2 rounded-lg text-sm font-medium hover:bg-accent/90 transition-colors shadow-accent"
          >
            Start Auto-Apply
          </button>
        </div>

        {showAutoApply && (
          <div className="bg-bg-secondary border border-bg-border rounded-xl p-5 mb-6 shadow-card animate-fade-in flex flex-col gap-4">
            <div className="flex items-end gap-4">
              <div className="flex-1">
                <h2 className="text-sm font-semibold text-text-primary mb-1">Trigger Job Search & Apply</h2>
                <p className="text-xs text-text-muted">
                  Run the background pipeline to discover jobs and auto-submit applications for this candidate.
                </p>
              </div>
              <div className="w-32">
                <label className="block text-xs font-medium text-text-secondary mb-1">Max Applications</label>
                <input
                  type="number"
                  value={maxApps}
                  onChange={(e) => setMaxApps(Number(e.target.value))}
                  className="w-full bg-bg-primary border border-bg-border rounded-lg px-3 py-2 text-sm text-text-primary focus:outline-none focus:border-accent"
                  min={1} max={50}
                />
              </div>
              <button
                onClick={triggerApply}
                disabled={isApplying}
                className="bg-accent text-white px-6 py-2 rounded-lg text-sm font-medium hover:bg-accent/90 disabled:opacity-50 h-[38px] flex items-center justify-center min-w-[120px]"
              >
                {isApplying ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Run Now'}
              </button>
            </div>

            {progressLogs.length > 0 && (
              <div className="mt-4 pt-4 border-t border-bg-border">
                <div className="flex items-center gap-2 mb-3">
                  <Activity className="w-4 h-4 text-accent" />
                  <h3 className="text-xs font-semibold text-text-secondary uppercase tracking-wider">Pipeline Progress</h3>
                </div>
                <div className="space-y-2 max-h-60 overflow-y-auto pr-2 custom-scrollbar">
                  {progressLogs.map((log, i) => (
                    <div key={i} className="flex items-start gap-3 animate-fade-in">
                      <div className="mt-1 flex-shrink-0">
                        {i === progressLogs.length - 1 && isApplying ? (
                          <Loader2 className="w-3.5 h-3.5 text-accent animate-spin" />
                        ) : (
                          <Check className="w-3.5 h-3.5 text-success" />
                        )}
                      </div>
                      <div>
                        <p className="text-sm text-text-primary">{log.message}</p>
                        <p className="text-[10px] text-text-muted mt-0.5">
                          {new Date(log.timestamp).toLocaleTimeString()}
                        </p>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {applyResult && !isApplying && progressLogs.length === 0 && (
          <div className="mb-6 p-4 rounded-lg bg-bg-secondary border border-bg-border text-xs text-text-secondary">
            <pre>{JSON.stringify(applyResult, null, 2)}</pre>
          </div>
        )}

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
          <CandidateProfileForm candidate={candidate} onSuccess={refetch} />
        )}

        {/* Gmail Connection Error Alert */}
        {gmailError && (
          <div className="mt-4 p-3 rounded-lg bg-danger/8 border border-danger/20 text-danger text-xs flex items-center gap-2 max-w-xl animate-fade-in">
            <AlertTriangle className="w-4 h-4" />
            <span>{gmailError}</span>
          </div>
        )}

        {/* Google OAuth Simulation Modal */}
        {showSimulateModal && (
          <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm animate-fade-in">
            <div className="bg-bg-secondary border border-bg-border w-full max-w-md rounded-xl p-6 shadow-card animate-scale-up relative">
              <button 
                onClick={() => setShowSimulateModal(false)}
                className="absolute top-4 right-4 text-text-muted hover:text-text-primary transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
              
              <div className="flex flex-col items-center text-center gap-4">
                <div className="w-12 h-12 rounded-full bg-accent/10 flex items-center justify-center text-accent">
                  <Mail className="w-6 h-6" />
                </div>
                <div>
                  <h3 className="text-base font-semibold text-text-primary">Connect Gmail (Simulation)</h3>
                  <p className="text-xs text-text-muted mt-2 leading-relaxed">
                    Real Google OAuth credentials are not set in the <code>.env</code> file. To test email classification and tracking, you can simulate a successful authorization flow.
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
                    {connectingGmail ? (
                      <>
                        <Loader2 className="w-4 h-4 animate-spin" />
                        Connecting...
                      </>
                    ) : (
                      'Simulate Connection'
                    )}
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
