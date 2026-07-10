'use client'

import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { KPICard } from '@/components/dashboard/KPICard'
import { Activity, CheckCircle2, Layers, RefreshCw, Send, Server } from 'lucide-react'

export default function PipelineOpsPage() {
  const { data, isLoading, isError, refetch, isFetching, dataUpdatedAt } = useQuery({
    queryKey: ['ops-stats'],
    queryFn: () => api.getOpsStats(),
    refetchInterval: 10_000,
  })

  const queues = data?.queues ?? []
  const workers = data?.workers ?? []
  const maxDepth = Math.max(1, ...queues.map((q) => q.depth))

  return (
    <div className="space-y-6 max-w-7xl mx-auto animate-fade-in">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-bg-border pb-6">
        <div>
          <h1 className="page-title">Pipeline Operations</h1>
          <p className="page-subtitle">
            Live Celery queue depths, worker status, and application throughput.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <span className="flex items-center gap-2 text-xs text-text-muted">
            <span className="live-dot" />
            Live · refreshes every 10s
            {dataUpdatedAt > 0 && (
              <span className="hidden sm:inline">
                · updated {new Date(dataUpdatedAt).toLocaleTimeString()}
              </span>
            )}
          </span>
          <button
            onClick={() => refetch()}
            disabled={isFetching}
            className="btn-secondary !py-1.5 !px-3 !text-xs"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${isFetching ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>
      </div>

      {/* Error State */}
      {isError && !data && (
        <div className="bg-danger/10 border border-danger/20 rounded-xl p-8 text-center">
          <h3 className="text-base font-semibold text-danger mb-1">Failed to load operations stats</h3>
          <p className="text-danger/80 text-xs mb-4">
            The /dashboard/ops endpoint did not respond. Check that the backend is running.
          </p>
          <button onClick={() => refetch()} className="btn-secondary !text-xs inline-flex items-center gap-1.5">
            <RefreshCw className="w-3.5 h-3.5" /> Retry
          </button>
        </div>
      )}

      {(!isError || !!data) && (
        <>
          {/* KPI Row */}
          <section className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <KPICard
              label="Applications In Flight"
              value={data?.applications_in_flight ?? 0}
              icon={<Activity className="w-4 h-4" />}
              accent="warning"
              loading={isLoading}
            />
            <KPICard
              label="Applications Created Today"
              value={data?.applications_today ?? 0}
              icon={<Send className="w-4 h-4" />}
              accent="info"
              loading={isLoading}
            />
            <KPICard
              label="Submitted Today"
              value={data?.submitted_today ?? 0}
              icon={<CheckCircle2 className="w-4 h-4" />}
              accent="success"
              loading={isLoading}
            />
          </section>

          <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
            {/* Queue Depths */}
            <div className="card">
              <div className="card-header">
                <h2 className="card-title flex items-center gap-2">
                  <Layers className="w-4 h-4 text-text-muted" />
                  Queue Depths
                </h2>
                <span className="text-xs text-text-muted">
                  {queues.filter((q) => q.depth > 0).length} of {queues.length} queues backed up
                </span>
              </div>
              <div className="p-4 sm:p-5">
                {isLoading ? (
                  <div className="space-y-4">
                    {Array.from({ length: 5 }).map((_, i) => (
                      <div key={i}>
                        <div className="skeleton h-3 w-40 rounded mb-1.5" />
                        <div className="skeleton h-5 rounded" />
                      </div>
                    ))}
                  </div>
                ) : queues.length === 0 ? (
                  <div className="py-10 text-center text-text-muted text-sm">
                    No queues reported by the broker.
                  </div>
                ) : (
                  <div className="space-y-4">
                    {queues.map((q) => {
                      const nonZero = q.depth > 0
                      const pct = nonZero ? Math.max(4, (q.depth / maxDepth) * 100) : 0
                      return (
                        <div key={q.name}>
                          <div className="flex items-center justify-between mb-1">
                            <span
                              className={`text-xs font-mono truncate ${
                                nonZero ? 'text-text-primary font-semibold' : 'text-text-muted'
                              }`}
                              title={q.name}
                            >
                              {q.name}
                            </span>
                            <span
                              className={`text-xs font-semibold tabular-nums ${
                                nonZero ? 'text-warning' : 'text-text-muted'
                              }`}
                            >
                              {q.depth.toLocaleString()}
                            </span>
                          </div>
                          <div
                            className={`h-5 rounded overflow-hidden border ${
                              nonZero
                                ? 'bg-warning/5 border-warning/20'
                                : 'bg-bg-secondary border-bg-border/60'
                            }`}
                          >
                            <div
                              className="h-full rounded bg-warning/70 transition-all duration-500 ease-out"
                              style={{ width: `${pct}%` }}
                            />
                          </div>
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            </div>

            {/* Workers */}
            <div className="card">
              <div className="card-header">
                <h2 className="card-title flex items-center gap-2">
                  <Server className="w-4 h-4 text-text-muted" />
                  Workers Online
                </h2>
                <span className="text-xs text-text-muted">
                  {workers.length} worker{workers.length === 1 ? '' : 's'} responding
                </span>
              </div>
              <div className="p-4 sm:p-5">
                {isLoading ? (
                  <div className="space-y-3">
                    {Array.from({ length: 4 }).map((_, i) => (
                      <div key={i} className="skeleton h-12 rounded-lg" />
                    ))}
                  </div>
                ) : workers.length === 0 ? (
                  <div className="py-10 text-center">
                    <Server className="w-10 h-10 text-bg-border mx-auto mb-3" />
                    <p className="text-text-primary text-sm font-medium mb-1">No workers responding</p>
                    <p className="text-text-muted text-xs max-w-xs mx-auto">
                      No workers responding — check that Celery workers are running.
                    </p>
                  </div>
                ) : (
                  <div className="space-y-2">
                    {workers.map((w) => (
                      <div
                        key={w.name}
                        className="flex items-center justify-between gap-3 px-3 py-2.5 bg-bg-primary border border-bg-border rounded-lg"
                      >
                        <div className="flex items-center gap-2.5 min-w-0">
                          <span className="live-dot" />
                          <span className="text-xs font-mono text-text-primary truncate" title={w.name}>
                            {w.name}
                          </span>
                        </div>
                        <span
                          className={`badge border shrink-0 ${
                            w.active_tasks > 0
                              ? 'bg-info/10 border-info/20 text-info'
                              : 'bg-bg-secondary border-bg-border text-text-muted'
                          }`}
                        >
                          {w.active_tasks} active task{w.active_tasks === 1 ? '' : 's'}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
