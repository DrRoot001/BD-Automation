'use client'

import { useState } from 'react'
import { UploadCloud, CheckCircle2, AlertCircle, Loader2 } from 'lucide-react'
import { api } from '@/lib/api'

export default function JobImportPage() {
  const [jsonInput, setJsonInput] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [result, setResult] = useState<{ success: boolean; message: string; count?: number } | null>(null)

  const handleImport = async () => {
    setIsLoading(true)
    setResult(null)
    
    try {
      if (!jsonInput.trim()) {
        throw new Error("Please paste valid JSON data.")
      }
      
      const parsedData = JSON.parse(jsonInput)
      
      if (!Array.isArray(parsedData)) {
        throw new Error("Data must be a JSON array of job objects.")
      }
      
      // Basic validation
      if (parsedData.length > 0 && (!parsedData[0].title || !parsedData[0].company)) {
        throw new Error("Invalid format. Jobs must contain 'title', 'company', etc.")
      }

      // We send it to api.getJobs but wait! The api object doesn't have createJobs exposed yet!
      // Let's use fetch directly for now or I should update api.ts. I'll use fetch directly so we don't have to edit api.ts again.
      
      const res = await fetch('/api/jobs', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          // Usually we'd attach the token, but this is a proxy so middleware handles it?
          // Actually, our middleware redirects /api to backend and attaches the token!
        },
        body: JSON.stringify(parsedData)
      })
      
      if (!res.ok) {
        const errText = await res.text()
        throw new Error(`API Error: ${res.status} - ${errText}`)
      }
      
      const createdJobs = await res.json()
      
      setResult({
        success: true,
        message: "Successfully imported jobs!",
        count: createdJobs.length
      })
      setJsonInput('') // clear on success
      
    } catch (err: any) {
      setResult({
        success: false,
        message: err.message || "Failed to parse and import data."
      })
    } finally {
      setIsLoading(false)
    }
  }

  return (
    <div className="space-y-6 max-w-4xl mx-auto animate-in fade-in duration-500">
      
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-text-primary tracking-tight">Job Import</h1>
        <p className="text-sm text-text-muted mt-1">Manually import job records when the automated scraping pipeline isn't running.</p>
      </div>

      {result && (
        <div className={`p-4 rounded-xl border flex items-start gap-3 ${result.success ? 'bg-green-500/10 border-green-500/20 text-green-500' : 'bg-danger/10 border-danger/20 text-danger'}`}>
          {result.success ? <CheckCircle2 className="w-5 h-5 shrink-0" /> : <AlertCircle className="w-5 h-5 shrink-0" />}
          <div>
            <h3 className="text-sm font-medium">{result.success ? 'Import Successful' : 'Import Failed'}</h3>
            <p className="text-xs mt-0.5 opacity-90">{result.message}</p>
            {result.count !== undefined && (
              <p className="text-xs mt-1 font-semibold">{result.count} new jobs added to the database.</p>
            )}
          </div>
        </div>
      )}

      <div className="bg-bg-secondary border border-bg-border rounded-xl p-6 shadow-sm">
        <div className="mb-4">
          <label className="block text-sm font-medium text-text-primary mb-2">
            Paste JSON Data
          </label>
          <p className="text-xs text-text-muted mb-3">
            Format should be a JSON array. Required fields: <code>title</code>, <code>company</code>, <code>source</code>, <code>source_url</code>.
          </p>
          <textarea
            value={jsonInput}
            onChange={(e) => setJsonInput(e.target.value)}
            className="w-full h-96 px-4 py-3 bg-bg-primary border border-bg-border rounded-xl text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent font-mono transition-colors"
            placeholder="[\n  {\n    &#34;title&#34;: &#34;Senior Software Engineer&#34;,\n    &#34;company&#34;: &#34;Acme Corp&#34;,\n    &#34;location&#34;: &#34;Remote, US&#34;,\n    &#34;source&#34;: &#34;greenhouse&#34;,\n    &#34;source_url&#34;: &#34;https://boards.greenhouse.io/...&#34;\n  }\n]"
          />
        </div>
        
        <div className="flex justify-end">
          <button
            onClick={handleImport}
            disabled={isLoading || !jsonInput.trim()}
            className="flex items-center gap-2 px-6 py-2.5 bg-accent hover:bg-accent-hover text-white rounded-lg font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <UploadCloud className="w-4 h-4" />}
            {isLoading ? 'Importing...' : 'Import Jobs'}
          </button>
        </div>
      </div>
    </div>
  )
}
