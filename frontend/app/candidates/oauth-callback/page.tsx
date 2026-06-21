'use client'

import { useEffect, useState, useRef, Suspense } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'
import { api } from '@/lib/api'
import { Loader2, CheckCircle, AlertTriangle } from 'lucide-react'
import Link from 'next/link'

function OAuthCallbackContent() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const [status, setStatus] = useState<'loading' | 'success' | 'error'>('loading')
  const [message, setMessage] = useState('Exchanging authorization code with Google...')
  const calledExchange = useRef(false)

  useEffect(() => {
    const code = searchParams.get('code')
    const candidateId = searchParams.get('state')

    if (!code || !candidateId) {
      setStatus('error')
      setMessage('Missing OAuth code or candidate ID in URL.')
      return
    }

    if (calledExchange.current) return
    calledExchange.current = true

    async function exchangeCode() {
      try {
        await api.exchangeGoogleCode(candidateId!, code!)
        setStatus('success')
        setMessage('Gmail successfully connected! Redirecting...')
        setTimeout(() => {
          router.push(`/candidates/${candidateId}?oauth=success`)
        }, 2000)
      } catch (err: any) {
        setStatus('error')
        setMessage(err.message || 'Failed to connect Gmail account.')
      }
    }

    exchangeCode()
  }, [searchParams, router])

  return (
    <div className="max-w-md w-full bg-bg-secondary border border-bg-border rounded-xl p-8 text-center shadow-card animate-fade-in">
      {status === 'loading' && (
        <div className="flex flex-col items-center gap-4">
          <Loader2 className="w-10 h-10 text-accent animate-spin" />
          <h1 className="text-base font-semibold text-text-primary">Connecting Gmail</h1>
          <p className="text-xs text-text-muted">{message}</p>
        </div>
      )}

      {status === 'success' && (
        <div className="flex flex-col items-center gap-4">
          <CheckCircle className="w-10 h-10 text-success" />
          <h1 className="text-base font-semibold text-text-primary">Success!</h1>
          <p className="text-xs text-success">{message}</p>
        </div>
      )}

      {status === 'error' && (
        <div className="flex flex-col items-center gap-4">
          <AlertTriangle className="w-10 h-10 text-danger" />
          <h1 className="text-base font-semibold text-text-primary">Connection Failed</h1>
          <p className="text-xs text-danger mb-2">{message}</p>
          <Link
            href="/candidates"
            className="text-xs bg-bg-primary border border-bg-border text-text-primary px-4 py-2 rounded-lg hover:bg-bg-hover transition-colors"
          >
            Back to Candidates
          </Link>
        </div>
      )}
    </div>
  )
}

export default function OAuthCallbackPage() {
  return (
    <div className="min-h-screen bg-bg-primary flex items-center justify-center p-8">
      <Suspense fallback={
        <div className="max-w-md w-full bg-bg-secondary border border-bg-border rounded-xl p-8 text-center shadow-card animate-fade-in flex flex-col items-center gap-4">
          <Loader2 className="w-10 h-10 text-accent animate-spin" />
          <h1 className="text-base font-semibold text-text-primary">Loading Callback Handler...</h1>
        </div>
      }>
        <OAuthCallbackContent />
      </Suspense>
    </div>
  )
}
