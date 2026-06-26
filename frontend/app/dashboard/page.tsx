'use client'

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { KPICard } from '@/components/dashboard/KPICard'
import { ActivityFeed } from '@/components/dashboard/ActivityFeed'
import { PipelineKanban } from '@/components/dashboard/PipelineKanban'
import { Send, Target, Award, Briefcase } from 'lucide-react'

export default function DashboardPage() {
  const [selectedCandidateId, setSelectedCandidateId] = useState<string>('')

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
    <div className="space-y-8 animate-in fade-in duration-500">
      {/* Header with Candidate Filter */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-text-primary tracking-tight">Overview</h1>
          <p className="text-sm text-text-muted mt-1">Your automated job search progress.</p>
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

      {/* Summary Cards */}
      <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <KPICard 
          label="Jobs Found Today" 
          value={kpis?.applied_today ?? 0} 
          icon={<Briefcase className="w-4 h-4" />} 
          accent="info"  
          loading={kpisLoading} 
        />
        <KPICard 
          label="Applications In Progress" 
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
        <div className="xl:col-span-3 space-y-6">
          <div className="card p-6">
            <h2 className="text-lg font-semibold text-text-primary mb-4">Application Pipeline</h2>
            <PipelineKanban candidateId={selectedCandidateId || undefined} />
          </div>
        </div>
        
        <div className="xl:col-span-1">
          <ActivityFeed candidateId={selectedCandidateId || undefined} />
        </div>
      </div>
    </div>
  )
}
