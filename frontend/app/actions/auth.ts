'use server'
import { cookies } from 'next/headers'

const API_BASE = (() => {
  const raw = process.env.API_URL || process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'
  return raw.replace(/\/api\/?$/, '') + '/api'
})()

export async function loginAction(email: string, password: string) {
  // Client-side validation before hitting backend
  if (!email?.trim()) return { error: 'Email is required' }
  if (!password) return { error: 'Password is required' }
  if (password.length < 6) return { error: 'Password must be at least 6 characters' }

  try {
    const res = await fetch(`${API_BASE}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: email.trim(), password }),
    })
    const data = await res.json()
    if (!res.ok) {
      let errorMessage = 'Invalid credentials'
      if (typeof data.detail === 'string') {
        errorMessage = data.detail
      } else if (Array.isArray(data.detail)) {
        errorMessage = data.detail.map((err: { msg?: string }) => err.msg || JSON.stringify(err)).join(', ')
      } else if (data.detail) {
        errorMessage = JSON.stringify(data.detail)
      }
      return { error: errorMessage }
    }
    const token = data.access_token
    if (!token) return { error: 'No token returned from server' }

    const cookieStore = await cookies()
    cookieStore.set({
      name: 'auth_token',
      value: token,
      httpOnly: true,
      secure: process.env.NODE_ENV === 'production',
      sameSite: 'lax',
      path: '/',
      maxAge: 86400,
    })

    const meRes = await fetch(`${API_BASE}/auth/me`, {
      headers: { Authorization: `Bearer ${token}` },
      cache: 'no-store',
    })
    if (!meRes.ok) return { error: 'Could not verify user after login' }
    const me = await meRes.json()

    if (me.role === 'admin') return { redirect: '/admin' }
    return { redirect: '/dashboard' }
  } catch {
    return { error: 'Network error — is the backend running?' }
  }
}

export async function logoutAction() {
  const cookieStore = await cookies()
  cookieStore.delete('auth_token')
  return { success: true }
}

export async function getWebSocketConnectionDetailsAction() {
  const apiUrl = process.env.API_URL || process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'
  const wsUrl = apiUrl.replace(/^http/, 'ws') + '/ws/updates'
  const cookieStore = await cookies()
  const token = cookieStore.get('auth_token')?.value || null
  return { wsUrl, token }
}

export async function getCurrentUserAction(): Promise<{
  id: string
  email: string
  name: string
  full_name?: string
  role: string
} | null> {
  const cookieStore = await cookies()
  const token = cookieStore.get('auth_token')?.value
  if (!token) return null
  try {
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), 3000)
    const res = await fetch(`${API_BASE}/auth/me`, {
      headers: { Authorization: `Bearer ${token}` },
      cache: 'no-store',
      signal: controller.signal,
    })
    clearTimeout(timeout)
    if (!res.ok) return null
    const data = await res.json()
    return {
      id: data.id,
      email: data.email,
      name: data.full_name || data.email,
      full_name: data.full_name,
      role: data.role,
    }
  } catch {
    return null
  }
}
