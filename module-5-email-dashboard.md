# Module 5: Email Intelligence & Analytics Dashboard

| Field | Value |
|-------|-------|
| **Owner** | Full-Stack / Integration Engineer |
| **Build Order** | 5 of 5 (start after Modules 1 & 4 are functional) |
| **Depends On** | Module 1 (API, DB, Celery, Google OAuth), Module 4 (`event:application.submitted`) |

---

## Scope

### This module DOES

- Gmail API integration (OAuth2 via tokens from Module 1)
- Email polling (every 15 minutes via Celery Beat)
- LLM-based email classification (5 categories + UNKNOWN)
- Application-to-email matching (link incoming email to the correct application record)
- Interview detail extraction (date, type, meeting link, interviewer)
- Application status updates triggered by email classification
- Analytics dashboard API (KPIs, conversion funnels, per-platform stats)
- Real-time WebSocket push for status changes
- Next.js frontend dashboard (stats cards, job queue, interview list, charts)

### This module DOES NOT

- Job discovery or scraping (→ Module 2)
- Resume processing or AI scoring (→ Module 3)
- Browser automation or form filling (→ Module 4)
- Database schema design (→ Module 1)

---

## Email Classification System

### Classification Categories

| Category | Label | Status Update | Example Patterns |
|----------|-------|---------------|-----------------|
| **Applied Confirmation** | `APPLIED_CONFIRMATION` | → `CONFIRMED` | "Thank you for applying", "Application received", "We received your application" |
| **Interview Round 1** | `INTERVIEW_R1` | → `INTERVIEW_R1` + create interview record | "Schedule interview", "Recruiter call", "Initial screening", "Phone screen" |
| **Interview Round 2** | `INTERVIEW_R2` | → `INTERVIEW_R2` + create interview record | "Technical interview", "Panel interview", "Coding assessment", "Hiring manager" |
| **Assessment** | `ASSESSMENT` | → `INTERVIEW_R1` + create interview record | "Online assessment", "Take-home assignment", "Coding challenge" |
| **Rejected** | `REJECTED` | → `REJECTED` | "Unfortunately", "decided not to move forward", "not a fit", "other candidates" |
| **Offer** | `OFFER` | → `OFFER` | "Congratulations", "Offer letter", "Compensation package", "We'd like to extend" |
| **Unknown** | `UNKNOWN` | No auto-update | Anything that doesn't match above with confidence > 0.7 |

### Classification Approach

> **LLM-based classification, NOT simple keyword rules.**
> Email formats vary dramatically across ATS platforms (Greenhouse, Lever, Workday, Ashby, iCIMS).
> Simple keyword rules become unreliable at scale.

```python
CLASSIFICATION_PROMPT = """
Classify this email into exactly one category:
- APPLIED_CONFIRMATION: Application was received/acknowledged
- INTERVIEW_R1: First round interview invitation (phone screen, recruiter call)
- INTERVIEW_R2: Later round interview (technical, panel, hiring manager)
- ASSESSMENT: Online assessment or take-home assignment
- REJECTED: Application was rejected
- OFFER: Job offer extended
- UNKNOWN: Cannot determine with confidence

Respond with JSON:
{"classification": "...", "confidence": 0.0-1.0, "reasoning": "..."}

Subject: {subject}
From: {from_addr}
Body:
{body_text}
"""
```

### Confidence Threshold

| Confidence | Action |
|-----------|--------|
| ≥ 0.7 | Auto-update application status |
| < 0.7 | Store as `UNKNOWN`, flag for manual review |

---

## Public Interface

### 1. Email Scanner

```python
class EmailClassification(BaseModel):
    email_id: str                     # our internal UUID
    gmail_id: str                     # Gmail message ID
    classification: Literal[
        "APPLIED_CONFIRMATION", "INTERVIEW_R1", "INTERVIEW_R2",
        "ASSESSMENT", "REJECTED", "OFFER", "UNKNOWN"
    ]
    confidence: float                 # 0.0 - 1.0
    matched_application_id: Optional[str]   # linked application
    matched_company: Optional[str]          # extracted company name
    extracted_data: Optional[dict]          # interview date, link, etc.
    reasoning: str                          # LLM's explanation


async def scan_inbox(
    candidate_id: str,
    since: datetime
) -> list[EmailClassification]:
    """
    1. Get Google OAuth token for candidate (from Module 1)
    2. Query Gmail API: messages after `since` timestamp
       Search query: "newer_than:1d" (or since last scan)
       Exclude: sent mail, drafts
    3. For each new message:
       a. Fetch full message (subject, from, body)
       b. Check if gmail_id already exists in emails table (skip if so)
       c. Classify via LLM
       d. Match to application (see matching logic below)
       e. Store in emails table
       f. If confidence >= 0.7: update application status
    4. Return list of classifications
    """


async def classify_email(
    email_body: str,
    subject: str,
    from_addr: str
) -> EmailClassification:
    """
    Call LLM with classification prompt.
    Model: GPT-4 Turbo (or Claude fallback)
    Temperature: 0.1 (highly deterministic)
    """
```

