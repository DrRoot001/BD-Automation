'use client'

import { useState, useRef } from 'react'
import { useParams } from 'next/navigation'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import Link from 'next/link'
import CandidateProfileForm from '@/components/dashboard/CandidateProfileForm'
import { ApplicationsQueue } from '@/components/dashboard/ApplicationsQueue'
import { Modal } from '@/components/ui/Modal'
import { useToast } from '@/components/ui/Toast'
import { useConfirm } from '@/components/ui/ConfirmDialog'
import { useCurrentUser } from '@/hooks/useCurrentUser'
import { api } from '@/lib/api'
import { Mail, Check, Loader2, AlertTriangle, Activity, FileText, ExternalLink, ArrowLeft, UserCheck, OctagonPause, Play } from 'lucide-react'
import { useWebSocket } from '@/hooks/useWebSocket'

// Steps published by backend/app/tasks/dynamic_apply.py that end the run —
// no further pipeline.progress events follow one of these.
const TERMINAL_STEPS = new Set(['no_jobs', 'no_matches', 'limit_reached', 'done', 'error'])

function ViewResumeButton({ resumeId }: { resumeId: string; fileUrl?: string }) {
  const [loading, setLoading] = useState(false)

  const handleView = () => {
    setLoading(true)
    window.open(`/api/resumes/${resumeId}/view`, '_blank', 'noopener,noreferrer')
    setTimeout(() => setLoading(false), 1000)
  }

  return (
    <button
      onClick={handleView}
      disabled={loading}
      className="btn-secondary !py-1.5 !px-3 !text-xs"
    >
      {loading ? (
        <Loader2 className="w-3.5 h-3.5 animate-spin" />
      ) : (
        <>
          <ExternalLink className="w-3.5 h-3.5" /> View Resume
        </>
      )}
    </button>
  )
}

