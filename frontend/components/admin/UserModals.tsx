'use client'

import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, BDUser } from '@/lib/api'
import { Modal } from '@/components/ui/Modal'
import { useToast } from '@/components/ui/Toast'
import { useConfirm } from '@/components/ui/ConfirmDialog'
import { AlertCircle, Loader2 } from 'lucide-react'

// ── 1. Create User Modal ──────────────────────────────────────────────────

export function CreateUserModal({
  open,
  onClose,
}: {
  open: boolean
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const toast = useToast()

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
      toast.success('User created successfully!')
      onClose()
    },
    onError: (err: unknown) => {
      const errorObj = err as { message?: string }
      setError(errorObj.message || 'Failed to create user')
    },
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (password.length < 6) {
      setError('Password must be at least 6 characters long.')
      return
    }
    setError(null)
    mutation.mutate()
  }

  return (
    <Modal open={open} onClose={onClose} title="Create New User" size="md">
      {error && (
        <div className="mb-4 p-3 bg-danger/10 border border-danger/20 rounded-lg text-sm text-danger flex items-start gap-2">
          <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
          <span>{error}</span>
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label className="input-label">Full Name</label>
          <input
            type="text"
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="input"
            placeholder="John Doe"
          />
        </div>
        <div>
          <label className="input-label">Email Address</label>
          <input
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="input"
            placeholder="john@example.com"
          />
        </div>
        <div>
          <label className="input-label">Password</label>
          <input
            type="password"
            required
            minLength={6}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="input"
            placeholder="••••••••"
          />
        </div>
        <div>
          <label className="input-label">Role</label>
          <select
            value={role}
            onChange={(e) => setRole(e.target.value)}
            className="input"
          >
            <option value="bd_user">BD User</option>
            <option value="admin">Admin</option>
          </select>
        </div>

        <div className="pt-4 flex gap-3">
          <button type="button" onClick={onClose} className="btn-secondary flex-1">
            Cancel
          </button>
          <button type="submit" disabled={mutation.isPending} className="btn-primary flex-1">
            {mutation.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Create User'}
          </button>
        </div>
      </form>
    </Modal>
  )
}

// ── 2. Edit User Modal ────────────────────────────────────────────────────

export function EditUserModal({
  user,
  onClose,
}: {
  user: BDUser | null
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const toast = useToast()

  const [name, setName] = useState(user?.full_name || '')
  const [email, setEmail] = useState(user?.email || '')
  const [role, setRole] = useState(user?.role || 'bd_user')
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () => {
      if (!user) throw new Error('No user selected')
      return api.updateUser(user.id, { name, email, role })
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin-users'] })
      toast.success('User updated successfully!')
      onClose()
    },
    onError: (err: unknown) => {
      const errorObj = err as { message?: string }
      setError(errorObj.message || 'Failed to update user')
    },
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    mutation.mutate()
  }

  return (
    <Modal open={!!user} onClose={onClose} title="Edit User Details" size="md">
      {error && (
        <div className="mb-4 p-3 bg-danger/10 border border-danger/20 rounded-lg text-sm text-danger flex items-start gap-2">
          <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
          <span>{error}</span>
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label className="input-label">Full Name</label>
          <input
            type="text"
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="input"
            placeholder="John Doe"
          />
        </div>
        <div>
          <label className="input-label">Email Address</label>
          <input
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="input"
            placeholder="john@example.com"
          />
        </div>
        <div>
          <label className="input-label">Role</label>
          <select value={role} onChange={(e) => setRole(e.target.value)} className="input">
            <option value="bd_user">BD User</option>
            <option value="admin">Admin</option>
          </select>
        </div>

        <div className="pt-4 flex gap-3">
          <button type="button" onClick={onClose} className="btn-secondary flex-1">
            Cancel
          </button>
          <button type="submit" disabled={mutation.isPending} className="btn-primary flex-1">
            {mutation.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Save Changes'}
          </button>
        </div>
      </form>
    </Modal>
  )
}

// ── 3. Change Password Modal ──────────────────────────────────────────────

export function ChangePasswordModal({
  user,
  onClose,
}: {
  user: BDUser | null
  onClose: () => void
}) {
  const toast = useToast()
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () => {
      if (!user) throw new Error('No user selected')
      return api.updateUserPassword(user.id, password)
    },
    onSuccess: () => {
      toast.success('Password updated successfully!')
      onClose()
    },
    onError: (err: unknown) => {
      const errorObj = err as { message?: string }
      setError(errorObj.message || 'Failed to change password')
    },
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (password.length < 6) {
      setError('Password must be at least 6 characters.')
      return
    }
    setError(null)
    mutation.mutate()
  }

  return (
    <Modal open={!!user} onClose={onClose} title="Change Password" size="md">
      <div className="mb-4 text-xs text-text-muted">
        Changing password for <strong>{user?.full_name || user?.email}</strong>
      </div>

      {error && (
        <div className="mb-4 p-3 bg-danger/10 border border-danger/20 rounded-lg text-sm text-danger flex items-start gap-2">
          <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
          <span>{error}</span>
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label className="input-label">New Password</label>
          <input
            type="password"
            required
            minLength={6}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="input"
            placeholder="••••••••"
          />
        </div>

        <div className="pt-4 flex gap-3">
          <button type="button" onClick={onClose} className="btn-secondary flex-1">
            Cancel
          </button>
          <button type="submit" disabled={mutation.isPending} className="btn-primary flex-1">
            {mutation.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Update Password'}
          </button>
        </div>
      </form>
    </Modal>
  )
}

// ── 4. Merge Users Modal ──────────────────────────────────────────────────

export function MergeUsersModal({
  open,
  users,
  onClose,
}: {
  open: boolean
  users: BDUser[]
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const confirm = useConfirm()

  const [sourceUserId, setSourceUserId] = useState('')
  const [targetUserId, setTargetUserId] = useState('')
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () => api.mergeUsers(sourceUserId, targetUserId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin-users'] })
      queryClient.invalidateQueries({ queryKey: ['admin-users-count'] })
      toast.success('Users merged and candidates re-assigned successfully!')
      onClose()
    },
    onError: (err: unknown) => {
      const errorObj = err as { message?: string }
      setError(errorObj.message || 'Failed to merge user profiles')
    },
  })

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)

    if (!sourceUserId || !targetUserId) {
      setError('Please select both source and target users.')
      return
    }
    if (sourceUserId === targetUserId) {
      setError('Cannot merge a user profile into itself.')
      return
    }

    const sourceUser = users.find((u) => u.id === sourceUserId)
    const targetUser = users.find((u) => u.id === targetUserId)

    const isConfirmed = await confirm({
      title: 'Merge BD User Profiles?',
      message: `Transfer all candidate relationships from ${sourceUser?.full_name || sourceUser?.email} to ${targetUser?.full_name || targetUser?.email}? The source user will be permanently deleted.`,
      confirmLabel: 'Merge & Delete Source',
      variant: 'warning',
    })

    if (isConfirmed) {
      mutation.mutate()
    }
  }

  const availableTargetUsers = users.filter((u) => u.id !== sourceUserId)

  return (
    <Modal open={open} onClose={onClose} title="Merge BD User Profiles" size="md">
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
          <p className="leading-relaxed">
            This will transfer all candidate relationships from the source user to the target user, then delete the source user profile.
          </p>
        </div>
      </div>

      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label className="input-label">Source User (User leaving)</label>
          <select
            value={sourceUserId}
            onChange={(e) => {
              setSourceUserId(e.target.value)
              if (e.target.value === targetUserId) setTargetUserId('')
            }}
            className="input"
            required
          >
            <option value="">-- Select Source User --</option>
            {users.map((u) => (
              <option key={u.id} value={u.id}>
                {u.full_name ? `${u.full_name} (${u.email})` : u.email} [{u.role === 'admin' ? 'Admin' : 'BD User'}]
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className="input-label">Target User (User receiving candidates)</label>
          <select
            value={targetUserId}
            onChange={(e) => setTargetUserId(e.target.value)}
            className="input"
            required
            disabled={!sourceUserId}
          >
            <option value="">-- Select Target User --</option>
            {availableTargetUsers.map((u) => (
              <option key={u.id} value={u.id}>
                {u.full_name ? `${u.full_name} (${u.email})` : u.email} [{u.role === 'admin' ? 'Admin' : 'BD User'}]
              </option>
            ))}
          </select>
        </div>

        <div className="pt-4 flex gap-3">
          <button type="button" onClick={onClose} className="btn-secondary flex-1">
            Cancel
          </button>
          <button
            type="submit"
            disabled={mutation.isPending || !sourceUserId || !targetUserId}
            className="btn-primary flex-1"
          >
            {mutation.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Merge & Re-assign'}
          </button>
        </div>
      </form>
    </Modal>
  )
}
