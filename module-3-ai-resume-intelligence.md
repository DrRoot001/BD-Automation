# Module 3: AI & Resume Intelligence Engine

| Field | Value |
|-------|-------|
| **Owner** | AI / ML Engineer |
| **Build Order** | 3 of 5 (start after Modules 1 & 2 are functional) |
| **Depends On** | Module 1 (API, DB, Celery), Module 2 (NormalizedJob schema, `event:job.passed_filters`) |

---

## Scope

### This module DOES

- Resume parsing (PDF/DOCX → structured data)
- Job-candidate fit scoring (skills overlap, experience relevance)
- ATS compatibility scoring (keyword match, formatting, semantic similarity)
- Combined scoring with 70% threshold gate
- Resume tailoring (**Summary, Keywords, Skills ONLY**)
- Cover letter generation from resume + JD
- Screening question answering
- Embedding generation for matching
- Application package orchestration (score → tailor → cover letter → queue)

### This module DOES NOT

- Job scraping or platform adapters (→ Module 2)
- Browser automation or form filling (→ Module 4)
- Email monitoring or classification (→ Module 5)
- Frontend dashboard (→ Module 5)
- Database schema design (→ Module 1)

---

## ⚠️ Critical Rules

> These rules are **non-negotiable**. Violations cause candidate blacklisting.

| Rule | Description |
|------|-------------|
| **Education is LOCKED** | The Education section of a resume is NEVER modified by any AI process |
| **Experience is LOCKED** | The Experience/Work History section is NEVER modified |
| **No Fabrication** | AI must NEVER add skills, technologies, or experience the candidate does not have |
| **No Hallucination** | All resume content must be grounded in the candidate's actual profile |
| **Threshold Gate** | Combined score < 70 → **do not proceed** to resume tailoring or application |
| **Truthfulness** | Cover letters must reference only real experience from the candidate's resume |

---

## Public Interface

### 1. Resume Parser

```python
class ResumeSection(BaseModel):
    summary: str
    skills: list[str]
    keywords: list[str]           # extracted from summary + skills
    experience: list[ExperienceEntry]   # READ-ONLY after parsing
    education: list[EducationEntry]     # READ-ONLY after parsing
    certifications: list[str]

class ExperienceEntry(BaseModel):
    company: str
    title: str
    start_date: str
    end_date: Optional[str]       # None = "Present"
    description: str
    technologies: list[str]

class EducationEntry(BaseModel):
    institution: str
    degree: str
    field: str
    graduation_year: Optional[int]

class ResumeData(BaseModel):
    candidate_id: str
    resume_id: str
    file_url: str
    sections: ResumeSection
    raw_text: str


async def parse_resume(file_url: str) -> ResumeData:
    """
    Parse PDF/DOCX into structured ResumeData.
    Uses: pdfplumber/PyMuPDF for extraction + LLM for structuring.
    """
```

### 2. Job Fit Scoring

```python
class MatchResult(BaseModel):
    job_id: str
    candidate_id: str
    fit_score: float              # 0-100
    ats_score: float              # 0-100
    combined_score: float         # (fit_score × 0.5) + (ats_score × 0.5)
    should_apply: bool            # combined_score > 70
    matching_skills: list[str]    # skills found in both resume and JD
    missing_skills: list[str]     # skills in JD but not in resume
    experience_match: float       # 0-100, years + domain relevance
    reasoning: str                # LLM explanation of score


async def score_job_fit(
    candidate: CandidateProfile,
    resume: ResumeData,
    job: NormalizedJob
) -> MatchResult:
    """
    Scoring formula:
      fit_score = (skills_overlap × 0.40) + (experience_relevance × 0.35)
                + (location_match × 0.15) + (seniority_match × 0.10)

      combined_score = (fit_score × 0.5) + (ats_score × 0.5)

    If combined_score < 70: should_apply = False, pipeline STOPS here.
    """
```

### 3. ATS Scoring

```python
class ATSScore(BaseModel):
    overall: float                # 0-100, weighted sum
    keyword_match: float          # 40% weight — JD keywords found in resume
    skills_overlap: float         # 25% weight — technical skills match
    experience_relevance: float   # 20% weight — years + domain
    education_match: float        # 10% weight — degree requirements met
    formatting_score: float       # 5%  weight — ATS-friendly structure
    missing_keywords: list[str]   # keywords in JD absent from resume


async def calculate_ats_score(
    resume: ResumeData,
    job: NormalizedJob
) -> ATSScore:
    """
    Uses:
    - TF-IDF for keyword extraction from JD
    - Exact + fuzzy matching against resume text
    - Embedding cosine similarity for semantic overlap
    - Rule-based formatting checks (no tables, standard sections)
    """
```

### 4. Resume Tailoring

