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
  FileCode
} from 'lucide-react'
import { parseCSV, mapCSVRowToJob, validateJobInputs, ParsedJob, RawJobInput } from '@/lib/csv-parser'
import { useToast } from '@/components/ui/Toast'

export default function JobImportPage() {
  const toast = useToast()
  const [activeTab, setActiveTab] = useState<'json' | 'csv'>('json')
  const [jsonInput, setJsonInput] = useState('')
  const [csvInput, setCsvInput] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [result, setResult] = useState<{ success: boolean; message: string; count?: number; errorCount?: number; duplicateCount?: number } | null>(null)
  
  const [selectedFile, setSelectedFile] = useState<File | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [dragActive, setDragActive] = useState(false)

  const getParsedJobs = (): ParsedJob[] => {
    const jobsList: RawJobInput[] = []
    
    if (activeTab === 'json') {
      if (!jsonInput.trim()) return []
      try {
        const parsed = JSON.parse(jsonInput)
        if (Array.isArray(parsed)) {
          jobsList.push(...parsed)
        } else if (typeof parsed === 'object' && parsed !== null) {
          jobsList.push(parsed)
        }
      } catch {
        return []
      }
    } else {
      if (!csvInput.trim()) return []
      try {
        const rows = parseCSV(csvInput)
        const mapped = rows.map(mapCSVRowToJob)
        jobsList.push(...mapped)
      } catch {
        return []
      }
    }

    return validateJobInputs(jobsList)
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
      toast.error("Invalid file type. Please upload a .json or .csv file.")
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
      toast.info(`Loaded ${file.name}`)
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
      // Strip parsing metadata fields
      const cleanData = validJobs.map(({ isValid: _isValid, errors: _errors, ...rest }) => rest)
      
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
      const skippedDuplicates = parseInt(res.headers.get('X-Skipped-Duplicates') || '0', 10)

      const importMessage = createdJobs.length === 0 && skippedDuplicates > 0
        ? `All ${skippedDuplicates} jobs already exist in the database.`
        : `Successfully imported ${createdJobs.length} job(s)!`

      setResult({
        success: true,
        message: importMessage,
        count: createdJobs.length,
        errorCount: invalidJobsCount,
        duplicateCount: skippedDuplicates
      })
      toast.success(importMessage)
      
      // Reset inputs on success
      setJsonInput('')
      setCsvInput('')
      setSelectedFile(null)
      if (fileInputRef.current) {
        fileInputRef.current.value = ''
      }
      
    } catch (err: unknown) {
      const errorObj = err as { message?: string }
      const msg = errorObj.message || "Failed to import data to database."
      setResult({
        success: false,
        message: msg
      })
      toast.error(msg)
    } finally {
      setIsLoading(false)
    }
  }

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
    <div className="space-y-6 max-w-5xl mx-auto animate-fade-in pb-16">
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 border-b border-bg-border pb-6">
        <div>
          <h1 className="page-title">Job Import Dashboard</h1>
          <p className="page-subtitle">Upload files or paste job datasets directly using CSV or JSON formats.</p>
        </div>
        
        <div className="flex gap-2">
          <button 
            onClick={downloadJSONTemplate}
            className="btn-secondary !py-1.5 !px-3 !text-xs"
          >
            <FileCode className="w-3.5 h-3.5" />
            JSON Template
          </button>
          <button 
            onClick={downloadCSVTemplate}
            className="btn-secondary !py-1.5 !px-3 !text-xs"
          >
            <FileText className="w-3.5 h-3.5" />
            CSV Template
          </button>
        </div>
      </div>

      {result && (
        <div className={`p-4 rounded-xl border flex items-start gap-3 shadow-sm transition-all ${result.success ? 'bg-success/10 border-success/20 text-success' : 'bg-danger/10 border-danger/20 text-danger'}`}>
          {result.success ? <CheckCircle2 className="w-5 h-5 shrink-0" /> : <AlertCircle className="w-5 h-5 shrink-0" />}
          <div>
            <h3 className="text-sm font-semibold">{result.success ? 'Import Complete' : 'Import Failed'}</h3>
            <p className="text-xs mt-0.5 opacity-90">{result.message}</p>
            {result.success && result.count !== undefined && (
              <div className="mt-2 text-xs font-medium flex flex-wrap gap-x-4 gap-y-1">
                {result.count > 0 && <span>✓ {result.count} jobs added successfully.</span>}
                {result.duplicateCount && result.duplicateCount > 0 ? (
                  <span className="text-warning">⚠ {result.duplicateCount} already exist in database.</span>
                ) : null}
                {result.errorCount && result.errorCount > 0 ? (
                  <span className="text-danger">✗ {result.errorCount} invalid rows skipped.</span>
                ) : null}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Main Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Upload Column */}
        <div className="lg:col-span-2 space-y-6">
          {/* Uploader Card */}
          <div className="card p-6 bg-bg-card border border-bg-border rounded-2xl shadow-sm">
            <h2 className="text-sm font-semibold text-text-primary mb-4 flex items-center gap-2">
              <UploadCloud className="w-4 h-4 text-text-muted" />
              1. Load Job File
            </h2>
            
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
              } ${selectedFile ? 'bg-success/5 border-success/40' : ''}`}
            >
              <input 
                type="file"
                ref={fileInputRef}
                onChange={handleFileChange}
                accept=".csv,.json"
                className="hidden"
              />
              
              <div className="flex flex-col items-center justify-center space-y-2">
                <div className={`p-3 rounded-full ${selectedFile ? 'bg-success/10 text-success' : 'bg-bg-secondary text-text-muted'}`}>
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
                  className="btn-danger !py-1.5 !px-3 !text-xs"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                  Remove File
                </button>
              </div>
            )}
          </div>

          {/* Paste Text Card */}
          <div className="card p-6 bg-bg-card border border-bg-border rounded-2xl shadow-sm">
            <div className="flex items-center justify-between border-b border-bg-border pb-3 mb-4">
              <h2 className="text-sm font-semibold text-text-primary flex items-center gap-2">
                <FileCode className="w-4 h-4 text-text-muted" />
                2. Or Paste Dataset Manually
              </h2>
              
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
                    Input a JSON list. Must include fields: <code>title</code>, <code>company</code>, <code>source</code>, and <code>source_url</code>.
                  </p>
                  <textarea
                    value={jsonInput}
                    onChange={(e) => { setJsonInput(e.target.value); setSelectedFile(null); }}
                    className="input font-mono h-72 resize-y"
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
                    className="input font-mono h-72 resize-y"
                    placeholder={`title,company,location,source,source_url\n"Senior React dev","Acme Inc","Remote","linkedin","https://linkedin.com/jobs/1"\n"Data engineer","BigData Corp","NY","indeed","https://indeed.com/jobs/2"`}
                  />
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Instructions & Actions Column */}
        <div className="space-y-6">
          <div className="card p-5 bg-bg-card border border-bg-border rounded-2xl shadow-sm space-y-4">
            <h2 className="text-sm font-semibold text-text-primary flex items-center gap-1.5">
              <Info className="w-4 h-4 text-text-muted" />
              Import Instructions
            </h2>
            
            <div className="space-y-3 text-xs text-text-secondary leading-relaxed">
              <div className="p-3 bg-bg-primary rounded-xl border border-bg-border">
                <span className="font-semibold block text-text-primary mb-1">Required Schema Fields</span>
                <ul className="list-disc pl-4 space-y-1">
                  <li><code>title</code>: Job title</li>
                  <li><code>company</code>: Company name</li>
                  <li><code>source</code>: platform name</li>
                  <li><code>source_url</code>: Job link</li>
                </ul>
              </div>

              <div className="p-3 bg-bg-primary rounded-xl border border-bg-border">
                <span className="font-semibold block text-text-primary mb-1">Optional Schema Fields</span>
                <p>location, canonical_url, description, skills, salary_min, salary_max, job_type.</p>
              </div>

              <div className="p-3 bg-warning/10 text-warning rounded-xl border border-warning/20">
                <span className="font-semibold block mb-1">Duplicate Handling</span>
                <p>Existing URLs will be automatically filtered out during ingestion based on <code>source_url</code>.</p>
              </div>
            </div>
          </div>

          {/* Action Card */}
          <div className="card p-5 bg-bg-card border border-bg-border rounded-2xl shadow-sm">
            <h2 className="text-sm font-semibold text-text-primary mb-3">Import Actions</h2>
            
            <div className="space-y-3.5">
              <div className="flex items-center justify-between text-xs py-1.5 border-b border-bg-border">
                <span className="text-text-muted">Parsed Total:</span>
                <span className="font-semibold text-text-primary">{parsedJobs.length}</span>
              </div>
              <div className="flex items-center justify-between text-xs py-1.5 border-b border-bg-border">
                <span className="text-text-muted">Valid Ready:</span>
                <span className="font-semibold text-success">{validJobs.length}</span>
              </div>
              <div className="flex items-center justify-between text-xs py-1.5 border-b border-bg-border">
                <span className="text-text-muted">Invalid Skipped:</span>
                <span className="font-semibold text-danger">{invalidJobsCount}</span>
              </div>

              <button
                onClick={handleImport}
                disabled={isLoading || validJobs.length === 0}
                className="btn-primary w-full py-3 shadow-sm"
              >
                {isLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <CheckCircle2 className="w-4 h-4" />}
                {isLoading ? 'Processing Ingestion...' : `Ingest ${validJobs.length} Valid Jobs`}
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* Preview Section */}
      {parsedJobs.length > 0 && (
        <div className="card bg-bg-card border border-bg-border rounded-2xl shadow-sm overflow-hidden animate-slide-up">
          <div className="px-6 py-4 border-b border-bg-border flex items-center justify-between">
            <div>
              <h2 className="text-sm font-bold text-text-primary">Parsed Jobs Preview</h2>
              <p className="text-xs text-text-muted mt-0.5">Review details below before committing database changes.</p>
            </div>
            <div className="flex gap-2">
              <span className="inline-flex items-center gap-1 px-2.5 py-1 bg-success/10 text-success rounded-md text-xs font-semibold border border-success/20">
                <Check className="w-3.5 h-3.5" />
                {validJobs.length} Ready
              </span>
              {invalidJobsCount > 0 && (
                <span className="inline-flex items-center gap-1 px-2.5 py-1 bg-warning/10 text-warning rounded-md text-xs font-semibold border border-warning/20">
                  <AlertTriangle className="w-3.5 h-3.5" />
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
                  <tr key={idx} className="hover:bg-bg-hover transition-colors">
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
                        <span className="inline-flex items-center gap-1 px-2 py-0.5 bg-success/10 border border-success/20 text-success rounded-md font-medium text-[10px]">
                          <Check className="w-3 h-3" />
                          Ready for Import
                        </span>
                      ) : (
                        <div className="space-y-1">
                          <span className="inline-flex items-center gap-1 px-2 py-0.5 bg-danger/10 border border-danger/20 text-danger rounded-md font-medium text-[10px]">
                            <AlertTriangle className="w-3 h-3" />
                            Validation Warning
                          </span>
                          <div className="text-[10px] text-danger pl-1">
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
