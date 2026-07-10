'use client'

import React, { useState, useRef, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Upload, FileText, CheckCircle, AlertTriangle, Loader2, X, Plus, ExternalLink } from 'lucide-react'

// Sentinel select value that reveals the free-text "Other…" category input.
const OTHER_CATEGORY = '__other__'

const TECH_OPTIONS = [
  'Python', 'JavaScript', 'TypeScript', 'Java', 'C++', 'C#', 'Go', 'Rust', 'Ruby', 'PHP',
  'Swift', 'Kotlin', 'Scala', 'R', 'MATLAB', 'SQL', 'Bash/Shell',
  'React', 'Next.js', 'Vue.js', 'Angular', 'Svelte', 'Node.js', 'Express', 'FastAPI',
  'Django', 'Flask', 'Spring Boot', 'Rails', 'Laravel', 'GraphQL', 'REST APIs',
  'PostgreSQL', 'MySQL', 'MongoDB', 'Redis', 'Elasticsearch', 'SQLite',
  'AWS', 'GCP', 'Azure', 'Docker', 'Kubernetes', 'Terraform', 'CI/CD',
  'Machine Learning', 'Deep Learning', 'PyTorch', 'TensorFlow', 'LLMs', 'NLP',
  'React Native', 'Flutter', 'iOS', 'Android',
  'Git', 'Linux', 'Microservices', 'System Design',
]

// ── Validation helpers ──────────────────────────────────────────────────────
const NAME_RE = /^[a-zA-Z\s'\-\.]{2,}$/
const EMAIL_RE = /^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z]{2,})+$/
const PHONE_RE = /^(\+?1?\s?)?(\(?\d{3}\)?[\s.\-]?)?\d{3}[\s.\-]?\d{4}$/

function validate(field: string, value: string): string | null {
  if (field === 'name') {
    if (!value.trim()) return 'Name is required'
    if (!NAME_RE.test(value.trim())) return 'Name must contain only letters, spaces, hyphens, or apostrophes (min 2 chars)'
  }
  if (field === 'email') {
    if (!value.trim()) return 'Email is required'
    if (!EMAIL_RE.test(value.trim())) return 'Enter a valid email address (e.g. name@domain.com)'
  }
  if (field === 'phone' && value.trim()) {
    if (!PHONE_RE.test(value.trim())) return 'Enter a valid phone number (e.g. +1 555 000 0000)'
  }
  return null
}

// ── Tech Stack Tag Input ─────────────────────────────────────────────────────
function TechStackInput({
  selected,
  onChange,
}: {
  selected: string[]
  onChange: (tags: string[]) => void
}) {
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)

  const filtered = TECH_OPTIONS.filter(
    (o) =>
      !selected.includes(o) &&
      o.toLowerCase().includes(query.toLowerCase()),
  ).slice(0, 10)

  const addTag = (tag: string) => {
    if (!selected.includes(tag)) onChange([...selected, tag])
    setQuery('')
    setOpen(false)
  }

  const removeTag = (tag: string) => onChange(selected.filter((t) => t !== tag))

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if ((e.key === 'Enter' || e.key === ',') && query.trim()) {
      e.preventDefault()
      addTag(query.trim())
    }
    if (e.key === 'Backspace' && !query && selected.length) {
      removeTag(selected[selected.length - 1])
    }
  }

  return (
    <div className="relative">
      <div
        className="input min-h-[42px] flex flex-wrap gap-1.5 items-center cursor-text"
        onClick={() => setOpen(true)}
      >
        {selected.map((tag) => (
          <span
            key={tag}
            className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-accent/10 text-accent text-xs font-medium"
          >
            {tag}
            <button
              type="button"
              onClick={(e) => { e.stopPropagation(); removeTag(tag) }}
              className="hover:text-accent/70 transition-colors"
            >
              <X className="w-3 h-3" />
            </button>
          </span>
        ))}
        <input
          type="text"
          value={query}
          onChange={(e) => { setQuery(e.target.value); setOpen(true) }}
          onKeyDown={handleKeyDown}
          onFocus={() => setOpen(true)}
          onBlur={() => setTimeout(() => setOpen(false), 150)}
          placeholder={selected.length === 0 ? 'Search or type a technology…' : ''}
          className="flex-1 min-w-[120px] bg-transparent outline-none text-sm text-text-primary placeholder:text-text-muted"
        />
      </div>
      {open && (filtered.length > 0 || query.trim()) && (
        <div className="absolute z-20 top-full left-0 right-0 mt-1 bg-bg-card border border-bg-border rounded-lg shadow-card max-h-52 overflow-y-auto">
          {query.trim() && !TECH_OPTIONS.some((o) => o.toLowerCase() === query.toLowerCase()) && (
            <button
              type="button"
              onMouseDown={() => addTag(query.trim())}
              className="flex items-center gap-2 w-full text-left px-3 py-2 text-sm text-text-secondary hover:bg-bg-hover"
            >
              <Plus className="w-3.5 h-3.5 text-accent" />
              Add "{query.trim()}"
            </button>
          )}
          {filtered.map((opt) => (
            <button
              key={opt}
              type="button"
              onMouseDown={() => addTag(opt)}
              className="flex w-full text-left px-3 py-2 text-sm text-text-primary hover:bg-bg-hover"
            >
              {opt}
            </button>
          ))}
        </div>
      )}
      <p className="text-[11px] text-text-muted mt-1">Select from list or type and press Enter / comma</p>
    </div>
  )
}