```python
class TailoredResume(BaseModel):
    candidate_id: str
    job_id: str
    version: int
    original_resume_id: str
    modified_summary: str         # rewritten to emphasize JD-relevant experience
    modified_skills: list[str]    # reordered + JD keywords naturally added
    modified_keywords: list[str]  # optimized keyword list
    # ── LOCKED SECTIONS (copied verbatim from original) ──
    experience: list[ExperienceEntry]   # UNCHANGED
    education: list[EducationEntry]     # UNCHANGED
    # ────────────────────────────────────────────────────
    pdf_url: str                  # URL of generated PDF
    ats_score_before: float
    ats_score_after: float


async def tailor_resume(
    resume: ResumeData,
    job: NormalizedJob,
    target_ats_score: int = 85
) -> TailoredResume:
    """
    LLM Prompt Rules (enforced in system prompt):
    1. Do NOT fabricate experience or skills
    2. Do NOT modify Education or Experience sections
    3. Rephrase Summary to emphasize JD-relevant achievements
    4. Reorder Skills to prioritize JD requirements
    5. Add missing keywords ONLY if candidate has genuine related experience
    6. Keep formatting ATS-friendly (no tables, standard fonts, clear sections)
    7. Maintain truthfulness — only use existing experience

    Post-processing validation:
    - Assert experience sections are byte-identical to original
    - Assert education sections are byte-identical to original
    - Re-score with ATS engine to verify improvement
    """
```

### 5. Cover Letter Generator

```python
class CoverLetter(BaseModel):
    candidate_id: str
    job_id: str
    content: str                  # formatted cover letter text
    pdf_url: str                  # URL of generated PDF


async def generate_cover_letter(
    resume: ResumeData,
    job: NormalizedJob
) -> CoverLetter:
    """
    LLM generates personalized cover letter using ONLY:
    - Candidate's actual experience from resume
    - Job description requirements
    - Company name and role title

    Does NOT require:
    - Company funding info
    - Company size
    - Hiring activity data

    Temperature: 0.5 (slightly creative but grounded)
    Max tokens: 800
    """
```

### 6. Screening Question Answerer

```python
async def answer_screening_questions(
    questions: list[str],
    resume: ResumeData,
    job: NormalizedJob
) -> dict[str, str]:
    """
    Returns: {question_text: answer_text}

    LLM prompt constraints:
    - Be concise and professional
    - Base answers on actual resume content
    - Never fabricate certifications or clearances
    - For yes/no questions about authorization/sponsorship,
      use candidate.work_auth to determine answer
    - For salary expectations, use job salary range if available

    Temperature: 0.3 (deterministic, factual)
    """
```

---

## LLM Configuration

| Model | Use Case | Temperature | Max Tokens |
|-------|----------|-------------|------------|
| GPT-4 Turbo | Resume tailoring, cover letters | 0.3 (resume), 0.5 (cover letter) | 2000 |
| text-embedding-3-small | Job matching, dedup embeddings | N/A | N/A |
| Claude 3.5 Sonnet | Fallback when GPT-4 unavailable | Same as above | Same |

### Grounding System Prompt (prepended to all LLM calls)

```
SYSTEM: You are an expert resume optimizer. You MUST follow these rules:
1. NEVER fabricate experience, skills, or qualifications.
2. NEVER add technologies the candidate has not used.
3. ONLY modify: Summary, Skills list, and Keywords.
4. Education and Experience sections are READ-ONLY.
5. All content must be truthful and verifiable.
6. If you cannot improve the resume without fabrication, return it unchanged.
```

---

## Celery Tasks

| Task Name | Queue | Trigger | Description |
|-----------|-------|---------|-------------|
| `task:score_job_match` | `queue:resume_generation` | `event:job.passed_filters` | Compute fit + ATS scores for candidate × job |
| `task:tailor_resume` | `queue:resume_generation` | After scoring (if should_apply=True) | Generate tailored resume |
| `task:generate_cover_letter` | `queue:resume_generation` | After resume tailoring | Create cover letter |
| `task:prepare_application_package` | `queue:resume_generation` | Orchestrator | Score → Tailor → Cover Letter → Queue for M4 |

### Orchestration Pipeline

```
event:job.passed_filters received
  → task:prepare_application_package(job_id, candidate_id)
    ├── Fetch candidate profile: GET /api/candidates/{id}
    ├── Fetch base resume: GET /api/resumes/{candidate_id}?is_base=true
    ├── Fetch job: GET /api/jobs/{job_id}
    │
    ├── Step 1: score_job_fit(candidate, resume, job) → MatchResult
    │   └── If combined_score < 70: STOP, update status → ANALYZED (no MATCHED)
    │
    ├── Step 2: Update application status → MATCHED
    │
    ├── Step 3: tailor_resume(resume, job) → TailoredResume
    │   ├── Validate: experience unchanged, education unchanged
    │   ├── Store: POST /api/resumes (new version)
    │   └── Update status → RESUME_UPDATED
    │
    ├── Step 4: generate_cover_letter(resume, job) → CoverLetter
    │   ├── Store cover_letter_url on application
    │   └── Update status → COVER_LETTER_CREATED
    │
    ├── Step 5: answer_screening_questions (pre-generate common answers)
    │
    ├── Step 6: Update status → QUEUED
    │
    └── Publish event:application.package_ready
        {application_id, resume_url, cover_letter_url, screening_answers}
```