### 2. Application Matcher

```python
async def match_email_to_application(
    candidate_id: str,
    from_addr: str,
    subject: str,
    body_text: str
) -> Optional[str]:
    """
    Returns application_id if a match is found.
    
    Matching strategy (ordered by reliability):
    1. From address domain → match to companies.domain
       e.g. "noreply@greenhouse.io" → check body for company name
    2. Subject line contains company name → match to applications.job.company
    3. Body contains company name → match to applications.job.company
    4. From address contains company name → direct match
    
    If multiple matches: prefer most recent application (by created_at).
    If no match: return None (email stored but not linked).
    """
```

### 3. Interview Extractor

```python
class InterviewDetails(BaseModel):
    company: str
    position: str
    interview_date: Optional[datetime]
    interview_type: Literal["phone", "video", "onsite", "assessment"]
    meeting_url: Optional[str]          # Zoom, Google Meet, Teams link
    interviewer_name: Optional[str]
    calendar_link: Optional[str]        # .ics or Google Calendar link
    additional_notes: str


async def extract_interview_details(
    email_body: str,
    subject: str
) -> InterviewDetails:
    """
    LLM extraction prompt:
    - Company name, Position title
    - Interview date/time (with timezone)
    - Interview type (phone, video, onsite, assessment)
    - Meeting link (Zoom, Google Meet, MS Teams)
    - Interviewer name
    
    Temperature: 0.1
    """
```

---

## Dashboard API Endpoints

All endpoints are FastAPI routers registered under `/api/dashboard/`.

| Method | Path | Response Schema | Description |
|--------|------|----------------|-------------|
| `GET` | `/api/dashboard/kpis` | `DashboardKPIs` | Summary stats |
| `GET` | `/api/dashboard/applications` | `PaginatedResponse[ApplicationSummary]` | Paginated list |
| `GET` | `/api/dashboard/interviews` | `list[InterviewSummary]` | Upcoming interviews |
| `GET` | `/api/dashboard/analytics` | `AnalyticsData` | Conversion funnel + per-platform |
| `GET` | `/api/dashboard/activity-feed` | `list[ActivityEvent]` | Recent events |
| `WS` | `/ws/updates` | Streaming JSON | Real-time push |

### Response Schemas

```python
class DashboardKPIs(BaseModel):
    total_applied: int
    applied_today: int
    interviews_this_week: int
    success_rate: float               # interviews / applications × 100
    pending_in_queue: int             # status = QUEUED
    total_rejected: int
    total_offers: int


class ApplicationSummary(BaseModel):
    application_id: str
    job_title: str
    company: str
    platform: str
    status: str
    fit_score: Optional[float]
    ats_score: Optional[float]
    submitted_at: Optional[datetime]
    created_at: datetime


class InterviewSummary(BaseModel):
    interview_id: str
    company: str
    position: str
    round: int
    type: str
    scheduled_at: Optional[datetime]
    meeting_url: Optional[str]
    application_id: str


class AnalyticsData(BaseModel):
    conversion_funnel: ConversionFunnel
    per_platform_stats: list[PlatformStats]
    daily_applications: list[DailyCount]     # last 30 days
    avg_time_to_response_hours: Optional[float]


class ConversionFunnel(BaseModel):
    total_applied: int
    total_confirmed: int
    total_r1: int
    total_r2: int
    total_offers: int
    total_rejected: int
    apply_to_r1_rate: float           # target: 12%
    r1_to_r2_rate: float              # target: 41%
    r2_to_offer_rate: float           # target: 20%


class PlatformStats(BaseModel):
    platform: str
    applications: int
    interviews: int
    success_rate: float


class ActivityEvent(BaseModel):
    event_type: str                   # "application.submitted", "email.classified", etc.
    timestamp: datetime
    summary: str                      # human-readable description
    application_id: Optional[str]
```

### WebSocket Protocol

```python
# Client connects to: ws://localhost:8000/ws/updates?token={jwt_token}

# Server pushes events as JSON:
{
    "event": "application.status_changed",
    "data": {
        "application_id": "uuid",
        "from_status": "SUBMITTED",
        "to_status": "CONFIRMED",
        "job_title": "Frontend Engineer",
        "company": "Google",
        "timestamp": "2026-06-18T20:30:00Z"
    }
}

# Event types pushed:
# - application.status_changed
# - application.submitted
# - application.failed
# - email.classified
# - interview.detected
# - job.discovered (count summary)
```

