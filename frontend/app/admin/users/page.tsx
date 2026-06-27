'use client'

import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Search, MoreVertical, Loader2, CheckCircle2, AlertCircle, Edit2, Key, Trash2, ChevronLeft, ChevronRight } from 'lucide-react'
import { api, BDUser } from '@/lib/api'
import { formatDistanceToNow } from '@/lib/utils'

export default function UserManagementPage() {
  const queryClient = useQueryClient()
  const [isModalOpen, setIsModalOpen] = useState(false)
  const [isMergeModalOpen, setIsMergeModalOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [activeMenuUserId, setActiveMenuUserId] = useState<string | null>(null)
  const [editingUser, setEditingUser] = useState<BDUser | null>(null)
  const [changingPasswordUser, setChangingPasswordUser] = useState<BDUser | null>(null)

  const [page, setPage] = useState(0)
  const limit = 10

  const { data: users, isLoading } = useQuery({
    queryKey: ['admin-users', page, limit],
    queryFn: () => api.getAdminUsers({ skip: page * limit, limit })
  })

  const { data: countData } = useQuery({
    queryKey: ['admin-users-count'],
    queryFn: () => api.getAdminUsersCount()
  })

  const totalCount = countData?.total_count ?? 0
  const totalPages = Math.ceil(totalCount / limit)

  // Reset page to 0 on search change
  useEffect(() => {
    setPage(0)
  }, [search])

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

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteUser(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin-users'] })
      queryClient.invalidateQueries({ queryKey: ['admin-users-count'] })
      alert("User deleted successfully")
    },
    onError: (err: any) => {
      alert(err.message || "Failed to delete user")
    }
  })

  const handleRoleChange = (userId: string, currentRole: string, newRole: string, userName: string) => {
    if (currentRole === newRole) return
    if (confirm(`Change ${userName || 'this user'}'s role to ${newRole}?`)) {
      updateRoleMutation.mutate({ id: userId, role: newRole })
    }
  }

  const handleDeleteClick = (user: BDUser) => {
    if (confirm(`Are you sure you want to permanently delete ${user.full_name || user.email}? This will delete the user from both Supabase Auth and the local database.`)) {
      deleteMutation.mutate(user.id)
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
            onClick={() => setIsMergeModalOpen(true)}
            className="flex items-center gap-2 px-4 py-2 bg-bg-primary border border-bg-border hover:bg-bg-hover text-text-primary rounded-lg text-sm font-medium transition-colors"
          >
            Merge Profiles
          </button>
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
                filteredUsers.map((user, index) => (
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
                    <td className="px-6 py-4 text-right relative">
                      <button 
                        onClick={() => setActiveMenuUserId(activeMenuUserId === user.id ? null : user.id)}
                        className="p-1.5 text-text-muted hover:text-text-primary rounded-md hover:bg-bg-primary transition-colors"
                      >
                        <MoreVertical className="w-4 h-4" />
                      </button>

                      {activeMenuUserId === user.id && (
                        <>
                          <div 
                            className="fixed inset-0 z-10" 
                            onClick={() => setActiveMenuUserId(null)}
                          />
                          <div className={`absolute right-6 w-44 bg-bg-secondary border border-bg-border rounded-lg shadow-lg py-1 z-20 text-left animate-in fade-in duration-100 ${
                            filteredUsers && index >= filteredUsers.length - 2
                              ? 'bottom-full mb-1 slide-in-from-bottom-1'
                              : 'top-full mt-1 slide-in-from-top-1'
                          }`}>
                            <button
                              onClick={() => {
                                setEditingUser(user)
                                setActiveMenuUserId(null)
                              }}
                              className="w-full px-4 py-2 text-xs text-text-primary hover:bg-bg-hover flex items-center gap-2 transition-colors"
                            >
                              <Edit2 className="w-3.5 h-3.5 text-text-muted" />
                              Edit User
                            </button>
                            <button
                              onClick={() => {
                                setChangingPasswordUser(user)
                                setActiveMenuUserId(null)
                              }}
                              className="w-full px-4 py-2 text-xs text-text-primary hover:bg-bg-hover flex items-center gap-2 transition-colors"
                            >
                              <Key className="w-3.5 h-3.5 text-text-muted" />
                              Change Password
                            </button>
                            <div className="h-px bg-bg-border my-1" />
                            <button
                              onClick={() => {
                                handleDeleteClick(user)
                                setActiveMenuUserId(null)
                              }}
                              className="w-full px-4 py-2 text-xs text-danger hover:bg-danger/10 flex items-center gap-2 transition-colors text-left"
                            >
                              <Trash2 className="w-3.5 h-3.5" />
                              Delete User
                            </button>
                          </div>
                        </>
                      )}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {/* Pagination */}
        <div className="px-6 py-4 border-t border-bg-border flex items-center justify-between">
          <div className="text-sm text-text-muted">
            Showing page {page + 1} of {Math.max(1, totalPages)} ({totalCount} total users)
          </div>
          <div className="flex items-center gap-2">
            <button 
              onClick={() => setPage(p => Math.max(0, p - 1))}
              disabled={page === 0 || isLoading}
              className="p-1.5 rounded-md border border-bg-border text-text-primary hover:bg-bg-hover disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              <ChevronLeft className="w-4 h-4" />
            </button>
            <button 
              onClick={() => setPage(p => p + 1)}
              disabled={page + 1 >= totalPages || isLoading}
              className="p-1.5 rounded-md border border-bg-border text-text-primary hover:bg-bg-hover disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              <ChevronRight className="w-4 h-4" />
            </button>
          </div>
        </div>
      </div>

      {/* Create User Modal */}
      {isModalOpen && <CreateUserModal onClose={() => setIsModalOpen(false)} />}

      {/* Edit User Modal */}
      {editingUser && (
        <EditUserModal 
          user={editingUser} 
          onClose={() => setEditingUser(null)} 
        />
      )}

      {/* Change Password Modal */}
      {changingPasswordUser && (
        <ChangePasswordModal 
          user={changingPasswordUser} 
          onClose={() => setChangingPasswordUser(null)} 
        />
      )}

      {/* Merge Profiles Modal */}
      {isMergeModalOpen && (
        <MergeUsersModal 
          users={users || []}
          onClose={() => setIsMergeModalOpen(false)}
        />
      )}
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
      queryClient.invalidateQueries({ queryKey: ['admin-users-count'] })
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

function EditUserModal({ user, onClose }: { user: BDUser, onClose: () => void }) {
  const queryClient = useQueryClient()
  const [name, setName] = useState(user.full_name || '')
  const [email, setEmail] = useState(user.email || '')
  const [role, setRole] = useState(user.role || 'bd_user')
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () => api.updateUser(user.id, { name, email, role }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin-users'] })
      alert("User updated successfully!")
      onClose()
    },
    onError: (err: any) => {
      setError(err.message || "Failed to update user")
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
          <h2 className="text-lg font-semibold text-text-primary">Edit User Details</h2>
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
                {mutation.isPending ? <Loader2 className="w-5 h-5 animate-spin" /> : 'Save Changes'}
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>
  )
}

function ChangePasswordModal({ user, onClose }: { user: BDUser, onClose: () => void }) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () => api.updateUserPassword(user.id, password),
    onSuccess: () => {
      alert("Password updated successfully!")
      onClose()
    },
    onError: (err: any) => {
      setError(err.message || "Failed to change password")
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
          <h2 className="text-lg font-semibold text-text-primary">Change Password</h2>
          <button onClick={onClose} className="text-text-muted hover:text-text-primary transition-colors">✕</button>
        </div>
        
        <div className="p-6">
          <div className="mb-4 text-xs text-text-muted">
            Changing password for <strong>{user.full_name || user.email}</strong>
          </div>
          
          {error && (
            <div className="mb-4 p-3 bg-danger/10 border border-danger/20 rounded-lg text-sm text-danger flex items-start gap-2">
              <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
              <span>{error}</span>
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-text-secondary mb-1.5">New Password</label>
              <input 
                type="password" required value={password} onChange={e => setPassword(e.target.value)}
                className="w-full px-3 py-2 bg-bg-primary border border-bg-border rounded-lg text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent"
                placeholder="••••••••"
              />
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
                {mutation.isPending ? <Loader2 className="w-5 h-5 animate-spin" /> : 'Update Password'}
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>
  )
}

function MergeUsersModal({ users, onClose }: { users: BDUser[], onClose: () => void }) {
  const queryClient = useQueryClient()
  const [sourceUserId, setSourceUserId] = useState('')
  const [targetUserId, setTargetUserId] = useState('')
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () => api.mergeUsers(sourceUserId, targetUserId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin-users'] })
      queryClient.invalidateQueries({ queryKey: ['admin-users-count'] })
      alert("Users merged and candidates re-assigned successfully!")
      onClose()
    },
    onError: (err: any) => {
      setError(err.message || "Failed to merge user profiles")
    }
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)

    if (!sourceUserId || !targetUserId) {
      setError("Please select both source and target users")
      return
    }

    if (sourceUserId === targetUserId) {
      setError("Cannot merge a user profile into itself")
      return
    }

    const sourceUser = users.find(u => u.id === sourceUserId)
    const targetUser = users.find(u => u.id === targetUserId)

    if (confirm(`Are you absolutely sure you want to merge ${sourceUser?.full_name || sourceUser?.email} into ${targetUser?.full_name || targetUser?.email}? All candidates under the source profile will be re-assigned, and the source user profile will be permanently deleted.`)) {
      mutation.mutate()
    }
  }

  // Filter target user options to exclude the selected source user
  const availableTargetUsers = users.filter(u => u.id !== sourceUserId)

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4">
      <div className="w-full max-w-md bg-bg-secondary rounded-2xl shadow-2xl border border-bg-border overflow-hidden animate-in zoom-in-95 duration-200">
        <div className="p-6 border-b border-bg-border flex items-center justify-between">
          <h2 className="text-lg font-semibold text-text-primary">Merge BD User Profiles</h2>
          <button onClick={onClose} className="text-text-muted hover:text-text-primary transition-colors">✕</button>
        </div>
        
        <div className="p-6">
          {error && (
            <div className="mb-4 p-3 bg-danger/10 border border-danger/20 rounded-lg text-sm text-danger flex items-start gap-2">
              <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
              <span>{error}</span>
            </div>
          )}

          <div className="mb-5 p-3.5 bg-warning/10 border border-warning/20 text-xs text-warning rounded-lg flex items-start gap-2.5">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            <div>
              <p className="font-semibold mb-0.5">Warning: Permanent Action</p>
              <p className="leading-relaxed">This will transfer all candidate relationships from the source user to the target user, then delete the source user from the system.</p>
            </div>
          </div>

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-text-secondary mb-1.5">Source User (User leaving the company)</label>
              <select 
                value={sourceUserId} 
                onChange={e => {
                  setSourceUserId(e.target.value)
                  if (e.target.value === targetUserId) setTargetUserId('')
                }}
                className="w-full px-3 py-2 bg-bg-primary border border-bg-border rounded-lg text-text-primary focus:outline-none focus:border-accent text-sm"
                required
              >
                <option value="">-- Select Source User --</option>
                {users.map(u => (
                  <option key={u.id} value={u.id}>
                    {u.full_name ? `${u.full_name} (${u.email})` : u.email} [{u.role === 'admin' ? 'Admin' : 'BD User'}]
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label className="block text-sm font-medium text-text-secondary mb-1.5">Target User (User receiving candidates)</label>
              <select 
                value={targetUserId} 
                onChange={e => setTargetUserId(e.target.value)}
                className="w-full px-3 py-2 bg-bg-primary border border-bg-border rounded-lg text-text-primary focus:outline-none focus:border-accent text-sm"
                required
                disabled={!sourceUserId}
              >
                <option value="">-- Select Target User --</option>
                {availableTargetUsers.map(u => (
                  <option key={u.id} value={u.id}>
                    {u.full_name ? `${u.full_name} (${u.email})` : u.email} [{u.role === 'admin' ? 'Admin' : 'BD User'}]
                  </option>
                ))}
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
                type="submit" 
                disabled={mutation.isPending || !sourceUserId || !targetUserId}
                className="flex-1 px-4 py-2 bg-accent hover:bg-accent-hover text-white rounded-lg font-medium transition-colors flex justify-center items-center"
              >
                {mutation.isPending ? <Loader2 className="w-5 h-5 animate-spin" /> : 'Merge & Re-assign'}
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>
  )
}
