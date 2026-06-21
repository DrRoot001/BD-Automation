'use client'

import { useEffect } from 'react'
import { usePathname, useRouter } from 'next/navigation'
import { Sidebar } from './Sidebar'

export function ClientLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname()
  const router = useRouter()

  useEffect(() => {
    const token = localStorage.getItem('auth_token')
    if (pathname === '/login') {
      if (token) router.replace('/dashboard')
      return
    }
    if (!token) router.replace('/login')
  }, [pathname, router])

  if (pathname === '/login') {
    return (
      <div className="min-h-screen bg-bg-primary flex items-center justify-center">
        {children}
      </div>
    )
  }

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <main className="flex-1 overflow-y-auto">
        {children}
      </main>
    </div>
  )
}