---

## KPIs to Track

| Metric | Formula | Target |
|--------|---------|--------|
| Applications Sent | `COUNT(status >= SUBMITTED)` | — |
| Response Rate | `COUNT(status >= CONFIRMED) / COUNT(status >= SUBMITTED)` | — |
| Apply → R1 | `COUNT(INTERVIEW_R1) / COUNT(SUBMITTED)` | **12%** |
| R1 → R2 | `COUNT(INTERVIEW_R2) / COUNT(INTERVIEW_R1)` | **41%** |
| R2 → Offer | `COUNT(OFFER) / COUNT(INTERVIEW_R2)` | **20%** |
| Rejection Rate | `COUNT(REJECTED) / COUNT(SUBMITTED)` | — |
| Avg Response Time | `AVG(email.received_at - application.submitted_at)` | — |
| Per-Platform Success | Grouped by `jobs.source` | — |

---

## Celery Tasks

| Task Name | Queue | Trigger | Description |
|-----------|-------|---------|-------------|
| `task:scan_candidate_inbox` | `queue:email_scan` | Celery Beat (every 15 min) | Poll Gmail for new emails |
| `task:classify_email` | `queue:email_scan` | Called per email | LLM classification |
| `task:extract_interview` | `queue:email_scan` | After R1/R2 classification | Parse interview details |
| `task:update_application_from_email` | `queue:email_scan` | After classification | Match email → application → update status |
| `task:refresh_analytics` | `queue:email_scan` | Celery Beat (every 1 hr) | Recompute dashboard KPIs |

### Email Scan Pipeline

```
Celery Beat (every 15 min)
  → task:scan_candidate_inbox(candidate_id)
    ├── Get OAuth token from DB
    ├── Query Gmail API (messages since last scan)
    ├── For each new message:
    │   ├── Skip if gmail_id already in emails table
    │   ├── task:classify_email(subject, from, body)
    │   │   └── Returns: classification, confidence
    │   ├── task:match_email_to_application(candidate_id, from, subject, body)
    │   │   └── Returns: application_id (or None)
    │   ├── Store email in emails table
    │   │
    │   ├── If confidence >= 0.7 AND application_id matched:
    │   │   ├── PATCH /api/applications/{id}/status → new status
    │   │   ├── If INTERVIEW_R1 or INTERVIEW_R2:
    │   │   │   └── task:extract_interview(body, subject)
    │   │   │       └── Create interview record in interviews table
    │   │   └── Publish event:email.classified
    │   │
    │   └── If confidence < 0.7:
    │       └── Store as UNKNOWN, no auto-update
    │
    └── Update last_email_scan:{candidate_id} in Redis
```

---

## Events

### Consumed

| Event | Action |
|-------|--------|
| `event:application.submitted` | Start actively watching for confirmation email for this application |
| `event:application.status_changed` | Push real-time update to WebSocket clients |

### Published

| Event | Payload | Consumed By |
|-------|---------|-------------|
| `event:email.classified` | `{email_id, classification, confidence, application_id}` | Dashboard (WebSocket) |
| `event:interview.detected` | `{interview_id, application_id, scheduled_at, type, meeting_url}` | Dashboard (WebSocket) |

---

## Frontend Dashboard (Next.js)

### Page Layout

```
┌─────────────────────────────────────────────────────────────────┐
│  BD Automation Dashboard                           [User] ▼    │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐       │
│  │ Applied  │  │Interviews│  │ Success  │  │ Pending  │       │
│  │   47     │  │    3     │  │  8.5%    │  │   12     │       │
│  │  Today   │  │This Week │  │  Rate    │  │ In Queue │       │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘       │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│  Active Jobs Queue                                              │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  [●] Software Engineer @ Google - Applied 2m ago        │   │
│  │  [○] Frontend Dev @ Meta - Optimizing resume...         │   │
│  │  [○] Full Stack @ Stripe - In queue...                  │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│  Upcoming Interviews                                            │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  📅 Tomorrow 2:00 PM - Google (Technical Round)         │   │
│  │     [Join Zoom] [View Details] [Add to Calendar]        │   │
│  │  📅 Friday 10:00 AM - Netflix (HR Screening)            │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│  Conversion Funnel                                              │
│  Applied (100) → R1 (12) → R2 (5) → Offer (1)                │
│  [═══════════════] [════] [══] [=]                             │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│  Filters & Configuration                                       │
│  [Remote Only ☑] [Min $120k ☐] [Easy Apply Only ☐]            │
│  [Keywords: React, Node, TypeScript] [+]                       │
└─────────────────────────────────────────────────────────────────┘
```

### Frontend Tech Stack

