'use client'

import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Search, MoreVertical, Loader2, CheckCircle2, AlertCircle } from 'lucide-react'
import { api, BDUser } from '@/lib/api'
import { formatDistanceToNow } from '@/lib/utils'

export default function UserManagementPage() {
  const queryClient = useQueryClient()
  const [isModalOpen, setIsModalOpen] = useState(false)
  const [search, setSearch] = useState('')

  const { data: users, isLoading } = useQuery({
    queryKey: ['admin-users'],
    queryFn: () => api.getAdminUsers()
  })

  const updateRoleMutation = useMutation({
    mutationFn: ({ id, role }: { id: string, role: string }) => api.updateUserRole(id, role),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin-users'] })
      alert("Role updated successfully")
    },
    onError: () => {
      alert("Failed to update role")
    }
  })

  const handleRoleChange = (userId: string, currentRole: string, newRole: string, userName: string) => {
    if (currentRole === newRole) return
    if (confirm(`Change ${userName || 'this user'}'s role to ${newRole}?`)) {
      updateRoleMutation.mutate({ id: userId, role: newRole })
    }
  }

  const filteredUsers = users?.filter(u => 
    u.email.toLowerCase().includes(search.toLowerCase()) || 
    (u.full_name && u.full_name.toLowerCase().includes(search.toLowerCase()))
  )

  return (
    <div className="space-y-6 max-w-7xl mx-auto animate-in fade-in duration-500">
      
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-text-primary tracking-tight">User Management</h1>
          <p className="text-sm text-text-muted mt-1">Manage admin and BD users across the platform.</p>
        </div>
        
        <div className="flex items-center gap-3">
          <div className="relative">
            <Search className="w-4 h-4 text-text-muted absolute left-3 top-1/2 -translate-y-1/2" />
            <input 
              type="text" 
              placeholder="Search users..." 
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="pl-9 pr-4 py-2 bg-bg-primary border border-bg-border rounded-lg text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent transition-colors w-64"
            />
          </div>
          <button 
            onClick={() => setIsModalOpen(true)}
            className="flex items-center gap-2 px-4 py-2 bg-accent hover:bg-accent-hover text-white rounded-lg text-sm font-medium transition-colors"
          >
            <Plus className="w-4 h-4" />
            Create User
          </button>
        </div>
      </div>

      {/* Table Area */}
      <div className="bg-bg-secondary border border-bg-border rounded-xl overflow-hidden shadow-sm">
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="bg-bg-primary/50 text-text-muted uppercase text-xs tracking-wider border-b border-bg-border">
              <tr>
                <th className="px-6 py-4 font-medium">Name & Email</th>
                <th className="px-6 py-4 font-medium">Role</th>
                <th className="px-6 py-4 font-medium">Status</th>
                <th className="px-6 py-4 font-medium">Created</th>
                <th className="px-6 py-4 font-medium text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-bg-border">
              {isLoading ? (
                Array.from({ length: 5 }).map((_, i) => (
                  <tr key={i}>
                    <td className="px-6 py-4">
                      <div className="h-4 bg-bg-border rounded w-32 animate-pulse mb-2"></div>
                      <div className="h-3 bg-bg-border rounded w-48 animate-pulse"></div>
                    </td>
                    <td className="px-6 py-4"><div className="h-6 bg-bg-border rounded w-20 animate-pulse"></div></td>
                    <td className="px-6 py-4"><div className="h-6 bg-bg-border rounded w-16 animate-pulse"></div></td>
                    <td className="px-6 py-4"><div className="h-4 bg-bg-border rounded w-24 animate-pulse"></div></td>
                    <td className="px-6 py-4 text-right"><div className="h-8 bg-bg-border rounded w-8 animate-pulse ml-auto"></div></td>
                  </tr>
                ))
              ) : !filteredUsers || filteredUsers.length === 0 ? (
                <tr>
                  <td colSpan={5} className="px-6 py-16 text-center">
                    <div className="flex flex-col items-center justify-center">
                      <h3 className="text-lg font-medium text-text-primary mb-1">No users found</h3>
                      <p className="text-text-muted text-sm max-w-sm mb-4">
                        {search ? "No users match your search query." : "There are no BD users created yet."}
                      </p>
                      {!search && (
                        <button 
                          onClick={() => setIsModalOpen(true)}
                          className="px-4 py-2 bg-bg-primary border border-bg-border hover:bg-bg-hover text-text-primary rounded-md transition-colors text-sm"
                        >
                          Create your first user
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ) : (
                filteredUsers.map((user) => (
                  <tr key={user.id} className="hover:bg-bg-hover transition-colors">
                    <td className="px-6 py-4">
                      <div className="font-medium text-text-primary">{user.full_name || 'Unknown User'}</div>
                      <div className="text-text-muted mt-0.5">{user.email}</div>
                    </td>
                    <td className="px-6 py-4">
                      <select 
                        value={user.role}
                        onChange={(e) => handleRoleChange(user.id, user.role, e.target.value, user.full_name || user.email)}
                        className="bg-bg-primary border border-bg-border text-text-primary text-xs rounded-md px-2 py-1 focus:outline-none focus:border-accent"
                        disabled={updateRoleMutation.isPending}
                      >
                        <option value="bd_user">BD User</option>
                        <option value="admin">Admin</option>
                      </select>
                    </td>
                    <td className="px-6 py-4">
                      <span className="inline-flex items-center gap-1.5 px-2 py-1 bg-green-500/10 border border-green-500/20 text-green-500 text-xs rounded-md font-medium">
                        <span className="w-1.5 h-1.5 rounded-full bg-green-500"></span>
                        Active
                      </span>
                    </td>
                    <td className="px-6 py-4 text-text-muted whitespace-nowrap">
                      {user.created_at ? formatDistanceToNow(user.created_at) + ' ago' : 'Unknown'}
                    </td>
                    <td className="px-6 py-4 text-right">
                      <button className="p-1.5 text-text-muted hover:text-text-primary rounded-md hover:bg-bg-primary transition-colors">
                        <MoreVertical className="w-4 h-4" />
                      </button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Create User Modal */}
      {isModalOpen && <CreateUserModal onClose={() => setIsModalOpen(false)} />}
    </div>
  )
}

function CreateUserModal({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState('bd_user')
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () => api.createBDUser({ name, email, password, role }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin-users'] })
      alert("User created successfully!")
      onClose()
    },
    onError: (err: any) => {
      setError(err.message || "Failed to create user")
    }
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    mutation.mutate()
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4">
      <div className="w-full max-w-md bg-bg-secondary rounded-2xl shadow-2xl border border-bg-border overflow-hidden animate-in zoom-in-95 duration-200">
        <div className="p-6 border-b border-bg-border flex items-center justify-between">
          <h2 className="text-lg font-semibold text-text-primary">Create New User</h2>
          <button onClick={onClose} className="text-text-muted hover:text-text-primary transition-colors">✕</button>
        </div>
        
        <div className="p-6">
          {error && (
            <div className="mb-4 p-3 bg-danger/10 border border-danger/20 rounded-lg text-sm text-danger flex items-start gap-2">
              <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
              <span>{error}</span>
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-text-secondary mb-1.5">Full Name</label>
              <input 
                type="text" required value={name} onChange={e => setName(e.target.value)}
                className="w-full px-3 py-2 bg-bg-primary border border-bg-border rounded-lg text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent"
                placeholder="John Doe"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-text-secondary mb-1.5">Email Address</label>
              <input 
                type="email" required value={email} onChange={e => setEmail(e.target.value)}
                className="w-full px-3 py-2 bg-bg-primary border border-bg-border rounded-lg text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent"
                placeholder="john@example.com"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-text-secondary mb-1.5">Password</label>
              <input 
                type="password" required value={password} onChange={e => setPassword(e.target.value)}
                className="w-full px-3 py-2 bg-bg-primary border border-bg-border rounded-lg text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent"
                placeholder="••••••••"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-text-secondary mb-1.5">Role</label>
              <select 
                value={role} onChange={e => setRole(e.target.value)}
                className="w-full px-3 py-2 bg-bg-primary border border-bg-border rounded-lg text-text-primary focus:outline-none focus:border-accent"
              >
                <option value="bd_user">BD User</option>
                <option value="admin">Admin</option>
              </select>
            </div>

            <div className="pt-4 flex gap-3">
              <button 
                type="button" onClick={onClose}
                className="flex-1 px-4 py-2 bg-bg-primary border border-bg-border hover:bg-bg-hover text-text-primary rounded-lg font-medium transition-colors"
              >
                Cancel
              </button>
              <button 
                type="submit" disabled={mutation.isPending}
                className="flex-1 px-4 py-2 bg-accent hover:bg-accent-hover text-white rounded-lg font-medium transition-colors flex justify-center items-center"
              >
                {mutation.isPending ? <Loader2 className="w-5 h-5 animate-spin" /> : 'Create User'}
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>
  )
}