---

## Events

### Consumed

| Event | Action |
|-------|--------|
| `event:job.passed_filters` | Triggers `task:prepare_application_package` for each active candidate |

### Published

| Event | Payload | Consumed By |
|-------|---------|-------------|
| `event:job.matched` | `{job_id, candidate_id, combined_score, should_apply}` | M5 (dashboard) |
| `event:resume.tailored` | `{resume_id, job_id, ats_score_before, ats_score_after}` | M5 (dashboard) |
| `event:application.package_ready` | `{application_id, resume_url, cover_letter_url, screening_answers}` | **M4** (triggers browser automation) |

---

## Dependencies

| Dependency | Module | What's Used |
|-----------|--------|-------------|
| `GET /api/candidates/{id}` | M1 | Fetch candidate profile |
| `GET /api/resumes/{candidate_id}?is_base=true` | M1 | Fetch base resume |
| `GET /api/jobs/{id}` | M1 | Fetch job details |
| `POST /api/resumes` | M1 | Store tailored resume versions |
| `POST /api/applications` | M1 | Create application record |
| `PATCH /api/applications/{id}/status` | M1 | Transition state machine |
| Celery `queue:resume_generation` | M1 | Task execution |
| Redis pub/sub | M1 | Event publishing/subscribing |
| `NormalizedJob` schema | M2 | Job data format |
| `event:job.passed_filters` | M2 | Pipeline trigger |

### External Dependencies (pip)

```
openai>=1.0              # GPT-4 + embeddings
anthropic>=0.20          # Claude fallback
pdfplumber               # PDF text extraction
python-docx              # DOCX parsing
scikit-learn             # TF-IDF, cosine similarity
spacy                    # NLP for skill extraction
reportlab                # PDF generation for tailored resumes
numpy                    # Vector operations
tiktoken                 # Token counting for LLM calls
```

---

## Implementation Sequence

| Step | Task | Est. Time | Notes |
|------|------|-----------|-------|
| 1 | Resume parser: PDF/DOCX → `ResumeData` | 1.5 days | pdfplumber + LLM structuring |
| 2 | Embedding generation (text-embedding-3-small) | 0.5 day | Shared with Module 2 dedup |
| 3 | ATS scoring engine (TF-IDF + semantic similarity) | 1.5 days | 5 scoring dimensions |
| 4 | Job fit scoring engine (skills overlap + experience) | 1 day | Includes LLM reasoning |
| 5 | Combined scoring pipeline with 70% threshold gate | 0.5 day | Gate logic + status updates |
| 6 | Resume tailoring engine (Summary/Keywords/Skills) | 2 days | LLM prompting + validation |
| 7 | Post-tailoring validation (assert Education/Experience unchanged) | 0.5 day | Byte-level comparison |
| 8 | Cover letter generator | 1 day | LLM + PDF generation |
| 9 | Screening question answerer | 1 day | Common Q&A patterns |
| 10 | `task:prepare_application_package` orchestrator | 1 day | End-to-end pipeline |
| 11 | LLM fallback (GPT-4 → Claude) | 0.5 day | Error handling, retry |
| 12 | Logging: token usage, latency, scores per job | 0.5 day | Cost tracking |

**Total: ~11.5 days**

---

## Integration Checklist

> Every item must pass before declaring this module ready for integration.

- [ ] **Event Trigger**: `event:job.passed_filters` from Module 2 correctly triggers `task:prepare_application_package`
- [ ] **Score Gate**: Job with combined_score = 65 does NOT produce an application package; status stays at ANALYZED
- [ ] **Score Gate**: Job with combined_score = 78 DOES produce a full package; status reaches QUEUED
- [ ] **Resume Integrity**: Tailored resume's Experience section is byte-identical to original
- [ ] **Resume Integrity**: Tailored resume's Education section is byte-identical to original
- [ ] **Resume Storage**: `POST /api/resumes` stores tailored version with correct `version` number and `tailored_for_job_id`
- [ ] **ATS Improvement**: `ats_score_after >= ats_score_before` for tailored resumes (verify with re-scoring)
- [ ] **Cover Letter Grounding**: Cover letter does NOT mention skills/experience absent from resume
- [ ] **Screening Answers**: Work authorization answer matches candidate's `work_auth` field
- [ ] **Event Published**: `event:application.package_ready` is published with correct `application_id`, `resume_url`, `cover_letter_url`
- [ ] **State Transitions**: Application status follows: FOUND → ANALYZED → MATCHED → RESUME_UPDATED → COVER_LETTER_CREATED → QUEUED
- [ ] **LLM Fallback**: When GPT-4 returns 429/500, system falls back to Claude and completes successfully
- [ ] **Token Logging**: Every LLM call logs model, tokens_in, tokens_out, latency_ms, cost_usd
