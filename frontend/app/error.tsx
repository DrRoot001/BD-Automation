'use client'

import { useEffect } from 'react'
import { AlertTriangle, RefreshCw } from 'lucide-react'

export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string }
  reset: () => void
}) {
  useEffect(() => {
    // Optionally log the error to an error reporting service
  }, [error])

  return (
    <div className="min-h-[70vh] flex flex-col items-center justify-center text-center p-6 animate-fade-in">
      <div className="w-16 h-16 rounded-2xl bg-danger/10 border border-danger/20 flex items-center justify-center mb-4 text-danger">
        <AlertTriangle className="w-8 h-8" />
      </div>
      <h1 className="text-2xl font-bold text-text-primary tracking-tight">Something went wrong</h1>
      <p className="text-sm text-text-muted mt-2 max-w-md">
        An unexpected error occurred. You can try recovering by clicking the button below.
      </p>
      <div className="mt-6">
        <button
          onClick={() => reset()}
          className="btn-primary"
        >
          <RefreshCw className="w-4 h-4" /> Try Again
        </button>
      </div>
    </div>
  )
}
