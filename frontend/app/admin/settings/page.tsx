'use client'

import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { RefreshCw, Save } from 'lucide-react'

export default function AdminSettingsPage() {
  const queryClient = useQueryClient()
  const { data, isLoading, isError, refetch, isFetching } = useQuery({
    queryKey: ['runtime-settings'],
    queryFn: () => api.getSettings(),
  })

  const settings = data?.settings ?? {}
  const [form, setForm] = useState<Record<string, string | number>>({})

  // Re-seed the form whenever fresh server data arrives.
  useEffect(() => {
    const seed: Record<string, string | number> = {}
    for (const [key, s] of Object.entries(settings)) seed[key] = s.value
    setForm(seed)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data])

  const mutation = useMutation({
    mutationFn: (updates: Record<string, string | number>) => api.updateSettings(updates),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['runtime-settings'] }),
  })

  const dirty = Object.entries(form).some(
    ([k, v]) => String(v) !== String(settings[k]?.value ?? ''),
  )

  const inputCls =
    'w-full bg-bg-secondary border border-bg-border rounded-lg px-3 py-2 text-sm ' +
    'text-text-primary focus:outline-none focus:ring-2 focus:ring-accent/40'

  return (
    <div className="space-y-6 max-w-3xl mx-auto animate-fade-in">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-bg-border pb-6">
        <div>
          <h1 className="page-title">Settings</h1>
          <p className="page-subtitle">
            Live pipeline controls — changes apply immediately across all workers, no restart.
          </p>
        </div>
        <button
          onClick={() => refetch()}
          disabled={isFetching}
          className="btn-secondary !py-1.5 !px-3 !text-xs"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${isFetching ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>

      {isError && (
        <div className="bg-danger/10 border border-danger/20 rounded-xl p-6 text-center">
          <h3 className="text-sm font-semibold text-danger mb-1">Failed to load settings</h3>
          <p className="text-danger/80 text-xs mb-3">
            The /settings endpoint did not respond. Check that the backend is running.
          </p>
          <button onClick={() => refetch()} className="btn-secondary !text-xs inline-flex items-center gap-1.5">
            <RefreshCw className="w-3.5 h-3.5" /> Retry
          </button>
        </div>
      )}

      {isLoading && !data && (
        <div className="space-y-3" aria-hidden>
          {[...Array(3)].map((_, i) => (
            <div key={i} className="h-24 rounded-xl bg-bg-hover animate-pulse" />
          ))}
        </div>
      )}

      {!isError && !!data && (
        <form
          onSubmit={(e) => {
            e.preventDefault()
            mutation.mutate(form)
          }}
          className="space-y-4"
        >
          {Object.entries(settings).map(([key, s]) => (
            <div key={key} className="bg-bg-card border border-bg-border rounded-xl p-4">
              <div className="flex items-center justify-between mb-1">
                <label htmlFor={key} className="text-sm font-medium text-text-primary">
                  {s.label}
                </label>
                <span
                  className={`text-[10px] px-2 py-0.5 rounded-full ${
                    s.source === 'override'
                      ? 'bg-accent/10 text-accent'
                      : 'bg-bg-hover text-text-muted'
                  }`}
                >
                  {s.source === 'override' ? 'custom' : 'default'}
                </span>
              </div>
              {s.help && <p className="text-xs text-text-muted mb-2">{s.help}</p>}
              {s.type === 'select' ? (
                <select
                  id={key}
                  value={String(form[key] ?? s.value)}
                  onChange={(e) => setForm({ ...form, [key]: e.target.value })}
                  className={inputCls}
                >
                  {(s.options ?? []).map((opt) => (
                    <option key={opt} value={opt}>
                      {opt}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  id={key}
                  type="number"
                  min={s.min}
                  max={s.max}
                  value={form[key] ?? ''}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      [key]: e.target.value === '' ? '' : Number(e.target.value),
                    })
                  }
                  className={inputCls}
                />
              )}
            </div>
          ))}

          <div className="flex items-center gap-3 pt-1">
            <button
              type="submit"
              disabled={!dirty || mutation.isPending}
              className="inline-flex items-center gap-2 rounded-lg bg-accent text-white text-sm font-medium px-4 py-2 disabled:opacity-40 disabled:cursor-not-allowed hover:opacity-90 transition-opacity"
            >
              <Save className="w-4 h-4" />
              {mutation.isPending ? 'Saving…' : 'Save changes'}
            </button>
            {mutation.isSuccess && !dirty && (
              <span className="text-xs text-accent">Saved — live now.</span>
            )}
            {mutation.isError && (
              <span className="text-xs text-danger">
                {(mutation.error as Error)?.message ?? 'Save failed'}
              </span>
            )}
          </div>
        </form>
      )}
    </div>
  )
}
