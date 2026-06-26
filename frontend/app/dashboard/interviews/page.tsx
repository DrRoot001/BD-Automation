'use client'

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { InterviewList } from '@/components/dashboard/InterviewList'
import { useWebSocket } from '@/hooks/useWebSocket'

export default function InterviewsPage() {
  const [selectedCandidateId, setSelectedCandidateId] = useState<string>('')

  // Connect WebSocket to get real-time cache invalidations
  useWebSocket()

  // Fetch candidates managed by the logged-in BD User
  const { data: candidates = [], isLoading: candidatesLoading } = useQuery({
    queryKey: ['candidates'],
    queryFn: () => api.getCandidates(),
  })

  return (
    <div className="space-y-6 animate-in fade-in duration-500 max-w-7xl mx-auto">
      {/* Header with Candidate Filter */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-text-primary tracking-tight">Interviews</h1>
          <p className="text-sm text-text-muted mt-1">Track and schedule upcoming candidate interviews.</p>
        </div>

        <div className="flex items-center gap-3">
          <label htmlFor="candidate-select" className="text-xs text-text-muted font-medium whitespace-nowrap">
            Candidate:
          </label>
          <select
            id="candidate-select"
            value={selectedCandidateId}
            onChange={(e) => setSelectedCandidateId(e.target.value)}
            disabled={candidatesLoading}
            className="bg-bg-secondary border border-bg-border text-text-primary rounded-lg text-sm px-4 py-2 focus:outline-none focus:border-accent transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed min-w-[200px]"
          >
            <option value="">All Candidates</option>
            {candidates.map((cand) => (
              <option key={cand.id} value={cand.id}>
                {cand.name}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Interviews List */}
      <div className="max-w-4xl">
        <InterviewList candidateId={selectedCandidateId || undefined} />
      </div>
    </div>
  )
}
