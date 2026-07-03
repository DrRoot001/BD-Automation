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
    <div className="space-y-6 animate-fade-in max-w-7xl mx-auto">
      {/* Header with Candidate Filter */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-bg-border pb-6">
        <div>
          <h1 className="page-title">Interviews</h1>
          <p className="page-subtitle">Track and schedule upcoming candidate interviews.</p>
        </div>

        <div className="flex items-center gap-3">
          <label htmlFor="candidate-select" className="text-xs text-text-muted font-medium whitespace-nowrap">
            Filter Candidate:
          </label>
          <select
            id="candidate-select"
            value={selectedCandidateId}
            onChange={(e) => setSelectedCandidateId(e.target.value)}
            disabled={candidatesLoading}
            className="input !w-auto min-w-[200px]"
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