export default function CandidateDetailPage() {
  const { id } = useParams()
  const queryClient = useQueryClient()
  const toast = useToast()
  const confirm = useConfirm()

  const { data: currentUser } = useCurrentUser()
  const isAdmin = currentUser?.role === 'admin'

  const [activeTab, setActiveTab] = useState<'history' | 'profile'>('history')
  const [isApplying, setIsApplying] = useState(false)
  const [showAutoApply, setShowAutoApply] = useState(false)
  const [maxApps, setMaxApps] = useState(10)
  const [maxAppsError, setMaxAppsError] = useState<string | null>(null)
  const [progressLogs, setProgressLogs] = useState<{ timestamp: string; message: string }[]>([])
  const processedLogsRef = useRef<Set<string>>(new Set())

  const [past24Only, setPast24Only] = useState(false)
  const [selectedPlatform, setSelectedPlatform] = useState('')

  // Fetch platforms dynamically with standard fallbacks
  const { data: platforms = ['greenhouse', 'lever', 'dice', 'indeed', 'remoteok', 'jobicy', 'remoterocketship'] } = useQuery({
    queryKey: ['platforms'],
    queryFn: () => api.getPlatforms().catch(() => ['greenhouse', 'lever', 'dice', 'indeed', 'remoteok', 'jobicy', 'remoterocketship']),
  })

  // Fetch count of available jobs matching the filters
  const { data: availableJobsData, isLoading: countLoading } = useQuery({
    queryKey: ['available-jobs-count', id, past24Only, selectedPlatform],
    queryFn: () => api.getJobsCount({
      candidateId: id as string,
      timeFilter: past24Only ? '24h' : undefined,
      source: selectedPlatform || undefined
    }),
    staleTime: 10 * 1000,
  })
  const availableJobsCount = availableJobsData?.total_count ?? 0

  useWebSocket((evt) => {
    if (evt.event === 'pipeline.progress' && evt.data) {
      const candidateId = evt.data?.candidate_id || evt.data?.candidateId
      if (candidateId && candidateId !== id) return

      const message = evt.data?.message as string
      const timestamp = evt.timestamp || new Date().toISOString()
      const logKey = `${timestamp}-${message}`

      if (processedLogsRef.current.has(logKey)) return
      processedLogsRef.current.add(logKey)

      setProgressLogs((prev) => [
        ...prev,
        {
          timestamp,
          message,
        },
      ])

      // Any terminal step means the pipeline run has finished (successfully,
      // emptily, or with an error) — stop showing the "Applying..." spinner
      // even if it somehow outlasted the trigger request's own response.
      const step = evt.data?.step as string
      if (TERMINAL_STEPS.has(step)) setIsApplying(false)
    }
  })

  const { data: candidate, isLoading, isError, refetch } = useQuery({
    queryKey: ['candidate', id],
    queryFn: () => api.getCandidate(id as string),
    retry: false,
  })

  // Fetch BD users list for admin assignment
  const { data: bdUsers } = useQuery({
    queryKey: ['admin-bd-users'],
    queryFn: () => api.getAdminUsers({ limit: 100 }),
    enabled: isAdmin,
  })

  // Mutation to re-assign candidate to a BD User
  const assignMutation = useMutation({
    mutationFn: (userId: string) => api.updateCandidate(id as string, { user_id: userId }),
    onSuccess: (_, userId) => {
      queryClient.invalidateQueries({ queryKey: ['candidate', id] })
      queryClient.invalidateQueries({ queryKey: ['candidates'] })
      const assignedUser = bdUsers?.find(u => u.id === userId)
      toast.success(`Assigned candidate to ${assignedUser?.full_name || assignedUser?.email || 'BD User'}`)
    },
    onError: (err: unknown) => {
      const errorObj = err as { message?: string }
      toast.error(errorObj.message || 'Failed to assign candidate')
    },
  })

  // BG-08: stop/resume the whole apply pipeline for this candidate. Stop pauses
  // every in-flight application (they stop counting toward the active cap) and
  // blocks new matching runs; resume releases them and re-dispatches queued ones.
  const pipelineMutation = useMutation({
    mutationFn: (action: 'stop' | 'resume'): Promise<{
      status: string
      paused_applications?: number
      in_browser_finishing?: number
      resumed_applications?: number
      redispatched_queued?: number
    }> =>
      action === 'stop'
        ? api.stopPipeline(id as string)
        : api.resumePipeline(id as string),
    onSuccess: (res, action) => {
      queryClient.invalidateQueries({ queryKey: ['candidate', id] })
      queryClient.invalidateQueries({ queryKey: ['applications'] })
      queryClient.invalidateQueries({ queryKey: ['kpis'] })
      if (action === 'stop') {
        toast.success(
          `Pipeline stopped — ${res.paused_applications ?? 0} application(s) paused` +
          ((res.in_browser_finishing ?? 0) > 0
            ? `. ${res.in_browser_finishing} already in a browser run will finish.`
            : '.'),
        )
      } else {
        toast.success(
          `Pipeline resumed — ${res.resumed_applications ?? 0} application(s) released` +
          ((res.redispatched_queued ?? 0) > 0 ? `, ${res.redispatched_queued} re-queued.` : '.'),
        )
      }
    },
    onError: (err: unknown) => {
      const errorObj = err as { message?: string }
      toast.error(errorObj.message || 'Pipeline action failed')
    },
  })

  const handlePipelineToggle = async () => {
    if (!candidate) return
    if (candidate.automation_paused) {
      pipelineMutation.mutate('resume')
      return
    }
    const isConfirmed = await confirm({
      title: 'Stop Pipeline?',
      message: `Stop the auto-apply pipeline for ${candidate.name}? All in-flight applications will be paused and no new applications will start until you resume. Applications already inside a live browser run will finish their current attempt.`,
      confirmLabel: 'Stop Pipeline',
      variant: 'danger',
    })
    if (isConfirmed) pipelineMutation.mutate('stop')
  }

  const handleAssignChange = async (newUserId: string) => {
    if (!newUserId || candidate?.user_id === newUserId) return

    const targetUser = bdUsers?.find(u => u.id === newUserId)
    const userName = targetUser?.full_name || targetUser?.email || 'selected user'

    const isConfirmed = await confirm({
      title: 'Assign Candidate to BD User?',
      message: `Re-assign ${candidate?.name} to ${userName}?`,
      confirmLabel: 'Assign Candidate',
      variant: 'default',
    })

    if (isConfirmed) {
      assignMutation.mutate(newUserId)
    }
  }

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
    } catch (err: unknown) {
      const errorObj = err as { message?: string }
      setGmailError(errorObj.message || 'Failed to fetch Google auth URL')
    }
  }

  const handleSimulateConnection = async () => {
    setConnectingGmail(true)
    setGmailError(null)
    try {
      await api.exchangeGoogleCode(id as string, 'mock_auth_code')
      await refetch()
      setShowSimulateModal(false)
      toast.success('Gmail connection simulated successfully!')
    } catch (err: unknown) {
      const errorObj = err as { message?: string }
      setGmailError(errorObj.message || 'Failed to connect simulated Gmail')
    } finally {
      setConnectingGmail(false)
    }
  }

  const handleDisconnectGmail = async () => {
    const isConfirmed = await confirm({
      title: 'Disconnect Gmail?',
      message: 'This will remove the candidate\'s connected Gmail account. The candidate will need to re-authenticate to allow AI email scans.',
      confirmLabel: 'Disconnect',
      variant: 'danger',
    })
    
    if (isConfirmed) {
      setConnectingGmail(true)
      try {
        await api.disconnectGmail(id as string)
        await refetch()
        toast.success('Gmail disconnected successfully.')
      } catch (err: unknown) {
        const errorObj = err as { message?: string }
        toast.error(errorObj.message || 'Failed to disconnect Gmail')
      } finally {
        setConnectingGmail(false)
      }
    }
  }

  const validateMaxApps = (val: number): string | null => {
    if (!Number.isInteger(val) || val < 1) return 'Must be at least 1'
    if (val > 100) return 'Maximum is 100'
    return null
  }

  const triggerApply = async () => {
    const err = validateMaxApps(maxApps)
    if (err) {
      setMaxAppsError(err)
      return
    }
    setMaxAppsError(null)
    setIsApplying(true)
    setProgressLogs([])
    processedLogsRef.current.clear()
    try {
      await api.triggerApply(
        id as string, 
        maxApps,
        past24Only ? '24h' : undefined,
        selectedPlatform || undefined
      )
      toast.success(`Auto-Apply started for up to ${maxApps} applications.`)
    } catch (e: unknown) {
      const errorObj = e as { message?: string }
      toast.error(errorObj.message || 'Auto-Apply trigger failed.')
    } finally {
      setIsApplying(false)
    }
  }

  if (isLoading) {
    return (
      <div className="space-y-6 max-w-7xl mx-auto animate-pulse">
        <div className="flex justify-between items-start mb-6">
          <div className="space-y-3">
            <div className="h-8 bg-bg-border rounded w-64" />
            <div className="h-4 bg-bg-border rounded w-32" />
          </div>
          <div className="h-10 bg-bg-border rounded w-32" />
        </div>
        <div className="h-64 bg-bg-border rounded-xl w-full" />
      </div>
    )
  }

  if (isError || !candidate) {
    return (
      <div className="text-center py-20 max-w-7xl mx-auto">
        <p className="text-danger text-base font-semibold mb-3">Candidate not found.</p>
        <Link href="/candidates" className="btn-secondary text-xs inline-flex items-center gap-1.5">
          <ArrowLeft className="w-3.5 h-3.5" /> Back to Candidates List
        </Link>
      </div>
    )
  }

  const assignedUser = bdUsers?.find(u => u.id === candidate.user_id)

  return (
    <div className="space-y-6 max-w-7xl mx-auto animate-fade-in">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:justify-between sm:items-start gap-4 border-b border-bg-border pb-6">
        <div>
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="page-title">{candidate.name}</h1>
            <span className="text-xs text-text-muted px-2.5 py-1 rounded-full bg-bg-secondary border border-bg-border font-medium">
              {candidate.email}
            </span>
            {candidate.google_connected ? (
              <div className="flex items-center gap-2">
                <span className="text-xs text-success bg-success/10 border border-success/20 px-2.5 py-0.5 rounded-full flex items-center gap-1.5 font-semibold">
                  <span className="w-1.5 h-1.5 rounded-full bg-success animate-pulse" />
                  Gmail Connected
                </span>
                <button
                  onClick={handleDisconnectGmail}
                  disabled={connectingGmail}
                  className="text-[10px] uppercase tracking-wide text-danger/80 hover:text-danger transition-colors font-bold px-2 py-0.5 border border-danger/30 rounded-full hover:bg-danger/10"
                >
                  Disconnect
                </button>
              </div>
            ) : (
              <button
                onClick={handleConnectGmail}
                className="text-xs text-text-muted hover:text-text-primary bg-bg-secondary hover:bg-bg-hover border border-bg-border px-2.5 py-0.5 rounded-full flex items-center gap-1.5 transition-colors font-semibold"
              >
                <span className="w-1.5 h-1.5 rounded-full bg-amber-500" />
                Connect Gmail
              </button>
            )}

            {/* Admin Candidate Assignment Selector */}
            {isAdmin && bdUsers && bdUsers.length > 0 && (
              <div className="flex items-center gap-1.5 bg-bg-secondary border border-bg-border px-2.5 py-1 rounded-full text-xs">
                <UserCheck className="w-3.5 h-3.5 text-text-muted" />
                <span className="text-text-muted font-medium">Assigned to:</span>
                <select
                  value={candidate.user_id || ''}
                  onChange={(e) => handleAssignChange(e.target.value)}
                  className="bg-transparent font-semibold text-text-primary focus:outline-none cursor-pointer"
                  disabled={assignMutation.isPending}
                >
                  <option value="">-- Unassigned --</option>
                  {bdUsers.map((u) => (
                    <option key={u.id} value={u.id}>
                      {u.full_name ? `${u.full_name} (${u.role === 'admin' ? 'Admin' : 'BD User'})` : u.email}
                    </option>
                  ))}
                </select>
              </div>
            )}
          </div>

          <Link href="/candidates" className="text-xs text-text-muted hover:text-text-primary transition-colors inline-flex items-center gap-1 mt-2 font-medium">
            <ArrowLeft className="w-3 h-3" /> Back to Candidates
          </Link>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {/* BG-08: candidate-level pipeline stop/resume */}
          <button
            onClick={handlePipelineToggle}
            disabled={pipelineMutation.isPending}
            className={
              candidate.automation_paused
                ? 'btn-secondary !border-success/40 !text-success hover:!bg-success/10'
                : 'btn-secondary !border-danger/40 !text-danger hover:!bg-danger/10'
            }
            title={
              candidate.automation_paused
                ? 'Pipeline is stopped — release paused applications and allow new runs'
                : 'Pause all in-flight applications and block new runs for this candidate'
            }
          >
            {pipelineMutation.isPending ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : candidate.automation_paused ? (
              <>
                <Play className="w-4 h-4" /> Resume Pipeline
              </>
            ) : (
              <>
                <OctagonPause className="w-4 h-4" /> Stop Pipeline
              </>
            )}
          </button>
          <button
            onClick={() => setShowAutoApply(!showAutoApply)}
            className="btn-primary"
          >
            {showAutoApply ? 'Hide Auto-Apply Control' : 'Start Auto-Apply'}
          </button>
        </div>
      </div>

      {/* Auto Apply Panel */}
      {showAutoApply && (
        <div className="card p-5 bg-bg-card border border-bg-border rounded-xl shadow-sm animate-fade-in flex flex-col gap-5">
          <div className="flex flex-col gap-4">
            <div>
              <h2 className="text-sm font-semibold text-text-primary mb-1 flex items-center gap-2">
                <Activity className="w-4 h-4 text-accent" />
                Trigger Job Search & Apply
              </h2>
              <p className="text-xs text-text-muted">
                Run the background pipeline to discover jobs and auto-submit applications for this candidate.
              </p>
            </div>
            
            <div className="flex flex-col sm:flex-row items-end gap-3 flex-wrap border-t border-bg-border pt-4">
              {/* Select Platform */}
              <div className="w-48">
                <label className="input-label text-xs">Select Platform</label>
                <select
                  value={selectedPlatform}
                  onChange={(e) => setSelectedPlatform(e.target.value)}
                  className="input capitalize"
                >
                  <option value="">All Platforms</option>
                  {platforms.map((plat) => (
                    <option key={plat} value={plat}>
                      {plat}
                    </option>
                  ))}
                </select>
              </div>

              {/* Max Applications */}
              <div className="w-36">
                <label className="input-label text-xs">Max Applications</label>
                <input
                  type="number"
                  value={maxApps}
                  onChange={(e) => {
                    const v = parseInt(e.target.value)
                    setMaxApps(isNaN(v) ? 1 : v)
                    setMaxAppsError(validateMaxApps(isNaN(v) ? 1 : v))
                  }}
                  className={`input ${maxAppsError ? '!border-danger' : ''}`}
                  min={1}
                  max={100}
                />
                {maxAppsError && <p className="text-danger text-[11px] mt-1">{maxAppsError}</p>}
              </div>

              {/* Past 24 Hours Checkbox */}
              <div className="flex items-center h-[38px] px-2">
                <label className="flex items-center gap-2 cursor-pointer text-xs font-semibold text-text-primary select-none">
                  <input
                    type="checkbox"
                    checked={past24Only}
                    onChange={(e) => setPast24Only(e.target.checked)}
                    className="rounded border-bg-border bg-bg-secondary text-accent focus:ring-accent w-4 h-4 cursor-pointer"
                  />
                  <span>Past 24 Hours Only</span>
                </label>
              </div>

              {/* Run Button */}
              <button
                onClick={triggerApply}
                disabled={isApplying || !!maxAppsError}
                className="btn-primary h-[38px] min-w-[110px] sm:ml-auto"
              >
                {isApplying ? (
                  <>
                    <Loader2 className="w-4 h-4 animate-spin" />
                    <span>Applying...</span>
                  </>
                ) : (
                  'Run Now'
                )}
              </button>
            </div>

            {/* Available Jobs Count */}
            <div className="text-xs text-text-muted mt-1 bg-bg-secondary border border-bg-border/60 rounded-lg p-2.5 flex items-center justify-between">
              <span>Available Jobs:</span>
              {countLoading ? (
                <span className="flex items-center gap-1.5 font-medium text-text-secondary">
                  <Loader2 className="w-3.5 h-3.5 animate-spin text-accent" />
                  Calculating available jobs...
                </span>
              ) : (
                <span className="font-semibold text-text-primary">
                  {availableJobsCount} available {availableJobsCount === 1 ? 'job' : 'jobs'} to apply
                </span>
              )}
            </div>
          </div>

          {progressLogs.length > 0 && (
            <div className="pt-4 border-t border-bg-border">
              <div className="flex items-center gap-2 mb-3">
                <h3 className="text-xs font-semibold text-text-secondary uppercase tracking-wider">Pipeline Progress</h3>
              </div>
              <div className="space-y-2.5 max-h-60 overflow-y-auto pr-2">
                {progressLogs.map((log, i) => (
                  <div key={i} className="flex items-start gap-3 animate-fade-in text-xs">
                    <div className="mt-0.5 shrink-0">
                      {i === progressLogs.length - 1 && isApplying ? (
                        <Loader2 className="w-3.5 h-3.5 text-accent animate-spin" />
                      ) : (
                        <Check className="w-3.5 h-3.5 text-success" />
                      )}
                    </div>
                    <div className="flex-1 flex items-center justify-between gap-4">
                      <p className="font-medium text-text-primary">{log.message}</p>
                      <span className="text-[10px] text-text-muted shrink-0 tabular-nums">
                        {new Date(log.timestamp).toLocaleTimeString()}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Tabs */}
      <div className="border-b border-bg-border flex gap-6">
        <button
          className={`pb-3 text-sm font-semibold transition-colors relative ${
            activeTab === 'history'
              ? 'text-text-primary after:absolute after:bottom-0 after:left-0 after:right-0 after:h-0.5 after:bg-accent'
              : 'text-text-muted hover:text-text-primary'
          }`}
          onClick={() => setActiveTab('history')}
        >
          Application History
        </button>
        <button
          className={`pb-3 text-sm font-semibold transition-colors relative ${
            activeTab === 'profile'
              ? 'text-text-primary after:absolute after:bottom-0 after:left-0 after:right-0 after:h-0.5 after:bg-accent'
              : 'text-text-muted hover:text-text-primary'
          }`}
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
            <div className="card p-5 flex items-center justify-between gap-4 bg-bg-card border border-bg-border rounded-xl">
              <div className="flex items-center gap-3">
                <div className="w-10 h-10 rounded-xl bg-accent/10 flex items-center justify-center text-accent">
                  <FileText className="w-5 h-5" />
                </div>
                <div>
                  <p className="text-sm font-semibold text-text-primary">Base Resume</p>
                  <p className="text-xs text-text-muted mt-0.5">
                    Version {baseResume.version} · uploaded {baseResume.created_at ? new Date(baseResume.created_at).toLocaleDateString() : ''}
                  </p>
                </div>
              </div>
              {baseResume?.id && <ViewResumeButton resumeId={baseResume.id} fileUrl={baseResume.file_url} />}
            </div>
          )}
          <CandidateProfileForm candidate={candidate} onSuccess={refetch} />
        </div>
      )}

      {/* Gmail error */}
      {gmailError && (
        <div className="p-3.5 rounded-xl bg-danger/10 border border-danger/20 text-danger text-xs flex items-center gap-2 max-w-xl animate-fade-in">
          <AlertTriangle className="w-4 h-4 shrink-0" />
          <span>{gmailError}</span>
        </div>
      )}

      {/* Google OAuth Modal */}
      <Modal
        open={showSimulateModal}
        onClose={() => setShowSimulateModal(false)}
        title="Connect Gmail (Simulation)"
        size="md"
      >
        <div className="flex flex-col items-center text-center gap-4">
          <div className="w-12 h-12 rounded-full bg-accent/10 flex items-center justify-center text-accent">
            <Mail className="w-6 h-6" />
          </div>
          <div>
            <p className="text-xs text-text-muted leading-relaxed">
              Real Google OAuth credentials are not set in environment variables. Simulate a successful authorization flow for testing?
            </p>
          </div>
          <div className="w-full flex gap-3 mt-2">
            <button
              onClick={() => setShowSimulateModal(false)}
              className="btn-secondary flex-1"
            >
              Cancel
            </button>
            <button
              onClick={handleSimulateConnection}
              disabled={connectingGmail}
              className="btn-primary flex-1"
            >
              {connectingGmail ? <><Loader2 className="w-4 h-4 animate-spin" />Connecting...</> : 'Simulate Connection'}
            </button>
          </div>
        </div>
      </Modal>
    </div>
  )
}