| Technology | Purpose |
|-----------|---------|
| Next.js 14 | React framework with SSR |
| shadcn/ui | Component library |
| Tailwind CSS | Styling |
| React Query / TanStack Query | Server state management |
| Recharts | Conversion funnel charts |
| WebSocket (native) | Real-time updates |

---

## Dependencies

| Dependency | Module | What's Used |
|-----------|--------|-------------|
| `POST /api/auth/google` | M1 | Google OAuth tokens for Gmail API |
| `GET /api/applications` | M1 | Application data for matching + dashboard |
| `PATCH /api/applications/{id}/status` | M1 | Status updates from email classification |
| `GET /api/analytics/summary` | M1 | Pre-computed analytics data |
| Celery `queue:email_scan` | M1 | Task execution |
| Celery Beat schedule | M1 | 15-minute email scan, 1-hour analytics refresh |
| Redis pub/sub | M1 | Event consumption + publishing |
| Redis `last_email_scan:{candidate_id}` | M1 | Track last scan timestamp |
| `event:application.submitted` | M4 | Trigger for email watching |

### External Dependencies (pip — backend)

```
google-auth>=2.0          # Google OAuth
google-api-python-client  # Gmail API
google-auth-oauthlib      # OAuth flow
openai>=1.0               # LLM classification
websockets                # WebSocket server
```

### External Dependencies (npm — frontend)

```
next@14
@tanstack/react-query
recharts
tailwindcss
shadcn/ui components
```

---

## Implementation Sequence

| Step | Task | Est. Time | Notes |
|------|------|-----------|-------|
| 1 | Gmail API integration (OAuth token flow from Module 1) | 1 day | Use google-api-python-client |
| 2 | Email fetcher (poll inbox, paginate, filter by date) | 1 day | Handle Gmail API pagination |
| 3 | LLM email classifier (5 categories + UNKNOWN) | 1.5 days | Prompt engineering, temperature tuning |
| 4 | Application matcher (email → application linking) | 1 day | Domain matching, company name extraction |
| 5 | Interview detail extractor | 1 day | LLM extraction of dates, links, names |
| 6 | Status update pipeline (email → classify → match → update) | 1 day | Wire up full pipeline |
| 7 | Dashboard API endpoints (KPIs, applications, interviews) | 1.5 days | SQL queries with proper aggregation |
| 8 | WebSocket server for real-time push | 1 day | Redis pub/sub → WebSocket broadcast |
| 9 | Next.js frontend: layout + stats cards + job queue | 2 days | React components + API integration |
| 10 | Conversion funnel chart + analytics page | 1 day | Recharts visualization |
| 11 | Interview list with action buttons (Join, Calendar) | 0.5 day | UI + deep links |
| 12 | Confidence threshold + flagging system | 0.5 day | Low-confidence review queue |
| 13 | Monitoring: classification accuracy, processing latency | 0.5 day | Logging + metrics |

**Total: ~13.5 days**

---

## Integration Checklist

> Every item must pass before declaring this module ready for integration.

- [ ] **Gmail Access**: OAuth token from Module 1 successfully authenticates with Gmail API
- [ ] **Email Fetch**: New emails since last scan are correctly retrieved (no duplicates, no missed)
- [ ] **Classification Accuracy**: LLM correctly classifies ≥ 90% of test emails across all categories
- [ ] **Confidence Gate**: Email with confidence 0.5 is stored as UNKNOWN and does NOT auto-update status
- [ ] **Confidence Gate**: Email with confidence 0.85 auto-updates the matched application's status
- [ ] **Application Matching**: Email from "noreply@greenhouse.io" mentioning "Stripe" correctly links to the Stripe application
- [ ] **Interview Created**: When INTERVIEW_R1 email is classified, a row is inserted into `interviews` table with correct details
- [ ] **Status Transition**: `PATCH /api/applications/{id}/status` from SUBMITTED → CONFIRMED succeeds when confirmation email arrives
- [ ] **WebSocket**: When `event:application.status_changed` fires, connected WebSocket clients receive the update within 2 seconds
- [ ] **Dashboard KPIs**: `GET /api/dashboard/kpis` returns counts that match `SELECT COUNT(*)` queries on the database
- [ ] **Conversion Funnel**: Apply→R1 rate matches `COUNT(INTERVIEW_R1) / COUNT(SUBMITTED)` exactly
- [ ] **Celery Beat**: Email scan runs every 15 minutes without manual intervention
- [ ] **No Duplicate Processing**: Same gmail_id is never processed twice (unique constraint enforced)
- [ ] **Frontend Loads**: Next.js dashboard renders with real data from all API endpoints
- [ ] **Real-time Updates**: Submitting a new application via Module 4 shows up on the dashboard within 30 seconds
