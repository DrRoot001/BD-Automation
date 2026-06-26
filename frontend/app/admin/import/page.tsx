'use client'

import { useState, useRef, ChangeEvent } from 'react'
import { 
  UploadCloud, 
  CheckCircle2, 
  AlertCircle, 
  Loader2, 
  FileText, 
  Check, 
  AlertTriangle, 
  Trash2, 
  Info, 
  ArrowRight,
  FileCode
} from 'lucide-react'

interface ParsedJob {
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
  pay_period: string | null
  job_type: string | null
  posted_at: string | null
  isValid: boolean
  errors: string[]
}

export default function JobImportPage() {
  const [activeTab, setActiveTab] = useState<'json' | 'csv'>('json')
  const [jsonInput, setJsonInput] = useState('')
  const [csvInput, setCsvInput] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [result, setResult] = useState<{ success: boolean; message: string; count?: number; errorCount?: number } | null>(null)
  
  // File upload state
  const [selectedFile, setSelectedFile] = useState<File | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [dragActive, setDragActive] = useState(false)

  // CSV Parser implementation
  const parseCSV = (text: string): Record<string, string>[] => {
    const lines: string[][] = []
    let row: string[] = []
    let inQuotes = false
    let currentField = ''

    for (let i = 0; i < text.length; i++) {
      const char = text[i]
      const nextChar = text[i + 1]

      if (char === '"') {
        if (inQuotes && nextChar === '"') {
          currentField += '"'
          i++ // skip LF
        } else {
          inQuotes = !inQuotes
        }
      } else if (char === ',' && !inQuotes) {
        row.push(currentField.trim())
        currentField = ''
      } else if ((char === '\n' || char === '\r') && !inQuotes) {
        if (char === '\r' && nextChar === '\n') {
          i++
        }
        row.push(currentField.trim())
        if (row.length > 1 || row[0] !== '') {
          lines.push(row)
        }
        row = []
        currentField = ''
      } else {
        currentField += char
      }
    }
    
    if (row.length > 0 || currentField !== '') {
      row.push(currentField.trim())
      lines.push(row)
    }

    if (lines.length < 2) return []

    // Normalize headers: strip quotes, make lowercase, trim
    const headers = lines[0].map(h => h.toLowerCase().replace(/['"’“”]/g, '').trim())
    const results: Record<string, string>[] = []

    for (let i = 1; i < lines.length; i++) {
      const values = lines[i]
      if (values.length === 0 || (values.length === 1 && values[0] === '')) continue
      
      const item: Record<string, string> = {}
      headers.forEach((header, index) => {
        item[header] = values[index] || ''
      })
      results.push(item)
    }

    return results
  }

  // Normalize CSV headers to Match expected Schema
  const mapCSVRowToJob = (row: Record<string, string>) => {
    const getValue = (keys: string[]) => {
      for (const key of keys) {
        if (row[key] !== undefined && row[key] !== '') return row[key]
      }
      return undefined
    }

    const title = getValue(['title', 'job title', 'job_title', 'position', 'role']) || ''
    const company = getValue(['company', 'company name', 'company_name', 'organization']) || ''
    const location = getValue(['location', 'job location', 'job_location', 'city', 'country']) || null
    const source = getValue(['source', 'origin']) || 'csv_import'
    const source_url = getValue(['source_url', 'source url', 'url', 'link', 'job url', 'job_url']) || ''
    const canonical_url = getValue(['canonical_url', 'canonical url', 'canonical_url_link']) || null
    const description = getValue(['description', 'job description', 'job_description', 'summary', 'details', 'body']) || null
    
    // skills parsing
    const skillsVal = getValue(['skills', 'key skills', 'requirements', 'technologies', 'tags'])
    let skills: string[] = []
    if (skillsVal) {
      if (skillsVal.startsWith('[') && skillsVal.endsWith(']')) {
        try {
          skills = JSON.parse(skillsVal)
        } catch (e) {
          skills = skillsVal.split(',').map(s => s.trim()).filter(Boolean)
        }
      } else {
        skills = skillsVal.split(',').map(s => s.trim()).filter(Boolean)
      }
    }

    // numeric parsing
    const parseMin = getValue(['salary_min', 'salary min', 'min_salary', 'salary_minimum', 'min salary'])
    const salary_min = parseMin ? parseInt(parseMin.replace(/[^0-9]/g, ''), 10) || null : null

    const parseMax = getValue(['salary_max', 'salary max', 'max_salary', 'salary_maximum', 'max salary'])
    const salary_max = parseMax ? parseInt(parseMax.replace(/[^0-9]/g, ''), 10) || null : null

    const pay_period = getValue(['pay_period', 'pay period', 'payment_period', 'frequency']) || null
    const job_type = getValue(['job_type', 'job type', 'type']) || null
    
    const posted_at_str = getValue(['posted_at', 'posted at', 'date', 'posted'])
    let posted_at = null
    if (posted_at_str) {
      try {
        const date = new Date(posted_at_str)
        if (!isNaN(date.getTime())) {
          posted_at = date.toISOString()
        }
      } catch (e) {}
    }

    return {
      title,
      company,
      location,
      source,
      source_url,
      canonical_url,
      description,
      skills,
      salary_min,
      salary_max,
      pay_period,
      job_type,
      posted_at,
    }
  }

  // Get parsed jobs list with validation status
  const getParsedJobs = (): ParsedJob[] => {
    const jobsList: any[] = []
    
    if (activeTab === 'json') {
      if (!jsonInput.trim()) return []
      try {
        const parsed = JSON.parse(jsonInput)
        if (Array.isArray(parsed)) {
          jobsList.push(...parsed)
        } else if (typeof parsed === 'object' && parsed !== null) {
          jobsList.push(parsed)
        }
      } catch (e) {
        return [] // invalid JSON
      }
    } else {
      if (!csvInput.trim()) return []
      try {
        const rows = parseCSV(csvInput)
        const mapped = rows.map(mapCSVRowToJob)
        jobsList.push(...mapped)
      } catch (e) {
        return []
      }
    }

    return jobsList.map(job => {
      const errors: string[] = []
      
      if (!job.title || typeof job.title !== 'string' || !job.title.trim()) {
        errors.push("Title is required")
      }
      if (!job.company || typeof job.company !== 'string' || !job.company.trim()) {
        errors.push("Company is required")
      }
      if (!job.source || typeof job.source !== 'string' || !job.source.trim()) {
        errors.push("Source is required")
      }
      if (!job.source_url || typeof job.source_url !== 'string' || !job.source_url.trim()) {
        errors.push("Source URL is required")
      }

      return {
        title: job.title || '',
        company: job.company || '',
        location: job.location || null,
        source: job.source || '',
        source_url: job.source_url || '',
        canonical_url: job.canonical_url || null,
        description: job.description || null,
        skills: Array.isArray(job.skills) ? job.skills : [],
        salary_min: job.salary_min || null,
        salary_max: job.salary_max || null,
        pay_period: job.pay_period || null,
        job_type: job.job_type || null,
        posted_at: job.posted_at || null,
        isValid: errors.length === 0,
        errors
      }
    })
  }

  const parsedJobs = getParsedJobs()
  const validJobs = parsedJobs.filter(j => j.isValid)
  const invalidJobsCount = parsedJobs.length - validJobs.length

  const handleDrag = (e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    if (e.type === "dragenter" || e.type === "dragover") {
      setDragActive(true)
    } else if (e.type === "dragleave") {
      setDragActive(false)
    }
  }

  const processFile = (file: File) => {
    const isJson = file.name.endsWith('.json')
    const isCsv = file.name.endsWith('.csv')

    if (!isJson && !isCsv) {
      alert("Invalid file type. Please upload a .json or .csv file.")
      return
    }

    setSelectedFile(file)
    setActiveTab(isJson ? 'json' : 'csv')

    const reader = new FileReader()
    reader.onload = (e) => {
      const text = e.target?.result as string
      if (isJson) {
        setJsonInput(text)
        setCsvInput('')
      } else {
        setCsvInput(text)
        setJsonInput('')
      }
      setResult(null)
    }
    reader.readAsText(file)
  }

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setDragActive(false)

    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      processFile(e.dataTransfer.files[0])
    }
  }

  const handleFileChange = (e: ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      processFile(e.target.files[0])
    }
  }

  const clearFile = () => {
    setSelectedFile(null)
    setJsonInput('')
    setCsvInput('')
    setResult(null)
    if (fileInputRef.current) {
      fileInputRef.current.value = ''
    }
  }

  const handleImport = async () => {
    if (validJobs.length === 0) return
    setIsLoading(true)
    setResult(null)
    
    try {
      // Strip parsing metadata fields (isValid, errors)
      const cleanData = validJobs.map(({ isValid, errors, ...rest }) => rest)
      
      const res = await fetch('/api/jobs', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(cleanData)
      })
      
      if (!res.ok) {
        const errText = await res.text()
        throw new Error(`API Error: ${res.status} - ${errText}`)
      }
      
      const createdJobs = await res.json()
      
      setResult({
        success: true,
        message: `Successfully imported jobs!`,
        count: createdJobs.length,
        errorCount: invalidJobsCount
      })
      
      // Reset inputs on success
      setJsonInput('')
      setCsvInput('')
      setSelectedFile(null)
      if (fileInputRef.current) {
        fileInputRef.current.value = ''
      }
      
    } catch (err: any) {
      setResult({
        success: false,
        message: err.message || "Failed to import data to database."
      })
    } finally {
      setIsLoading(false)
    }
  }

  // Template generators
  const downloadJSONTemplate = () => {
    const template = [
      {
        "title": "Software Engineer",
        "company": "Google",
        "location": "Mountain View, CA",
        "source": "careers",
        "source_url": "https://careers.google.com/jobs/123",
        "canonical_url": null,
        "description": "Develop next-gen applications.",
        "skills": ["Python", "Go", "Docker"],
        "salary_min": 120000,
        "salary_max": 180000,
        "pay_period": "yearly",
        "job_type": "full-time",
        "posted_at": new Date().toISOString()
      }
    ]
    const blob = new Blob([JSON.stringify(template, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'job_import_template.json'
    a.click()
    URL.revokeObjectURL(url)
  }

  const downloadCSVTemplate = () => {
    const csvContent = 'title,company,location,source,source_url,description,skills,salary_min,salary_max,pay_period,job_type\n"Frontend Engineer","Vercel","Remote","careers","https://vercel.com/careers/456","Build the future of web deployment","React,Next.js,TypeScript",110000,160000,"yearly","full-time"'
    const blob = new Blob([csvContent], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'job_import_template.csv'
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="space-y-6 max-w-5xl mx-auto animate-in fade-in duration-500 pb-16">
      
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-text-primary tracking-tight">Job Import Dashboard</h1>
          <p className="text-sm text-text-muted mt-1">Upload files or paste job datasets directly using CSV or JSON formats.</p>
        </div>
        
        <div className="flex gap-2">
          <button 
            onClick={downloadJSONTemplate}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-bg-secondary border border-bg-border hover:bg-bg-hover text-text-secondary hover:text-text-primary rounded-lg text-xs font-medium transition-all"
          >
            <FileCode className="w-3.5 h-3.5" />
            JSON Template
          </button>
          <button 
            onClick={downloadCSVTemplate}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-bg-secondary border border-bg-border hover:bg-bg-hover text-text-secondary hover:text-text-primary rounded-lg text-xs font-medium transition-all"
          >
            <FileText className="w-3.5 h-3.5" />
            CSV Template
          </button>
        </div>
      </div>

      {result && (
        <div className={`p-4 rounded-xl border flex items-start gap-3 shadow-sm transition-all ${result.success ? 'bg-green-500/10 border-green-500/20 text-green-600' : 'bg-danger/10 border-danger/20 text-danger'}`}>
          {result.success ? <CheckCircle2 className="w-5 h-5 shrink-0 text-green-500" /> : <AlertCircle className="w-5 h-5 shrink-0" />}
          <div>
            <h3 className="text-sm font-semibold">{result.success ? 'Import Complete' : 'Import Failed'}</h3>
            <p className="text-xs mt-0.5 opacity-90">{result.message}</p>
            {result.success && result.count !== undefined && (
              <div className="mt-2 text-xs font-medium flex flex-wrap gap-x-4 gap-y-1">
                <span className="text-green-700">✓ {result.count} jobs added successfully.</span>
                {result.errorCount && result.errorCount > 0 ? (
                  <span className="text-amber-700">⚠ {result.errorCount} invalid jobs were skipped.</span>
                ) : null}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Main Grid: Upload options on left/top, info on right */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        
        {/* Upload Column (Col-Span-2) */}
        <div className="lg:col-span-2 space-y-6">
          
          {/* Uploader Card */}
          <div className="bg-bg-card border border-bg-border rounded-2xl p-6 shadow-sm">
            <h2 className="text-sm font-semibold text-text-primary mb-4 flex items-center gap-2">
              <UploadCloud className="w-4 h-4 text-text-muted" />
              1. Load Job File
            </h2>
            
            {/* Drag & Drop Area */}
            <div 
              onDragEnter={handleDrag}
              onDragOver={handleDrag}
              onDragLeave={handleDrag}
              onDrop={handleDrop}
              onClick={() => fileInputRef.current?.click()}
              className={`border-2 border-dashed rounded-xl p-8 text-center cursor-pointer transition-all ${
                dragActive 
                  ? 'border-accent bg-bg-hover scale-[0.99]' 
                  : 'border-bg-border hover:border-text-muted hover:bg-bg-primary/50'
              } ${selectedFile ? 'bg-bg-primary/80 border-green-500/40' : ''}`}
            >
              <input 
                type="file"
                ref={fileInputRef}
                onChange={handleFileChange}
                accept=".csv,.json"
                className="hidden"
              />
              
              <div className="flex flex-col items-center justify-center space-y-2">
                <div className={`p-3 rounded-full ${selectedFile ? 'bg-green-500/10 text-green-500' : 'bg-bg-secondary text-text-muted'}`}>
                  <UploadCloud className="w-6 h-6 animate-pulse" />
                </div>
                
                {selectedFile ? (
                  <div>
                    <p className="text-sm font-medium text-text-primary">{selectedFile.name}</p>
                    <p className="text-xs text-text-muted mt-0.5">{(selectedFile.size / 1024).toFixed(1)} KB • Click or drop a different file</p>
                  </div>
                ) : (
                  <div>
                    <p className="text-sm font-medium text-text-primary">Drag & drop CSV or JSON file here</p>
                    <p className="text-xs text-text-muted mt-0.5">or click to browse your local filesystem</p>
                  </div>
                )}
              </div>
            </div>

            {selectedFile && (
              <div className="mt-3 flex justify-end">
                <button
                  onClick={clearFile}
                  className="flex items-center gap-1.5 px-3 py-1.5 border border-danger/20 hover:border-danger/40 bg-danger/5 hover:bg-danger/10 text-danger rounded-lg text-xs font-medium transition-colors"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                  Remove Selected File
                </button>
              </div>
            )}
          </div>

          {/* Paste Text Card */}
          <div className="bg-bg-card border border-bg-border rounded-2xl p-6 shadow-sm">
            <div className="flex items-center justify-between border-b border-bg-border pb-3 mb-4">
              <h2 className="text-sm font-semibold text-text-primary flex items-center gap-2">
                <FileCode className="w-4 h-4 text-text-muted" />
                2. Or Paste Dataset Manual
              </h2>
              
              {/* Custom styled Tabs */}
              <div className="flex bg-bg-secondary p-0.5 rounded-lg border border-bg-border">
                <button
                  onClick={() => { setActiveTab('json'); setSelectedFile(null); }}
                  className={`px-3 py-1 text-xs font-semibold rounded-md transition-all ${activeTab === 'json' ? 'bg-bg-card text-text-primary shadow-sm' : 'text-text-muted hover:text-text-primary'}`}
                >
                  JSON Format
                </button>
                <button
                  onClick={() => { setActiveTab('csv'); setSelectedFile(null); }}
                  className={`px-3 py-1 text-xs font-semibold rounded-md transition-all ${activeTab === 'csv' ? 'bg-bg-card text-text-primary shadow-sm' : 'text-text-muted hover:text-text-primary'}`}
                >
                  CSV Format
                </button>
              </div>
            </div>

            <div>
              {activeTab === 'json' ? (
                <div>
                  <p className="text-xs text-text-muted mb-2">
                    Input a raw JSON list. Must include fields: <code>title</code>, <code>company</code>, <code>source</code>, and <code>source_url</code>.
                  </p>
                  <textarea
                    value={jsonInput}
                    onChange={(e) => { setJsonInput(e.target.value); setSelectedFile(null); }}
                    className="w-full h-80 px-4 py-3 bg-bg-primary border border-bg-border rounded-xl text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:border-text-primary font-mono transition-all resize-y"
                    placeholder={`[\n  {\n    "title": "Backend developer",\n    "company": "GitHub",\n    "source": "github_jobs",\n    "source_url": "https://github.com/careers/2"\n  }\n]`}
                  />
                </div>
              ) : (
                <div>
                  <p className="text-xs text-text-muted mb-2">
                    Input CSV rows. Include header names such as: <code>title</code>, <code>company</code>, <code>location</code>, <code>source</code>, <code>source_url</code>.
                  </p>
                  <textarea
                    value={csvInput}
                    onChange={(e) => { setCsvInput(e.target.value); setSelectedFile(null); }}
                    className="w-full h-80 px-4 py-3 bg-bg-primary border border-bg-border rounded-xl text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:border-text-primary font-mono transition-all resize-y"
                    placeholder={`title,company,location,source,source_url\n"Senior React dev","Acme Inc","Remote","linkedin","https://linkedin.com/jobs/1"\n"Data engineer","BigData Corp","NY","indeed","https://indeed.com/jobs/2"`}
                  />
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Instructions & Import Settings (Col-Span-1) */}
        <div className="space-y-6">
          <div className="bg-bg-card border border-bg-border rounded-2xl p-5 shadow-sm space-y-4">
            <h2 className="text-sm font-semibold text-text-primary flex items-center gap-1.5">
              <Info className="w-4 h-4 text-text-muted" />
              Import Instructions
            </h2>
            
            <div className="space-y-3 text-xs text-text-secondary leading-relaxed">
              <div className="p-3 bg-bg-primary rounded-xl border border-bg-border">
                <span className="font-semibold block text-text-primary mb-1">Required Schema Fields</span>
                <ul className="list-disc pl-4 space-y-1">
                  <li><code>title</code>: Job title (e.g. Node Developer)</li>
                  <li><code>company</code>: Company name (e.g. Stripe)</li>
                  <li><code>source</code>: platform name (e.g. linkedin)</li>
                  <li><code>source_url</code>: Job link</li>
                </ul>
              </div>

              <div className="p-3 bg-bg-primary rounded-xl border border-bg-border">
                <span className="font-semibold block text-text-primary mb-1">Optional Schema Fields</span>
                <p>location, canonical_url, description, skills (comma separated or JSON array), salary_min, salary_max, job_type (e.g. full-time).</p>
              </div>

              <div className="p-3 bg-amber-500/5 text-amber-800 rounded-xl border border-amber-500/10">
                <span className="font-semibold block mb-1">Duplicate Handling</span>
                <p>The backend identifies duplicate jobs using the <code>source_url</code> property. Existing URLs will be automatically filtered out during ingestion.</p>
              </div>
            </div>
          </div>

          {/* Action Card */}
          <div className="bg-bg-card border border-bg-border rounded-2xl p-5 shadow-sm">
            <h2 className="text-sm font-semibold text-text-primary mb-3">Import Actions</h2>
            
            <div className="space-y-3.5">
              <div className="flex items-center justify-between text-xs py-1.5 border-b border-bg-border">
                <span className="text-text-muted">Parsed Total:</span>
                <span className="font-semibold text-text-primary">{parsedJobs.length}</span>
              </div>
              <div className="flex items-center justify-between text-xs py-1.5 border-b border-bg-border">
                <span className="text-text-muted">Valid Ready:</span>
                <span className="font-semibold text-green-600">{validJobs.length}</span>
              </div>
              <div className="flex items-center justify-between text-xs py-1.5 border-b border-bg-border">
                <span className="text-text-muted">Invalid Skipped:</span>
                <span className="font-semibold text-danger">{invalidJobsCount}</span>
              </div>

              <button
                onClick={handleImport}
                disabled={isLoading || validJobs.length === 0}
                className="w-full flex items-center justify-center gap-2 py-3 bg-accent hover:bg-accent-hover disabled:bg-bg-secondary text-white disabled:text-text-muted border disabled:border-bg-border rounded-xl text-sm font-medium transition-all disabled:cursor-not-allowed shadow-sm"
              >
                {isLoading ? <Loader2 className="w-4 h-4 animate-spin text-text-muted" /> : <CheckCircle2 className="w-4 h-4" />}
                {isLoading ? 'Processing Ingestion...' : `Ingest ${validJobs.length} Valid Jobs`}
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* Preview Section */}
      {parsedJobs.length > 0 && (
        <div className="bg-bg-card border border-bg-border rounded-2xl shadow-sm overflow-hidden animate-in slide-in-from-bottom-2 duration-300">
          <div className="px-6 py-4 border-b border-bg-border flex items-center justify-between">
            <div>
              <h2 className="text-sm font-bold text-text-primary">Parsed Jobs Preview</h2>
              <p className="text-xs text-text-muted mt-0.5">Please review the details below before committing database changes.</p>
            </div>
            <div className="flex gap-2">
              <span className="inline-flex items-center gap-1 px-2.5 py-1 bg-green-500/10 text-green-600 rounded-md text-xs font-semibold border border-green-500/20">
                <Check className="w-3.5 h-3.5 text-green-500" />
                {validJobs.length} Ready
              </span>
              {invalidJobsCount > 0 && (
                <span className="inline-flex items-center gap-1 px-2.5 py-1 bg-amber-500/10 text-amber-600 rounded-md text-xs font-semibold border border-amber-500/20">
                  <AlertTriangle className="w-3.5 h-3.5 text-amber-500" />
                  {invalidJobsCount} Invalid
                </span>
              )}
            </div>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs border-collapse">
              <thead className="bg-bg-primary text-text-muted font-medium uppercase tracking-wider border-b border-bg-border">
                <tr>
                  <th className="px-6 py-3.5 font-medium">Job Title & Company</th>
                  <th className="px-6 py-3.5 font-medium">Location</th>
                  <th className="px-6 py-3.5 font-medium">Source / Platform</th>
                  <th className="px-6 py-3.5 font-medium">Status & Validation Details</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-bg-border">
                {parsedJobs.map((job, idx) => (
                  <tr key={idx} className="hover:bg-bg-hover/40 transition-colors">
                    <td className="px-6 py-4">
                      <div className="font-semibold text-text-primary">{job.title || <span className="italic text-danger">Missing Title</span>}</div>
                      <div className="text-text-muted mt-0.5">{job.company || <span className="italic text-danger">Missing Company</span>}</div>
                    </td>
                    <td className="px-6 py-4 text-text-secondary whitespace-nowrap">
                      {job.location || 'N/A'}
                    </td>
                    <td className="px-6 py-4">
                      <div className="font-medium text-text-primary capitalize">{job.source || <span className="italic text-danger">Missing Source</span>}</div>
                      {job.source_url ? (
                        <a 
                          href={job.source_url} 
                          target="_blank" 
                          rel="noreferrer" 
                          className="text-info hover:underline text-[10px] block mt-0.5 truncate max-w-[200px]"
                        >
                          {job.source_url}
                        </a>
                      ) : (
                        <span className="italic text-danger text-[10px]">Missing URL link</span>
                      )}
                    </td>
                    <td className="px-6 py-4">
                      {job.isValid ? (
                        <span className="inline-flex items-center gap-1 px-2 py-0.5 bg-green-500/10 border border-green-500/20 text-green-600 rounded-md font-medium text-[10px]">
                          <Check className="w-3 h-3 text-green-500" />
                          Ready for Import
                        </span>
                      ) : (
                        <div className="space-y-1">
                          <span className="inline-flex items-center gap-1 px-2 py-0.5 bg-danger/10 border border-danger/20 text-danger rounded-md font-medium text-[10px]">
                            <AlertTriangle className="w-3 h-3 text-danger" />
                            Validation Warning
                          </span>
                          <div className="text-[10px] text-danger/80 pl-1">
                            {job.errors.join(', ')}
                          </div>
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
