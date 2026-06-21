'use client'

import React, { useState, useRef } from 'react'
import { supabase } from '@/lib/supabase'
import { Upload, FileText, CheckCircle, AlertTriangle, Loader2 } from 'lucide-react'

interface CandidateProfileFormProps {
  onSuccess?: () => void
}

export default function CandidateProfileForm({ onSuccess }: CandidateProfileFormProps) {
  const [formData, setFormData] = useState({
    name: '',
    email: '',
    phone: '',
    location: 'US',
    work_auth: 'us_authorized',
    tech_stack: '',
    years_exp: '',
  })

  const [resumeFile, setResumeFile] = useState<File | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [status, setStatus] = useState<{ type: 'success' | 'error' | null; message: string }>({
    type: null,
    message: '',
  })
  const [dragActive, setDragActive] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const handleChange = (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
    const { name, value } = e.target
    setFormData((prev) => ({ ...prev, [name]: value }))
  }

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files?.[0]) setResumeFile(e.target.files[0])
  }

  const handleDrag = (e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setDragActive(e.type === 'dragenter' || e.type === 'dragover')
  }

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setDragActive(false)
    if (e.dataTransfer.files?.[0]) setResumeFile(e.dataTransfer.files[0])
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!resumeFile) {
      setStatus({ type: 'error', message: 'Please upload a base resume.' })
      return
    }

    setSubmitting(true)
    setStatus({ type: null, message: '' })

    try {
      const candidatePayload = {
        name: formData.name,
        email: formData.email,
        phone: formData.phone || null,
        location: formData.location,
        work_auth: formData.work_auth,
        tech_stack: formData.tech_stack.split(',').map((s) => s.trim()).filter(Boolean),
        years_exp: formData.years_exp ? parseInt(formData.years_exp) : null,
        linkedin_url: null,
      }

      const res = await fetch('/api/candidates', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(candidatePayload),
      })

      if (!res.ok) throw new Error((await res.text()) || 'Failed to create candidate profile.')

      const { id: candidateId } = await res.json()

      if (candidateId) {
        const filePath = `${candidateId}/${Date.now()}_${resumeFile.name}`
        const { error } = await supabase.storage.from('resume').upload(filePath, resumeFile)
        if (error) throw new Error(`Profile created, but resume upload failed: ${error.message}`)
      }

      setStatus({ type: 'success', message: 'Profile created and resume uploaded. The agent will start processing shortly.' })

      setFormData({ name: '', email: '', phone: '', location: 'US', work_auth: 'us_authorized', tech_stack: '', years_exp: '' })
      setResumeFile(null)
      if (fileInputRef.current) fileInputRef.current.value = ''
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
        <h2 className="text-base font-semibold text-text-primary">Onboard Candidate</h2>
        <p className="text-sm text-text-muted mt-1">
          Add candidate details and a base resume. The agent will score jobs, tailor resumes, and apply automatically.
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

      <form onSubmit={handleSubmit} className="space-y-5">
        {/* Name + Email */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label className="input-label">Full Name <span className="text-danger">*</span></label>
            <input type="text" name="name" required value={formData.name} onChange={handleChange}
              placeholder="Jane Smith" className="input" />
          </div>
          <div>
            <label className="input-label">Email <span className="text-danger">*</span></label>
            <input type="email" name="email" required value={formData.email} onChange={handleChange}
              placeholder="jane@example.com" className="input" />
          </div>
        </div>

        {/* Phone */}
        <div>
          <label className="input-label">Phone</label>
          <input type="tel" name="phone" value={formData.phone} onChange={handleChange}
            placeholder="+1 (555) 000-0000" className="input" />
        </div>

        {/* Years exp + Location */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label className="input-label">Years of Experience</label>
            <input type="number" name="years_exp" value={formData.years_exp} onChange={handleChange}
              placeholder="5" min="0" className="input" />
          </div>
          <div>
            <label className="input-label">Target Location</label>
            <input type="text" name="location" value={formData.location} onChange={handleChange}
              placeholder="US, Remote, London…" className="input" />
          </div>
        </div>

        {/* Work auth + Tech stack */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label className="input-label">Work Authorization</label>
            <select name="work_auth" value={formData.work_auth} onChange={handleChange} className="input appearance-none">
              <option value="us_authorized">US Authorized (Citizen / GC / EAD)</option>
              <option value="visa_required">Requires Visa Sponsorship</option>
              <option value="remote_global">Remote Global</option>
            </select>
          </div>
          <div>
            <label className="input-label">Tech Stack <span className="text-danger">*</span></label>
            <input type="text" name="tech_stack" required value={formData.tech_stack} onChange={handleChange}
              placeholder="Python, React, FastAPI…" className="input" />
          </div>
        </div>

        {/* Resume upload */}
        <div>
          <label className="input-label">Base Resume <span className="text-danger">*</span></label>
          <div
            onDragEnter={handleDrag}
            onDragOver={handleDrag}
            onDragLeave={handleDrag}
            onDrop={handleDrop}
            onClick={() => fileInputRef.current?.click()}
            className={`border rounded-md p-6 text-center cursor-pointer transition-colors duration-150 ${
              dragActive
                ? 'border-accent bg-accent/5'
                : resumeFile
                ? 'border-success/30 bg-success/5'
                : 'border-bg-border bg-bg-secondary hover:border-[#3d3d50]'
            }`}
          >
            <input ref={fileInputRef} type="file" accept=".pdf,.doc,.docx" onChange={handleFileChange} className="hidden" />
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
                <p className="text-xs text-text-muted">PDF, DOC, DOCX up to 10 MB</p>
              </div>
            )}
          </div>
        </div>

        <button type="submit" disabled={submitting} className="btn-primary">
          {submitting ? (
            <>
              <Loader2 className="w-4 h-4 animate-spin" />
              Saving…
            </>
          ) : (
            'Save & Onboard Candidate'
          )}
        </button>
      </form>
    </div>
  )
}
