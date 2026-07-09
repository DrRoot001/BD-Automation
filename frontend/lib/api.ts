const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? '/api'

async function handleResponseError(res: Response, path: string): Promise<never> {
  let detail: string | null = null
  try {
    const errData = await res.json()
    if (errData && errData.detail) {
      detail = typeof errData.detail === 'string'
        ? errData.detail
        : JSON.stringify(errData.detail)
    }
  } catch {
    // Response body is not JSON — fall through to generic message
  }
  throw new Error(detail ?? `API error ${res.status}: ${path}`)
}

async function fetchJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`)
  if (!res.ok) await handleResponseError(res, path)
  return res.json() as Promise<T>
}

async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) await handleResponseError(res, path)
  return res.json() as Promise<T>
}

async function patchJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) await handleResponseError(res, path)
  return res.json() as Promise<T>
}

async function putJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) await handleResponseError(res, path)
  return res.json() as Promise<T>
}

async function deleteJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'DELETE',
  })
  if (!res.ok) await handleResponseError(res, path)
  return res.json() as Promise<T>
}

// ── Types ─────────────────────────────────────────────────────────────────

export interface JobSummary {
  id: string
  title: string
  company: string
  location: string | null
  source: string
  source_url: string
  canonical_url: string | null
  description: string | null
  skills: string[]
  salary_min: number | null
  salary_max: number | null
  pay_period: 'hourly' | 'yearly' | null
  job_type: 'full-time' | 'contract' | 'part-time' | null
  posted_at: string | null
  is_duplicate: boolean
  created_at: string
}

export interface Candidate {
  id: string
  user_id?: string | null
  name: string
  email: string
  phone?: string
  location?: string
  work_auth?: string
  tech_stack: string[]
  years_exp?: number
  linkedin_url?: string
  title?: string
  google_connected?: boolean
  // 0 = pipeline running, 1 = pipeline stopped by operator
  automation_paused?: number
  created_at: string
  updated_at: string
}

export interface ApplicationHistoryEntry {
  id: string
  application_id: string
  from_status: string | null
  to_status: string
  meta_data: Record<string, unknown> | null
  created_at: string
}

export interface DashboardKPIs {
  total_applied: number
  applied_today: number
  interviews_this_week: number
  success_rate: number
  pending_in_queue: number
  total_rejected: number
  total_offers: number
}

export interface ApplicationSummary {
  application_id: string
  job_id: string
  candidate_id?: string
  job_title: string
  company: string
  platform: string
  status: string
  paused?: boolean
  fit_score: number | null
  ats_score: number | null
  ats_score_before?: number | null
  ats_score_after?: number | null
  resume_is_base?: boolean | null
  submitted_at: string | null
  created_at: string
  error_message?: string | null
  failure_reason?: string | null
  resume_url?: string | null
  resume_id?: string | null
  cover_letter_url?: string | null
  screenshot_url?: string | null
  job_url?: string | null
  candidate_name?: string
  bd_user_name?: string
  bd_user_email?: string
}

export interface ApplicationDetail extends ApplicationSummary {
  job_description?: string
  match_reason?: string
  tailored_resume_url?: string
  cover_letter_text?: string
  logs?: Array<{ timestamp: string; message: string }>
}

export interface InterviewSummary {
  interview_id: string
  company: string
  position: string
  round: number
  type: string
  scheduled_at: string | null
  meeting_url: string | null
  application_id?: string | null
  received_at?: string | null
  status?: string | null
}

export interface ConversionFunnel {
  total_applied: number
  total_confirmed: number
  total_r1: number
  total_r2: number
  total_offers: number
  total_rejected: number
  apply_to_r1_rate: number
  r1_to_r2_rate: number
  r2_to_offer_rate: number
}

export interface PlatformStats {
  platform: string
  applications: number
  interviews: number
  success_rate: number
}

export interface DailyCount {
  date: string
  count: number
}

export interface AnalyticsData {
  conversion_funnel: ConversionFunnel
  per_platform_stats: PlatformStats[]
  daily_applications: DailyCount[]
  avg_time_to_response_hours: number | null
}

export interface ActivityEvent {
  event_type: string
  timestamp: string
  summary: string
  application_id: string | null
}

export interface BDUser {
  id: string
  email: string
  role: string
  full_name?: string
  created_at?: string
  is_active?: boolean
}

export interface RetryResponse {
  message: string
  status: string
}

export interface ApplyTriggerResponse {
  message: string
  status: string
  task_id?: string
}

export interface GoogleCodeResponse {
  message: string
  connected: boolean
}

export interface MatchingDetail {
  job_title: string
  company: string
  score: number
  passed: boolean
}

export interface MatchingRunResponse {
  jobs_scanned: number
  pgvector_passed: number
  llm_passed: number
  enqueued_count: number
  details: MatchingDetail[]
}

export interface JobDiscoveryResponse {
  message: string
  task_id?: string
}

export interface DiscoveryStatusResponse {
  running: boolean
  last_result?: {
    status: 'completed' | 'failed'
    total_discovered?: number
    total_saved?: number
    errors?: string[]
  }
}

// ── API functions ──────────────────────────────────────────────────────────

export const api = {
  createBDUser: (data: { name: string; email: string; password: string; role: string }) =>
    postJSON<BDUser>('/auth/admin/create_bd_user', data),
    
  getAdminUsersCount: () =>
    fetchJSON<{ total_count: number }>('/auth/admin/users/count'),

  getAdminUsers: (params?: { skip?: number; limit?: number }) => {
    const qs = new URLSearchParams()
    if (params?.skip != null) qs.set('skip', String(params.skip))
    if (params?.limit != null) qs.set('limit', String(params.limit))
    const query = qs.toString()
    return fetchJSON<BDUser[]>(`/auth/admin/users${query ? `?${query}` : ''}`)
  },
    
  updateUserRole: (userId: string, role: string) =>
    patchJSON<BDUser>(`/auth/admin/users/${userId}/role`, { role }),

  updateUser: (userId: string, data: { name?: string; email?: string; role?: string }) =>
    patchJSON<BDUser>(`/auth/admin/users/${userId}`, data),

  updateUserPassword: (userId: string, password: string) =>
    putJSON<{ message: string }>(`/auth/admin/users/${userId}/password`, { password }),

  deleteUser: (userId: string) =>
    deleteJSON<{ message: string }>(`/auth/admin/users/${userId}`),

  mergeUsers: (sourceUserId: string, targetUserId: string) =>
    postJSON<{ message: string }>('/auth/admin/users/merge', { source_user_id: sourceUserId, target_user_id: targetUserId }),

  getJobsCount: (params?: { candidateId?: string; search?: string; source?: string; jobType?: string; timeFilter?: string }) => {
    const qs = new URLSearchParams()
    if (params?.candidateId) qs.set('candidate_id', params.candidateId)
    if (params?.search) qs.set('search', params.search)
    if (params?.source) qs.set('source', params.source)
    if (params?.jobType) qs.set('job_type', params.jobType)
    if (params?.timeFilter) qs.set('time_filter', params.timeFilter)
    const query = qs.toString()
    return fetchJSON<{ total_count: number }>(`/jobs/count${query ? `?${query}` : ''}`)
  },

  getJobs: (params?: { 
    skip?: number; 
    limit?: number; 
    candidateId?: string; 
    search?: string; 
    source?: string; 
    jobType?: string; 
    timeFilter?: string; 
  }) => {
    const qs = new URLSearchParams()
    if (params?.skip != null) qs.set('skip', String(params.skip))
    if (params?.limit != null) qs.set('limit', String(params.limit))
    if (params?.candidateId) qs.set('candidate_id', params.candidateId)
    if (params?.search) qs.set('search', params.search)
    if (params?.source) qs.set('source', params.source)
    if (params?.jobType) qs.set('job_type', params.jobType)
    if (params?.timeFilter) qs.set('time_filter', params.timeFilter)
    const query = qs.toString()
    return fetchJSON<JobSummary[]>(`/jobs${query ? `?${query}` : ''}`)
  },
  
  getKPIs: (candidateId?: string) =>
    fetchJSON<DashboardKPIs>(
      `/dashboard/kpis${candidateId ? `?candidate_id=${candidateId}` : ''}`,
    ),

  getApplications: (params?: { candidateId?: string; status?: string; limit?: number; offset?: number }) => {
    const qs = new URLSearchParams()
    if (params?.candidateId) qs.set('candidate_id', params.candidateId)
    if (params?.status) qs.set('status', params.status)
    if (params?.limit != null) qs.set('limit', String(params.limit))
    if (params?.offset != null) qs.set('offset', String(params.offset))
    const query = qs.toString()
    return fetchJSON<ApplicationSummary[]>(`/dashboard/applications${query ? `?${query}` : ''}`)
  },

  getInterviews: (upcomingOnly = true, candidateId?: string) => {
    const qs = new URLSearchParams()
    qs.set('upcoming_only', String(upcomingOnly))
    if (candidateId) qs.set('candidate_id', candidateId)
    return fetchJSON<InterviewSummary[]>(`/dashboard/interviews?${qs.toString()}`)
  },

  getAnalytics: (candidateId?: string) =>
    fetchJSON<AnalyticsData>(
      `/dashboard/analytics${candidateId ? `?candidate_id=${candidateId}` : ''}`,
    ),

  getActivityFeed: (limit = 20, candidateId?: string) => {
    const qs = new URLSearchParams()
    qs.set('limit', String(limit))
    if (candidateId) qs.set('candidate_id', candidateId)
    return fetchJSON<ActivityEvent[]>(`/dashboard/activity-feed?${qs.toString()}`)
  },

  getApplication: (id: string) =>
    fetchJSON<ApplicationDetail>(`/applications/${id}`),

  getApplicationHistory: (id: string) =>
    fetchJSON<ApplicationHistoryEntry[]>(`/applications/${id}/history`),

  retryApplication: (appId: string) =>
    postJSON<RetryResponse>(`/applications/${appId}/retry`, {}),

  // Cancel (withdraw) an application without deleting it — frees the queue slot
  // but keeps the audit trail.
  cancelApplication: (appId: string) =>
    postJSON<ApplicationSummary>(`/applications/${appId}/cancel`, {}),

  // Hard-delete an application and its history. Destructive.
  deleteApplication: (appId: string) =>
    deleteJSON<{ deleted: number; application_id: string }>(`/applications/${appId}`),

  // Hold / release a single application.
  pauseApplication: (appId: string) =>
    postJSON<ApplicationSummary>(`/applications/${appId}/pause`, {}),
  resumeApplication: (appId: string) =>
    postJSON<ApplicationSummary>(`/applications/${appId}/unpause`, {}),

  getJob: (id: string) =>
    fetchJSON<JobSummary>(`/jobs/${id}`),

  getCandidates: () =>
    fetchJSON<Candidate[]>('/candidates'),

  getCandidate: (id: string) =>
    fetchJSON<Candidate>(`/candidates/${id}`),

  updateCandidate: (id: string, data: Record<string, unknown>) =>
    putJSON<Candidate>(`/candidates/${id}`, data),

  triggerApply: (candidateId: string, maxApps: number, timeFilter?: string, platform?: string) =>
    postJSON<ApplyTriggerResponse>(`/candidates/${candidateId}/apply`, {
      max_apps: maxApps,
      time_filter: timeFilter,
      platform: platform
    }),

  getGoogleAuthUrl: (candidateId: string) =>
    fetchJSON<{ auth_url: string; is_mock: boolean }>(`/candidates/${candidateId}/google/auth-url`),

  exchangeGoogleCode: (candidateId: string, code: string) =>
    postJSON<GoogleCodeResponse>(`/candidates/${candidateId}/google/callback`, { code }),

  disconnectGmail: (candidateId: string) =>
    postJSON<{ status: string; message: string }>(`/candidates/${candidateId}/google/disconnect`, {}),

  runMatching: (candidateId: string) =>
    postJSON<MatchingRunResponse>(`/candidates/${candidateId}/run-matching`, {}),

  // Stop the whole apply pipeline for a candidate: blocks new matching runs,
  // halts an active run at its next job boundary, and bulk-pauses every
  // in-flight application so none of them block new job queues.
  stopPipeline: (candidateId: string) =>
    postJSON<{ status: string; paused_applications: number; in_browser_finishing: number }>(
      `/candidates/${candidateId}/pipeline/pause`, {}),

  resumePipeline: (candidateId: string) =>
    postJSON<{ status: string; resumed_applications: number; redispatched_queued: number }>(
      `/candidates/${candidateId}/pipeline/resume`, {}),

  triggerJobDiscovery: () =>
    postJSON<JobDiscoveryResponse>('/jobs/discover', {}),

  getDiscoveryStatus: () =>
    fetchJSON<DiscoveryStatusResponse>('/jobs/discover/status'),

  getPlatforms: () =>
    fetchJSON<string[]>('/jobs/platforms'),
}
