'use client'

import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Search, MoreVertical, Edit2, Key, Trash2, ChevronLeft, ChevronRight } from 'lucide-react'
import { api, BDUser } from '@/lib/api'
import { formatDistanceToNow } from '@/lib/utils'
import { useToast } from '@/components/ui/Toast'
import { useConfirm } from '@/components/ui/ConfirmDialog'
import {
  CreateUserModal,
  EditUserModal,
  ChangePasswordModal,
  MergeUsersModal,
} from '@/components/admin/UserModals'

export default function UserManagementPage() {
  const queryClient = useQueryClient()
  const toast = useToast()
  const confirm = useConfirm()

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
    queryFn: () => api.getAdminUsers({ skip: page * limit, limit }),
  })

  const { data: countData } = useQuery({
    queryKey: ['admin-users-count'],
    queryFn: () => api.getAdminUsersCount(),
  })

  const totalCount = countData?.total_count ?? 0
  const totalPages = Math.ceil(totalCount / limit)

  // Reset page to 0 on search change
  useEffect(() => {
    setPage(0)
  }, [search])

  const updateRoleMutation = useMutation({
    mutationFn: ({ id, role }: { id: string; role: string }) => api.updateUserRole(id, role),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin-users'] })
      toast.success('Role updated successfully!')
    },
    onError: () => {
      toast.error('Failed to update role.')
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteUser(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin-users'] })
      queryClient.invalidateQueries({ queryKey: ['admin-users-count'] })
      toast.success('User deleted successfully!')
    },
    onError: (err: unknown) => {
      const errorObj = err as { message?: string }
      toast.error(errorObj.message || 'Failed to delete user')
    },
  })

  const handleRoleChange = async (userId: string, currentRole: string, newRole: string, userName: string) => {
    if (currentRole === newRole) return
    const isConfirmed = await confirm({
      title: 'Change User Role?',
      message: `Change ${userName || 'this user'}'s role to ${newRole === 'admin' ? 'Admin' : 'BD User'}?`,
      confirmLabel: 'Update Role',
      variant: 'warning',
    })
    if (isConfirmed) {
      updateRoleMutation.mutate({ id: userId, role: newRole })
    }
  }

  const handleDeleteClick = async (user: BDUser) => {
    const isConfirmed = await confirm({
      title: 'Delete User Account?',
      message: `Permanently delete ${user.full_name || user.email}? This will delete the user from both Auth and Database.`,
      confirmLabel: 'Delete User',
      variant: 'danger',
    })
    if (isConfirmed) {
      deleteMutation.mutate(user.id)
    }
  }

  const filteredUsers = users?.filter(
    (u) =>
      u.email.toLowerCase().includes(search.toLowerCase()) ||
      (u.full_name && u.full_name.toLowerCase().includes(search.toLowerCase()))
  )

  return (
    <div className="space-y-6 max-w-7xl mx-auto animate-fade-in">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-bg-border pb-6">
        <div>
          <h1 className="page-title">User Management</h1>
          <p className="page-subtitle">Manage admin and BD users across the platform.</p>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <div className="relative">
            <Search className="w-4 h-4 text-text-muted absolute left-3 top-1/2 -translate-y-1/2" />
            <input
              type="text"
              placeholder="Search users..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="input pl-9 w-60"
            />
          </div>
          <button
            onClick={() => setIsMergeModalOpen(true)}
            className="btn-secondary"
          >
            Merge Profiles
          </button>
          <button
            onClick={() => setIsModalOpen(true)}
            className="btn-primary"
          >
            <Plus className="w-4 h-4" />
            Create User
          </button>
        </div>
      </div>

      {/* Table Area */}
      <div className="card bg-bg-card border border-bg-border rounded-xl overflow-hidden shadow-sm">
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="bg-bg-primary text-text-muted uppercase text-xs tracking-wider border-b border-bg-border">
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
                        {search ? 'No users match your search query.' : 'There are no BD users created yet.'}
                      </p>
                      {!search && (
                        <button
                          onClick={() => setIsModalOpen(true)}
                          className="btn-secondary"
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
                        className="input !py-1 !text-xs !w-auto"
                        disabled={updateRoleMutation.isPending}
                      >
                        <option value="bd_user">BD User</option>
                        <option value="admin">Admin</option>
                      </select>
                    </td>
                    <td className="px-6 py-4">
                      <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 bg-success/10 border border-success/20 text-success text-xs rounded-full font-semibold">
                        <span className="w-1.5 h-1.5 rounded-full bg-success"></span>
                        Active
                      </span>
                    </td>
                    <td className="px-6 py-4 text-text-muted whitespace-nowrap text-xs">
                      {user.created_at ? formatDistanceToNow(user.created_at) + ' ago' : 'Unknown'}
                    </td>
                    <td className="px-6 py-4 text-right relative">
                      <button
                        onClick={() => setActiveMenuUserId(activeMenuUserId === user.id ? null : user.id)}
                        className="p-1.5 text-text-muted hover:text-text-primary rounded-md hover:bg-bg-primary transition-colors"
                        aria-label="User actions"
                      >
                        <MoreVertical className="w-4 h-4" />
                      </button>

                      {activeMenuUserId === user.id && (
                        <>
                          <div
                            className="fixed inset-0 z-10"
                            onClick={() => setActiveMenuUserId(null)}
                          />
                          <div
                            className={`absolute right-6 w-44 bg-bg-card border border-bg-border rounded-xl shadow-xl py-1 z-20 text-left animate-scale-up ${
                              filteredUsers && index >= filteredUsers.length - 2
                                ? 'bottom-full mb-1'
                                : 'top-full mt-1'
                            }`}
                          >
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
                              className="w-full px-4 py-2 text-xs text-danger hover:bg-danger/10 flex items-center gap-2 transition-colors text-left font-medium"
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
          <div className="text-xs text-text-muted">
            Showing page {page + 1} of {Math.max(1, totalPages)} ({totalCount} total users)
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              disabled={page === 0 || isLoading}
              className="btn-secondary !p-1.5"
              aria-label="Previous page"
            >
              <ChevronLeft className="w-4 h-4" />
            </button>
            <button
              onClick={() => setPage((p) => p + 1)}
              disabled={page + 1 >= totalPages || isLoading}
              className="btn-secondary !p-1.5"
              aria-label="Next page"
            >
              <ChevronRight className="w-4 h-4" />
            </button>
          </div>
        </div>
      </div>

      {/* Modals */}
      <CreateUserModal open={isModalOpen} onClose={() => setIsModalOpen(false)} />
      <EditUserModal user={editingUser} onClose={() => setEditingUser(null)} />
      <ChangePasswordModal user={changingPasswordUser} onClose={() => setChangingPasswordUser(null)} />
      <MergeUsersModal
        open={isMergeModalOpen}
        users={users || []}
        onClose={() => setIsMergeModalOpen(false)}
      />
    </div>
  )
}
