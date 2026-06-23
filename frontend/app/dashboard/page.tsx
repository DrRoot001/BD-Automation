'use client'

import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { KPICard } from '@/components/dashboard/KPICard'
import { ActivityFeed } from '@/components/dashboard/ActivityFeed'
import { PipelineKanban } from '@/components/dashboard/PipelineKanban'
import { Send, Target, Award, Briefcase } from 'lucide-react'

export default function DashboardPage() {
  const { data: kpis, isLoading: kpisLoading } = useQuery({
    queryKey: ['kpis'],
    queryFn: () => api.getKPIs(),
    refetchInterval: 60_000,
  })

  return (
    <div className="space-y-8 animate-in fade-in duration-500">
      <div>
        <h1 className="text-2xl font-bold text-text-primary tracking-tight">Overview</h1>
        <p className="text-sm text-text-muted mt-1">Your automated job search progress.</p>
      </div>

      {/* Summary Cards */}
      <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <KPICard 
          label="Jobs Found Today" 
          value={kpis?.applied_today ?? 0} // In real app, we might need a specific endpoint for 'found today'
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
            <PipelineKanban />
          </div>
        </div>
        
        <div className="xl:col-span-1">
          <ActivityFeed />
        </div>
      </div>
    </div>
  )
}
