'use client'

import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import Link from 'next/link'
import { api, Candidate } from '@/lib/api'
import { Plus, Search, User, Mail, MapPin, Briefcase, ChevronRight, RefreshCw, UserCheck, Shield } from 'lucide-react'
import { useCurrentUser } from '@/hooks/useCurrentUser'
import { useToast } from '@/components/ui/Toast'
import { useConfirm } from '@/components/ui/ConfirmDialog'

export default function CandidatesPage() {
  const queryClient = useQueryClient()
  const toast = useToast()
  const confirm = useConfirm()
  const [search, setSearch] = useState('')

  const { data: currentUser } = useCurrentUser()
  const isAdmin = currentUser?.role === 'admin'

  const { data: candidates = [], isLoading, isError, refetch } = useQuery({
    queryKey: ['candidates'],
    queryFn: () => api.getCandidates(),
  })

  // Fetch BD users list if admin (to populate assignment dropdowns)
  const { data: bdUsers } = useQuery({
    queryKey: ['admin-bd-users'],
    queryFn: () => api.getAdminUsers({ limit: 100 }),
    enabled: isAdmin,
  })

  // Mutation to re-assign candidate to a BD User
  const assignMutation = useMutation({
    mutationFn: ({ candidateId, userId }: { candidateId: string; userId: string }) =>
      api.updateCandidate(candidateId, { user_id: userId }),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['candidates'] })
      const assignedUser = bdUsers?.find(u => u.id === variables.userId)
      toast.success(`Assigned to ${assignedUser?.full_name || assignedUser?.email || 'BD User'}`)
    },
    onError: (err: unknown) => {
      const errorObj = err as { message?: string }
      toast.error(errorObj.message || 'Failed to assign candidate')
    },
  })

  const handleAssignChange = async (candidate: Candidate, newUserId: string) => {
    if (!newUserId || candidate.user_id === newUserId) return

    const targetUser = bdUsers?.find(u => u.id === newUserId)
    const userName = targetUser?.full_name || targetUser?.email || 'selected user'

    const isConfirmed = await confirm({
      title: 'Assign Candidate to BD User?',
      message: `Re-assign ${candidate.name} to ${userName}? This will give them full access to manage this candidate's application pipeline.`,
      confirmLabel: 'Assign Candidate',
      variant: 'default',
    })

    if (isConfirmed) {
      assignMutation.mutate({ candidateId: candidate.id, userId: newUserId })
    }
  }

  const filteredCandidates = candidates.filter(cand =>
    cand.name.toLowerCase().includes(search.toLowerCase()) ||
    cand.email.toLowerCase().includes(search.toLowerCase()) ||
    (cand.location && cand.location.toLowerCase().includes(search.toLowerCase())) ||
    (cand.title && cand.title.toLowerCase().includes(search.toLowerCase()))
  )

  return (
    <div className="space-y-6 max-w-7xl mx-auto animate-fade-in">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-bg-border pb-6">
        <div>
          <h1 className="page-title flex items-center gap-2">
            Candidates
            {isAdmin && (
              <span className="text-xs bg-accent/10 text-accent font-semibold px-2.5 py-0.5 rounded-full border border-accent/20">
                Admin Mode
              </span>
            )}
          </h1>
          <p className="page-subtitle">
            {isAdmin 
              ? 'Manage candidate profiles and assign candidates to BD users across the platform.' 
              : 'Manage candidate profiles and track automated application pipelines.'}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <div className="relative">
            <Search className="w-4 h-4 text-text-muted absolute left-3 top-1/2 -translate-y-1/2" />
            <input
              type="text"
              placeholder="Search candidates..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="input pl-9 w-60"
            />
          </div>
          <Link href="/candidates/new" className="btn-primary">
            <Plus className="w-4 h-4" />
            Add Candidate
          </Link>
        </div>
      </div>

      {isLoading ? (
        <div className="space-y-3">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="skeleton h-16 rounded-xl" />
          ))}
        </div>
      ) : isError ? (
        <div className="p-8 text-center bg-danger/10 border border-danger/20 rounded-xl">
          <p className="text-danger text-sm font-medium mb-3">Failed to load candidates.</p>
          <button onClick={() => refetch()} className="btn-secondary !text-xs inline-flex items-center gap-1.5">
            <RefreshCw className="w-3.5 h-3.5" /> Retry
          </button>
        </div>
      ) : filteredCandidates.length === 0 ? (
        <div className="p-12 text-center bg-bg-card border border-bg-border rounded-2xl shadow-sm">
          <User className="w-12 h-12 text-text-muted mx-auto mb-3 opacity-40" />
          <p className="text-text-primary font-semibold text-base mb-1">
            {search ? 'No matching candidates found' : 'No candidates registered'}
          </p>
          <p className="text-text-muted text-xs mb-6 max-w-sm mx-auto">
            {search ? 'Try adjusting your search filter.' : 'Add your first candidate to start the automated application pipeline.'}
          </p>
          {!search && (
            <Link href="/candidates/new" className="btn-primary">
              <Plus className="w-4 h-4" />
              Add Candidate
            </Link>
          )}
        </div>
      ) : (
        <div className="card overflow-hidden bg-bg-card border border-bg-border rounded-xl shadow-sm">
          {/* Mobile view */}
          <div className="sm:hidden divide-y divide-bg-border">
            {filteredCandidates.map((cand) => (
              <div key={cand.id} className="p-4 hover:bg-bg-hover transition-colors">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="font-semibold text-text-primary text-sm truncate">{cand.name}</p>
                    <p className="text-xs text-text-muted truncate mt-0.5 flex items-center gap-1">
                      <Mail className="w-3 h-3 shrink-0" /> {cand.email}
                    </p>
                    <p className="text-xs text-text-muted mt-1 flex items-center gap-2">
                      <span>{cand.location || 'Remote'}</span>
                      <span>•</span>
                      <span>{cand.years_exp ? `${cand.years_exp} yrs exp` : 'Exp not set'}</span>
                    </p>

                    {/* Admin assignment dropdown for mobile */}
                    {isAdmin && bdUsers && bdUsers.length > 0 && (
                      <div className="mt-3 pt-2 border-t border-bg-border flex items-center gap-2">
                        <UserCheck className="w-3.5 h-3.5 text-text-muted shrink-0" />
                        <select
                          value={cand.user_id || ''}
                          onChange={(e) => handleAssignChange(cand, e.target.value)}
                          className="input !py-1 !text-xs !w-full"
                          disabled={assignMutation.isPending}
                        >
                          <option value="">-- Assign BD User --</option>
                          {bdUsers.map((u) => (
                            <option key={u.id} value={u.id}>
                              {u.full_name ? `${u.full_name} (${u.email})` : u.email}
                            </option>
                          ))}
                        </select>
                      </div>
                    )}
                  </div>
                  <Link href={`/candidates/${cand.id}`} className="btn-secondary !py-1.5 !px-3 !text-xs shrink-0">
                    View <ChevronRight className="w-3 h-3 ml-0.5" />
                  </Link>
                </div>
              </div>
            ))}
          </div>

          {/* Desktop table */}
          <div className="hidden sm:block overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="bg-bg-primary text-text-muted uppercase text-xs tracking-wider border-b border-bg-border">
                <tr>
                  <th className="px-6 py-4 font-medium">Candidate Name</th>
                  <th className="px-6 py-4 font-medium">Contact</th>
                  <th className="px-6 py-4 font-medium">Target Title / Location</th>
                  {isAdmin && <th className="px-6 py-4 font-medium">Assigned BD User</th>}
                  <th className="px-6 py-4 font-medium">Experience</th>
                  <th className="px-6 py-4 font-medium text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-bg-border">
                {filteredCandidates.map((cand) => {
                  const assignedUser = bdUsers?.find(u => u.id === cand.user_id)
                  return (
                    <tr key={cand.id} className="hover:bg-bg-hover transition-colors">
                      <td className="px-6 py-4">
                        <div className="font-semibold text-text-primary">{cand.name}</div>
                      </td>
                      <td className="px-6 py-4 text-text-secondary text-xs">
                        <div className="flex items-center gap-1.5">
                          <Mail className="w-3.5 h-3.5 text-text-muted" />
                          {cand.email}
                        </div>
                      </td>
                      <td className="px-6 py-4 text-text-secondary text-xs">
                        <div className="font-medium text-text-primary">{cand.title || 'General'}</div>
                        <div className="text-text-muted flex items-center gap-1 mt-0.5">
                          <MapPin className="w-3 h-3" /> {cand.location || 'Remote'}
                        </div>
                      </td>

                      {/* Admin BD User Assignment Dropdown */}
                      {isAdmin && (
                        <td className="px-6 py-4 text-xs">
                          {bdUsers && bdUsers.length > 0 ? (
                            <div className="flex items-center gap-1.5">
                              <select
                                value={cand.user_id || ''}
                                onChange={(e) => handleAssignChange(cand, e.target.value)}
                                className="input !py-1.5 !px-2.5 !text-xs !w-auto min-w-[160px] font-medium"
                                disabled={assignMutation.isPending}
                              >
                                <option value="">-- Unassigned --</option>
                                {bdUsers.map((u) => (
                                  <option key={u.id} value={u.id}>
                                    {u.full_name ? `${u.full_name} (${u.role === 'admin' ? 'Admin' : 'BD User'})` : u.email}
                                  </option>
                                ))}
                              </select>
                            </div>
                          ) : (
                            <span className="text-text-muted italic">Loading users...</span>
                          )}
                        </td>
                      )}

                      <td className="px-6 py-4 text-text-muted text-xs">
                        {cand.years_exp ? `${cand.years_exp} years` : '—'}
                      </td>
                      <td className="px-6 py-4 text-right">
                        <div className="flex items-center justify-end gap-2">
                          <Link
                            href={`/candidates/${cand.id}`}
                            className="btn-secondary !py-1.5 !px-3 !text-xs"
                          >
                            View Profile
                          </Link>
                          <Link
                            href={`/candidates/${cand.id}`}
                            className="btn-primary !py-1.5 !px-3 !text-xs"
                          >
                            <Briefcase className="w-3.5 h-3.5" />
                            Auto Apply
                          </Link>
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
