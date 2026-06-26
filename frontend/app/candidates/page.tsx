'use client'

import { useQuery } from '@tanstack/react-query'
import Link from 'next/link'
import { api } from '@/lib/api'

export default function CandidatesPage() {
  const { data: candidates = [], isLoading, isError, refetch } = useQuery({
    queryKey: ['candidates'],
    queryFn: () => api.getCandidates(),
  })

  return (
    <div className="min-h-screen bg-bg-primary p-8">
      <div className="max-w-screen-xl mx-auto px-6 pb-8 animate-fade-in">
        <div className="flex justify-between items-center mb-6">
          <div>
            <h1 className="text-base font-semibold text-text-primary">Candidates</h1>
            <p className="text-xs text-text-muted mt-1">Manage profiles and track active job applications.</p>
          </div>
          <Link
            href="/candidates/new"
            className="bg-text-primary text-bg-primary px-4 py-2 rounded-lg text-sm font-medium hover:opacity-90 transition-opacity"
          >
            Add Candidate
          </Link>
        </div>

        {isLoading ? (
          <div className="space-y-2">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="skeleton h-14 rounded-xl" />
            ))}
          </div>
        ) : isError ? (
          <div className="p-8 text-center bg-danger/10 border border-danger/20 rounded-xl">
            <p className="text-danger text-sm mb-4">Failed to load candidates.</p>
            <button onClick={() => refetch()} className="px-4 py-2 bg-danger text-white rounded-lg text-sm font-medium">Retry</button>
          </div>
        ) : candidates.length === 0 ? (
          <div className="p-12 text-center bg-bg-secondary border border-bg-border rounded-xl">
            <div className="text-4xl mb-4">👤</div>
            <p className="text-text-primary font-medium mb-2">No candidates found</p>
            <p className="text-text-muted text-sm mb-6">Start by adding your first candidate to the system.</p>
            <Link
              href="/candidates/new"
              className="bg-accent text-white px-4 py-2 rounded-lg text-sm font-medium hover:bg-accent/90"
            >
              Add Candidate
            </Link>
          </div>
        ) : (
          <div className="card overflow-hidden">
            {/* Mobile cards */}
            <div className="sm:hidden divide-y divide-bg-border">
              {candidates.map((cand) => (
                <div key={cand.id} className="p-4 hover:bg-bg-hover transition-colors">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="font-medium text-text-primary text-sm truncate">{cand.name}</p>
                      <p className="text-xs text-text-muted truncate mt-0.5">{cand.email}</p>
                      <p className="text-xs text-text-muted mt-0.5">
                        {cand.location || 'No location'} · {cand.years_exp ? `${cand.years_exp} yrs exp` : 'Exp not set'}
                      </p>
                    </div>
                    <Link href={`/candidates/${cand.id}`} className="btn-secondary text-xs py-1 px-2.5 whitespace-nowrap shrink-0">
                      View
                    </Link>
                  </div>
                </div>
              ))}
            </div>
            {/* Desktop table */}
            <div className="hidden sm:block overflow-x-auto">
              <table className="w-full text-left text-sm text-text-secondary">
                <thead className="border-b border-bg-border text-xs uppercase text-text-muted">
                  <tr>
                    <th className="px-6 py-3">Name</th>
                    <th className="px-6 py-3 hidden md:table-cell">Email</th>
                    <th className="px-6 py-3 hidden lg:table-cell">Location</th>
                    <th className="px-6 py-3 hidden md:table-cell">Experience</th>
                    <th className="px-6 py-3 text-right">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {candidates.map((cand) => (
                    <tr key={cand.id} className="border-b border-bg-border last:border-0 hover:bg-bg-hover transition-colors">
                      <td className="px-6 py-4 font-medium text-text-primary">{cand.name}</td>
                      <td className="px-6 py-4 hidden md:table-cell">{cand.email}</td>
                      <td className="px-6 py-4 hidden lg:table-cell">{cand.location || '—'}</td>
                      <td className="px-6 py-4 hidden md:table-cell">{cand.years_exp ? `${cand.years_exp} yrs` : '—'}</td>
                      <td className="px-6 py-4 text-right space-x-3">
                        <Link href={`/candidates/${cand.id}`} className="text-text-secondary hover:text-text-primary font-medium text-xs transition-colors">
                          View Details
                        </Link>
                        <Link href={`/candidates/${cand.id}`} className="bg-accent/10 text-accent hover:bg-accent/20 px-3 py-1.5 rounded text-xs font-medium transition-colors">
                          Auto Apply
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
