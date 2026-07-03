'use client'

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { KPICard } from '@/components/dashboard/KPICard'
import { ActivityFeed } from '@/components/dashboard/ActivityFeed'
import { PipelineKanban } from '@/components/dashboard/PipelineKanban'
import { Send, Target, Award, CheckCircle2 } from 'lucide-react'
import { useWebSocket } from '@/hooks/useWebSocket'

export default function DashboardPage() {
  const [selectedCandidateId, setSelectedCandidateId] = useState<string>('')

  // Connect WebSocket to get real-time cache invalidations
  useWebSocket()

  // Fetch candidates managed by the logged-in BD User
  const { data: candidates = [], isLoading: candidatesLoading } = useQuery({
    queryKey: ['candidates'],
    queryFn: () => api.getCandidates(),
  })

  const { data: kpis, isLoading: kpisLoading } = useQuery({
    queryKey: ['kpis', selectedCandidateId],
    queryFn: () => api.getKPIs(selectedCandidateId || undefined),
    refetchInterval: 60_000,
  })

  return (
    <div className="space-y-8 animate-fade-in max-w-7xl mx-auto">
      {/* Header with Candidate Filter */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-bg-border pb-6">
        <div>
          <h1 className="page-title">Overview</h1>
          <p className="page-subtitle">Track job search progress and automated pipeline metrics.</p>
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
            className="input !w-auto min-w-[220px]"
          >
            <option value="">All Managed Candidates</option>
            {candidates.map((cand) => (
              <option key={cand.id} value={cand.id}>
                {cand.name}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Summary Cards */}
      <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <KPICard 
          label="Applied Today" 
          value={kpis?.applied_today ?? 0} 
          icon={<CheckCircle2 className="w-4 h-4" />} 
          accent="info"  
          loading={kpisLoading} 
        />
        <KPICard 
          label="Queued in Pipeline" 
          value={kpis?.pending_in_queue ?? 0} 
          icon={<Send className="w-4 h-4" />} 
          accent="warning" 
          loading={kpisLoading} 
        />
        <KPICard 
          label="Interviews Scheduled" 
          value={kpis?.interviews_this_week ?? 0} 
          icon={<Target className="w-4 h-4" />} 
          accent="purple" 
          loading={kpisLoading} 
        />
        <KPICard 
          label="Offers Received" 
          value={kpis?.total_offers ?? 0} 
          icon={<Award className="w-4 h-4" />} 
          accent="success" 
          loading={kpisLoading} 
        />
      </section>

      {/* Main content grid */}
      <div className="grid grid-cols-1 xl:grid-cols-4 gap-6">
        <div className="xl:col-span-3 space-y-6 min-w-0">
          <div className="card p-6">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-base font-semibold text-text-primary">Application Pipeline</h2>
              <span className="text-xs text-text-muted">Real-time status tracking</span>
            </div>
            <PipelineKanban candidateId={selectedCandidateId || undefined} />
          </div>
        </div>
        
        <div className="xl:col-span-1 min-w-0">
          <ActivityFeed candidateId={selectedCandidateId || undefined} />
        </div>
      </div>
    </div>
  )
}
