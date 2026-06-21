'use client'

import { useState } from 'react'
import { useRouter } from 'next/navigation'

export default function LoginPage() {
  const router = useRouter()
  const [email, setEmail] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState('')

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!email.trim()) return

    setIsLoading(true)
    setError('')

    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: email.trim(), password: '' }),
      })

      if (!res.ok) {
        setError('Login failed. Please try again.')
        return
      }

      const data = await res.json()
      localStorage.setItem('auth_token', data.access_token)
      router.replace('/dashboard')
    } catch {
      setError('Could not reach the server. Is the backend running?')
    } finally {
      setIsLoading(false)
    }
  }

  return (
    <div className="w-full max-w-sm">
      <div className="bg-bg-secondary border border-bg-border rounded-2xl p-8 shadow-card">
        <div className="flex items-center gap-3 mb-8">
          <div className="w-9 h-9 rounded-lg bg-accent flex items-center justify-center text-white text-sm font-bold shadow-accent">
            BD
          </div>
          <span className="font-semibold text-text-primary text-lg">BD Automator</span>
        </div>

        <h1 className="text-base font-semibold text-text-primary mb-1">Sign in</h1>
        <p className="text-xs text-text-muted mb-6">Enter your email to access the dashboard.</p>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-xs font-medium text-text-secondary mb-1.5">
              Email address
            </label>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
              required
              className="w-full bg-bg-primary border border-bg-border rounded-lg px-3 py-2.5 text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent transition-colors"
            />
          </div>

          {error && (
            <p className="text-danger text-xs">{error}</p>
          )}

          <button
            type="submit"
            disabled={isLoading || !email.trim()}
            className="w-full bg-accent text-white py-2.5 rounded-lg text-sm font-medium hover:bg-accent/90 disabled:opacity-50 transition-colors"
          >
            {isLoading ? 'Signing in...' : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  )
}
