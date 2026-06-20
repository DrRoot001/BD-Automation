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
    linkedin_url: '',
  })
  
  const [resumeFile, setResumeFile] = useState<File | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [status, setStatus] = useState<{ type: 'success' | 'error' | null; message: string }>({
    type: null,
    message: '',
  })

  const fileInputRef = useRef<HTMLInputElement>(null)

  const handleChange = (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
    const { name, value } = e.target
    setFormData((prev) => ({ ...prev, [name]: value }))
  }

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      setResumeFile(e.target.files[0])
    }
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
      // 1. Submit candidate metadata
      const candidatePayload = {
        name: formData.name,
        email: formData.email,
        phone: formData.phone || null,
        location: formData.location,
        work_auth: formData.work_auth,
        tech_stack: formData.tech_stack.split(',').map(item => item.trim()).filter(Boolean),
        years_exp: formData.years_exp ? parseInt(formData.years_exp) : null,
        linkedin_url: formData.linkedin_url || null,
      }

      const res = await fetch('/api/candidates', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(candidatePayload)
      })

      if (!res.ok) {
        const errText = await res.text()
        throw new Error(errText || 'Failed to create candidate profile.')
      }

      const candidateData = await res.json()
      const candidateId = candidateData.id

      // 2. Upload base resume to Supabase bucket
      if (resumeFile && candidateId) {
        const filePath = `${candidateId}/${Date.now()}_${resumeFile.name}`
        const { data, error } = await supabase.storage
          .from('resume')
          .upload(filePath, resumeFile)

        if (error) {
          throw new Error(`Candidate profile created, but resume upload failed: ${error.message}`)
        }
        
        // Note: the backend needs to store the resume URL/path and we should ideally call an endpoint here 
        // to associate the uploaded resume with the candidate profile, e.g.
        // PATCH /api/candidates/${candidateId} { resume_path: filePath }
      }

      setStatus({
        type: 'success',
        message: 'Candidate profile and base resume uploaded successfully! The automation agent will start processing jobs shortly.',
      })

      // Reset form
      setFormData({
        name: '',
        email: '',
        phone: '',
        location: 'US',
        work_auth: 'us_authorized',
        tech_stack: '',
        years_exp: '',
        linkedin_url: '',
      })
      setResumeFile(null)
      if (fileInputRef.current) fileInputRef.current.value = ''
      
      if (onSuccess) {
        onSuccess()
      }
    } catch (err: any) {
      setStatus({
        type: 'error',
        message: err.message || 'An error occurred during onboarding.',
      })
    } finally {
      setSubmitting(false)
    }
  }

  // drag and drop states
  const [dragActive, setDragActive] = useState(false)
  
  const handleDrag = (e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    if (e.type === "dragenter" || e.type === "dragover") {
      setDragActive(true)
    } else if (e.type === "dragleave") {
      setDragActive(false)
    }
  }

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setDragActive(false)
    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      setResumeFile(e.dataTransfer.files[0])
    }
  }

  return (
    <div className="card-glow max-w-4xl mx-auto my-4 p-8 bg-bg-card border border-bg-border rounded-xl">
      <div className="mb-6">
        <h2 className="text-2xl font-bold text-text-primary">Onboard Candidate Profile</h2>
        <p className="text-text-muted text-sm mt-1">
          Provide candidate details and upload a base resume. The BD Automator agent will automatically score matching jobs, tailor resumes, write cover letters, and auto-apply.
        </p>
      </div>

      {status.type && (
        <div
          className={`mb-6 p-4 rounded-lg border text-sm font-medium flex items-center gap-2 ${
            status.type === 'success'
              ? 'bg-success/10 border-success/20 text-success'
              : 'bg-danger/10 border-danger/20 text-danger'
          }`}
        >
          {status.type === 'success' ? <CheckCircle className="w-5 h-5" /> : <AlertTriangle className="w-5 h-5" />} 
          {status.message}
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-6">
        {/* Personal Details */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <div className="flex flex-col gap-2">
            <label className="text-sm font-semibold text-text-secondary">Full Name *</label>
            <input
              type="text"
              name="name"
              required
              value={formData.name}
              onChange={handleChange}
              placeholder="e.g. John Doe"
              className="bg-bg-primary border border-bg-border rounded-lg px-4 py-2.5 text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent transition-colors"
            />
          </div>
          <div className="flex flex-col gap-2">
            <label className="text-sm font-semibold text-text-secondary">Email Address *</label>
            <input
              type="email"
              name="email"
              required
              value={formData.email}
              onChange={handleChange}
              placeholder="e.g. john.doe@example.com"
              className="bg-bg-primary border border-bg-border rounded-lg px-4 py-2.5 text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent transition-colors"
            />
          </div>
        </div>

        {/* Contact Details */}
        <div className="flex flex-col gap-2">
          <label className="text-sm font-semibold text-text-secondary">Phone Number</label>
          <input
            type="tel"
            name="phone"
            value={formData.phone}
            onChange={handleChange}
            placeholder="e.g. +1 (555) 123-4567"
            className="bg-bg-primary border border-bg-border rounded-lg px-4 py-2.5 text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent transition-colors"
          />
        </div>

        {/* Experience & Tech Stack */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <div className="flex flex-col gap-2">
            <label className="text-sm font-semibold text-text-secondary">Years of Experience</label>
            <input
              type="number"
              name="years_exp"
              value={formData.years_exp}
              onChange={handleChange}
              placeholder="e.g. 5"
              min="0"
              className="bg-bg-primary border border-bg-border rounded-lg px-4 py-2.5 text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent transition-colors"
            />
          </div>
          <div className="flex flex-col gap-2">
            <label className="text-sm font-semibold text-text-secondary">Target Location / Country</label>
            <input
              type="text"
              name="location"
              value={formData.location}
              onChange={handleChange}
              placeholder="e.g. US, Remote, London"
              className="bg-bg-primary border border-bg-border rounded-lg px-4 py-2.5 text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent transition-colors"
            />
          </div>
        </div>

        {/* Work Auth & Tech Stack Textarea */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <div className="flex flex-col gap-2">
            <label className="text-sm font-semibold text-text-secondary">Work Authorization</label>
            <select
              name="work_auth"
              value={formData.work_auth}
              onChange={handleChange}
              className="bg-bg-primary border border-bg-border rounded-lg px-4 py-2.5 text-text-primary focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent transition-colors appearance-none"
            >
              <option value="us_authorized">US Authorized (Citizen/GC/EAD)</option>
              <option value="visa_required">Requires Visa Sponsorship</option>
              <option value="remote_global">Remote Global</option>
            </select>
          </div>
          <div className="flex flex-col gap-2">
            <label className="text-sm font-semibold text-text-secondary">Tech Stack (comma separated) *</label>
            <input
              type="text"
              name="tech_stack"
              required
              value={formData.tech_stack}
              onChange={handleChange}
              placeholder="e.g. Python, React, Next.js, FastAPI, PostgreSQL"
              className="bg-bg-primary border border-bg-border rounded-lg px-4 py-2.5 text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent transition-colors"
            />
          </div>
        </div>

        {/* Drag and Drop Resume Upload */}
        <div className="flex flex-col gap-2">
          <label className="text-sm font-semibold text-text-secondary">Base Resume (PDF/Word) *</label>
          <div
            onDragEnter={handleDrag}
            onDragOver={handleDrag}
            onDragLeave={handleDrag}
            onDrop={handleDrop}
            className={`border-2 border-dashed rounded-xl p-8 text-center cursor-pointer transition-all duration-200 ${
              dragActive
                ? 'border-accent bg-accent/5'
                : resumeFile
                ? 'border-success/40 bg-success/5'
                : 'border-bg-border bg-bg-primary/50 hover:bg-bg-primary'
            }`}
            onClick={() => fileInputRef.current?.click()}
          >
            <input
              ref={fileInputRef}
              type="file"
              accept=".pdf,.doc,.docx"
              onChange={handleFileChange}
              className="hidden"
            />
            {resumeFile ? (
              <div className="flex flex-col items-center justify-center gap-2">
                <FileText className="w-10 h-10 text-accent mb-2" />
                <p className="text-sm font-semibold text-text-primary">{resumeFile.name}</p>
                <p className="text-xs text-text-muted">
                  {(resumeFile.size / 1024 / 1024).toFixed(2)} MB · Click or drag to replace
                </p>
              </div>
            ) : (
              <div className="flex flex-col items-center justify-center gap-2">
                <Upload className="w-10 h-10 text-text-muted mb-2" />
                <p className="text-sm font-medium text-text-secondary">
                  Drag and drop your resume here, or <span className="text-accent hover:underline">browse</span>
                </p>
                <p className="text-xs text-text-muted">Supports PDF, DOC, DOCX up to 10MB</p>
              </div>
            )}
          </div>
        </div>

        {/* Submit button */}
        <div className="pt-4">
          <button
            type="submit"
            disabled={submitting}
            className="btn-primary w-full md:w-auto flex items-center justify-center gap-2"
          >
            {submitting ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin" />
                Processing Profile...
              </>
            ) : (
              'Save & Onboard Candidate'
            )}
          </button>
        </div>
      </form>
    </div>
  )
}