// ── Main Component ───────────────────────────────────────────────────────────
interface CandidateProfileFormProps {
  candidate?: any
  onSuccess?: () => void
}

export default function CandidateProfileForm({ candidate, onSuccess }: CandidateProfileFormProps) {
  const [formData, setFormData] = useState({
    name: '',
    email: '',
    phone: '',
    location: 'US',
    work_auth: 'us_authorized',
    years_exp: '',
    gmail: '',
    password: '',
    linkedin_url: '',
  })
  const [techStack, setTechStack] = useState<string[]>([])
  const [errors, setErrors] = useState<Record<string, string>>({})

  // Job category: select value is either a known category, '' (none) or the
  // "Other…" sentinel that reveals a free-text input.
  const [categorySelect, setCategorySelect] = useState('')
  const [categoryOther, setCategoryOther] = useState('')

  const { data: jobCategories = [] } = useQuery({
    queryKey: ['job-categories'],
    queryFn: () => api.getJobCategories(),
    staleTime: 5 * 60 * 1000,
  })

  const categoryOptions = useMemo(() => {
    const opts = jobCategories.map((c) => c.category)
    // Keep an existing candidate value selectable even if the categories
    // endpoint doesn't list it (e.g. no jobs currently carry it).
    if (categorySelect && categorySelect !== OTHER_CATEGORY && !opts.includes(categorySelect)) {
      opts.push(categorySelect)
    }
    return opts
  }, [jobCategories, categorySelect])

  const [resumeFile, setResumeFile] = useState<File | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [status, setStatus] = useState<{ type: 'success' | 'error' | null; message: string }>({
    type: null,
    message: '',
  })
  const [dragActive, setDragActive] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  React.useEffect(() => {
    if (candidate) {
      setFormData({
        name: candidate.name || '',
        email: candidate.email || '',
        phone: candidate.phone || '',
        location: candidate.location || 'US',
        work_auth: candidate.work_auth || 'us_authorized',
        years_exp: candidate.years_exp != null ? String(candidate.years_exp) : '',
        gmail: candidate.gmail || '',
        // Never pre-fill the password: the API no longer returns it, and leaving
        // it blank means "keep the existing password". Typing a value updates it.
        password: '',
        linkedin_url: candidate.linkedin_url || '',
      })
      setTechStack(Array.isArray(candidate.tech_stack) ? candidate.tech_stack : [])
      setCategorySelect(candidate.job_category || '')
      setCategoryOther('')
    }
  }, [candidate])

  const handleChange = (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
    const { name, value } = e.target
    setFormData((prev) => ({ ...prev, [name]: value }))
    // Clear error on change
    if (errors[name]) setErrors((prev) => { const n = { ...prev }; delete n[name]; return n })
  }

  const handleBlur = (e: React.FocusEvent<HTMLInputElement>) => {
    const { name, value } = e.target
    const err = validate(name, value)
    if (err) setErrors((prev) => ({ ...prev, [name]: err }))
    else setErrors((prev) => { const n = { ...prev }; delete n[name]; return n })
  }

  const acceptFile = (file: File): boolean => {
    if (file.type !== 'application/pdf' && !file.name.endsWith('.pdf')) {
      setStatus({ type: 'error', message: 'Only PDF files are accepted for the base resume.' })
      return false
    }
    if (file.size > 10 * 1024 * 1024) {
      setStatus({ type: 'error', message: 'File must be under 10 MB.' })
      return false
    }
    return true
  }

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file && acceptFile(file)) {
      setResumeFile(file)
      setStatus({ type: null, message: '' })
    }
  }

  const handleDrag = (e: React.DragEvent) => {
    e.preventDefault(); e.stopPropagation()
    setDragActive(e.type === 'dragenter' || e.type === 'dragover')
  }

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault(); e.stopPropagation()
    setDragActive(false)
    const file = e.dataTransfer.files?.[0]
    if (file && acceptFile(file)) {
      setResumeFile(file)
      setStatus({ type: null, message: '' })
    }
  }

  const runValidations = (): boolean => {
    const newErrors: Record<string, string> = {}
    const fields = ['name', 'email', 'phone'] as const
    for (const f of fields) {
      const err = validate(f, formData[f])
      if (err) newErrors[f] = err
    }
    if (techStack.length === 0) newErrors.tech_stack = 'Add at least one technology'
    setErrors(newErrors)
    return Object.keys(newErrors).length === 0
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!runValidations()) return
    if (!candidate && !resumeFile) {
      setStatus({ type: 'error', message: 'Please upload a base resume (PDF).' })
      return
    }

    setSubmitting(true)
    setStatus({ type: null, message: '' })

    try {
      const candidatePayload: Record<string, unknown> = {
        name: formData.name.trim(),
        email: formData.email.trim(),
        phone: formData.phone.trim() || null,
        location: formData.location,
        work_auth: formData.work_auth,
        tech_stack: techStack,
        years_exp: formData.years_exp ? parseInt(formData.years_exp) : null,
        linkedin_url: formData.linkedin_url.trim() || null,
        job_category:
          (categorySelect === OTHER_CATEGORY ? categoryOther : categorySelect)
            .trim()
            .toLowerCase() || null,
        gmail: formData.gmail.trim() || null,
      }
      // Only send the password when the user actually typed one. Omitting it
      // (exclude_unset on the backend) preserves the existing stored password
      // instead of overwriting it with null on a blank edit.
      if (formData.password) {
        candidatePayload.password = formData.password
      }

      let candidateId = candidate?.id

      if (candidateId) {
        const res = await fetch(`/api/candidates/${candidateId}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(candidatePayload),
        })
        if (!res.ok) {
          const errData = await res.json().catch(() => null)
          throw new Error(errData?.detail || 'Failed to update candidate profile.')
        }
      } else {
        const res = await fetch('/api/candidates', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(candidatePayload),
        })
        if (!res.ok) {
          const errData = await res.json().catch(() => null)
          throw new Error(errData?.detail || 'Failed to create candidate profile.')
        }
        const data = await res.json()
        candidateId = data.id
      }

      if (candidateId && resumeFile) {
        const fd = new FormData()
        fd.append('file', resumeFile)
        fd.append('is_base', 'true')
        const uploadRes = await fetch(`/api/candidates/${candidateId}/resumes`, {
          method: 'POST',
          body: fd,
        })
        if (!uploadRes.ok) {
          const errData = await uploadRes.json().catch(() => null)
          throw new Error(`Profile saved, but resume upload failed: ${errData?.detail || 'Upload error'}`)
        }
      }

      setStatus({
        type: 'success',
        message: candidate
          ? 'Profile updated successfully.'
          : 'Profile created and resume uploaded. The agent will start processing shortly.',
      })

      if (!candidate) {
        setFormData({ name: '', email: '', phone: '', location: 'US', work_auth: 'us_authorized', years_exp: '', gmail: '', password: '', linkedin_url: '' })
        setTechStack([])
        setCategorySelect('')
        setCategoryOther('')
        setResumeFile(null)
        if (fileInputRef.current) fileInputRef.current.value = ''
      }
      onSuccess?.()
    } catch (err: any) {
      setStatus({ type: 'error', message: err.message || 'An unexpected error occurred.' })
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="max-w-2xl">
      <div className="mb-6">
        <h2 className="text-base font-semibold text-text-primary">
          {candidate ? 'Edit Profile Details' : 'Onboard Candidate'}
        </h2>
        <p className="text-sm text-text-muted mt-1">
          {candidate
            ? 'Update the candidate details. You can optionally upload a new base resume.'
            : 'Add candidate details and a base resume. The agent will score jobs, tailor resumes, and apply automatically.'}
        </p>
      </div>

      {status.type && (
        <div
          className={`mb-5 p-3 rounded-md border text-sm flex items-start gap-2.5 ${
            status.type === 'success'
              ? 'bg-success/8 border-success/20 text-success'
              : 'bg-danger/8 border-danger/20 text-danger'
          }`}
        >
          {status.type === 'success'
            ? <CheckCircle className="w-4 h-4 mt-0.5 shrink-0" />
            : <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />}
          {status.message}
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-5" noValidate>
        {/* Name + Email */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label className="input-label">Full Name <span className="text-danger">*</span></label>
            <input
              type="text" name="name" required
              value={formData.name} onChange={handleChange} onBlur={handleBlur}
              placeholder="Jane Smith"
              className={`input ${errors.name ? 'border-danger focus:border-danger' : ''}`}
            />
            {errors.name && <p className="text-danger text-xs mt-1">{errors.name}</p>}
          </div>
          <div>
            <label className="input-label">Email <span className="text-danger">*</span></label>
            <input
              type="email" name="email" required
              value={formData.email} onChange={handleChange} onBlur={handleBlur}
              placeholder="jane@example.com"
              className={`input ${errors.email ? 'border-danger focus:border-danger' : ''}`}
            />
            {errors.email && <p className="text-danger text-xs mt-1">{errors.email}</p>}
          </div>
        </div>

        {/* Gmail + Password */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label className="input-label">Gmail (for AI Login)</label>
            <input
              type="email" name="gmail"
              value={formData.gmail} onChange={handleChange} onBlur={handleBlur}
              placeholder="candidate@gmail.com"
              className="input"
            />
          </div>
          <div>
            <label className="input-label">Password</label>
            <input
              type="password" name="password"
              value={formData.password} onChange={handleChange}
              placeholder="••••••••"
              className="input"
            />
          </div>
        </div>

        {/* Phone */}
        <div>
          <label className="input-label">Phone</label>
          <input
            type="tel" name="phone"
            value={formData.phone} onChange={handleChange} onBlur={handleBlur}
            placeholder="+1 (555) 000-0000"
            className={`input ${errors.phone ? 'border-danger focus:border-danger' : ''}`}
          />
          {errors.phone && <p className="text-danger text-xs mt-1">{errors.phone}</p>}
        </div>

        {/* Years exp + Location */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label className="input-label">Years of Experience</label>
            <input
              type="number" name="years_exp" value={formData.years_exp} onChange={handleChange}
              placeholder="5" min="0" max="50" className="input"
            />
          </div>
          <div>
            <label className="input-label">Target Location</label>
            <input
              type="text" name="location" value={formData.location} onChange={handleChange}
              placeholder="US, Remote, London…" className="input"
            />
          </div>
        </div>

        {/* Work auth */}
        <div>
          <label className="input-label">Work Authorization</label>
          <select name="work_auth" value={formData.work_auth} onChange={handleChange} className="input appearance-none">
            <option value="us_authorized">US Authorized (Citizen / GC / EAD)</option>
            <option value="visa_required">Requires Visa Sponsorship</option>
            <option value="remote_global">Remote Global</option>
          </select>
        </div>

        {/* Job category + LinkedIn */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label className="input-label">Job Category</label>
            <select
              value={categorySelect}
              onChange={(e) => setCategorySelect(e.target.value)}
              className="input appearance-none"
            >
              <option value="">— Not set —</option>
              {categoryOptions.map((cat) => (
                <option key={cat} value={cat}>{cat}</option>
              ))}
              <option value={OTHER_CATEGORY}>Other…</option>
            </select>
            {categorySelect === OTHER_CATEGORY && (
              <input
                type="text"
                value={categoryOther}
                onChange={(e) => setCategoryOther(e.target.value)}
                placeholder="e.g. devops"
                className="input mt-2"
              />
            )}
            <p className="text-text-muted text-xs mt-1">Scopes job matching to this stack.</p>
          </div>
          <div>
            <label className="input-label">LinkedIn URL</label>
            <input
              type="url" name="linkedin_url"
              value={formData.linkedin_url} onChange={handleChange}
              placeholder="https://linkedin.com/in/…" className="input"
            />
          </div>
        </div>

        {/* Tech Stack */}
        <div>
          <label className="input-label">Tech Stack <span className="text-danger">*</span></label>
          <TechStackInput selected={techStack} onChange={setTechStack} />
          {errors.tech_stack && <p className="text-danger text-xs mt-1">{errors.tech_stack}</p>}
        </div>

        {/* Current base resume (edit mode only) */}
        {candidate?.base_resume_url && !resumeFile && (
          <div className="flex items-center gap-2 p-3 rounded-md bg-bg-secondary border border-bg-border text-sm">
            <FileText className="w-4 h-4 text-text-muted shrink-0" />
            <span className="text-text-secondary text-xs flex-1 truncate">Current Base Resume</span>
            <a
              href={candidate.base_resume_url}
              target="_blank"
              rel="noreferrer"
              className="text-accent hover:underline text-xs flex items-center gap-1"
            >
              View <ExternalLink className="w-3 h-3" />
            </a>
          </div>
        )}

        {/* Resume upload */}
        <div>
          <label className="input-label">
            Base Resume (PDF){candidate ? ' — Optional' : <span className="text-danger"> *</span>}
          </label>
          <div
            onDragEnter={handleDrag} onDragOver={handleDrag}
            onDragLeave={handleDrag} onDrop={handleDrop}
            onClick={() => fileInputRef.current?.click()}
            className={`border rounded-md p-6 text-center cursor-pointer transition-colors duration-150 ${
              dragActive
                ? 'border-accent bg-accent/5'
                : resumeFile
                ? 'border-success/30 bg-success/5'
                : 'border-bg-border bg-bg-secondary hover:border-[#3d3d50]'
            }`}
          >
            <input
              ref={fileInputRef}
              type="file"
              accept=".pdf,application/pdf"
              onChange={handleFileChange}
              className="hidden"
            />
            {resumeFile ? (
              <div className="flex flex-col items-center gap-1.5">
                <FileText className="w-7 h-7 text-success" />
                <p className="text-sm font-medium text-text-primary">{resumeFile.name}</p>
                <p className="text-xs text-text-muted">{(resumeFile.size / 1024 / 1024).toFixed(2)} MB · click to replace</p>
              </div>
            ) : (
              <div className="flex flex-col items-center gap-1.5">
                <Upload className="w-7 h-7 text-text-muted" />
                <p className="text-sm text-text-secondary">
                  Drop your resume here or <span className="text-accent">browse</span>
                </p>
                <p className="text-xs text-text-muted">PDF only · up to 10 MB</p>
              </div>
            )}
          </div>
        </div>

        <button type="submit" disabled={submitting} className="btn-primary">
          {submitting ? (
            <><Loader2 className="w-4 h-4 animate-spin" />Saving…</>
          ) : candidate ? (
            'Update Profile'
          ) : (
            'Save & Onboard Candidate'
          )}
        </button>
      </form>
    </div>
  )
}
