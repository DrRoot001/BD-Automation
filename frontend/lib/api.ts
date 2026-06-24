const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? '/api'

async function fetchJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`)
  if (!res.ok) throw new Error(`API error ${res.status}: ${path}`)
  return res.json() as Promise<T>
}

async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`API error ${res.status}: ${path}`)
  return res.json() as Promise<T>
}

async function patchJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`API error ${res.status}: ${path}`)
  return res.json() as Promise<T>
}

async function putJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`API error ${res.status}: ${path}`)
  return res.json() as Promise<T>
}

async function deleteJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'DELETE',
  })
  if (!res.ok) throw new Error(`API error ${res.status}: ${path}`)
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
  google_connected?: boolean
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
  job_title: string
  company: string
  platform: string
  status: string
  fit_score: number | null
  ats_score: number | null
  submitted_at: string | null
  created_at: string
  error_message?: string | null
  resume_url?: string | null
  cover_letter_url?: string | null
  job_url?: string | null
  candidate_name?: string
  bd_user_name?: string
  bd_user_email?: string
}

export interface InterviewSummary {
  interview_id: string
  company: string
  position: string
  round: number
  type: string
  scheduled_at: string | null
  meeting_url: string | null
  application_id: string
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

// ── API functions ──────────────────────────────────────────────────────────

export const api = {
  createBDUser: (data: { name: string; email: string; password: string; role: string }) =>
    postJSON<BDUser>('/auth/admin/create_bd_user', data),
    
  getAdminUsers: () =>
    fetchJSON<BDUser[]>('/auth/admin/users'),
    
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

  getJobs: (params?: { skip?: number; limit?: number }) => {
    const qs = new URLSearchParams()
    if (params?.skip != null) qs.set('skip', String(params.skip))
    if (params?.limit != null) qs.set('limit', String(params.limit))
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
    fetchJSON<any>(`/applications/${id}`),

  getApplicationHistory: (id: string) =>
    fetchJSON<ApplicationHistoryEntry[]>(`/applications/${id}/history`),

  getJob: (id: string) =>
    fetchJSON<JobSummary>(`/jobs/${id}`),

  getCandidates: () =>
    fetchJSON<Candidate[]>('/candidates'),

  getCandidate: (id: string) =>
    fetchJSON<Candidate>(`/candidates/${id}`),

  triggerApply: (candidateId: string, maxApps: number) =>
    postJSON<any>(`/candidates/${candidateId}/apply`, { max_apps: maxApps }),

  getGoogleAuthUrl: (candidateId: string) =>
    fetchJSON<{ auth_url: string; is_mock: boolean }>(`/candidates/${candidateId}/google/auth-url`),

  exchangeGoogleCode: (candidateId: string, code: string) =>
    postJSON<any>(`/candidates/${candidateId}/google/callback`, { code }),
}
