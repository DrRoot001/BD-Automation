'use client'

import { useState, useEffect } from 'react'
import { usePathname } from 'next/navigation'
import { Sidebar, MobileMenuButton } from './Sidebar'
import { TopBar } from './TopBar'
import { X } from 'lucide-react'
import { ToastProvider } from '@/components/ui/Toast'
import { ConfirmDialogProvider } from '@/components/ui/ConfirmDialog'

const SIDEBAR_COLLAPSED_KEY = 'bd-sidebar-collapsed'

export function ClientLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname()
  const [collapsed, setCollapsed] = useState(false)
  const [mobileOpen, setMobileOpen] = useState(false)

  // Restore sidebar collapsed state from localStorage
  useEffect(() => {
    try {
      const stored = localStorage.getItem(SIDEBAR_COLLAPSED_KEY)
      if (stored === 'true') setCollapsed(true)
    } catch {
      // localStorage not available
    }
  }, [])

  // Persist sidebar collapsed state
  const handleToggle = () => {
    setCollapsed((c) => {
      const next = !c
      try { localStorage.setItem(SIDEBAR_COLLAPSED_KEY, String(next)) } catch {}
      return next
    })
  }

  // Close mobile drawer on route change
  useEffect(() => { setMobileOpen(false) }, [pathname])

  if (pathname === '/login') {
    return (
      <ToastProvider>
        <ConfirmDialogProvider>
          {children}
        </ConfirmDialogProvider>
      </ToastProvider>
    )
  }

  return (
    <ToastProvider>
      <ConfirmDialogProvider>
        <div className="flex h-screen overflow-hidden bg-bg-primary">
          {/* ── Desktop sidebar ───────────────────────────────────────── */}
          <div className="hidden md:flex">
            <Sidebar collapsed={collapsed} onToggle={handleToggle} />
          </div>

          {/* ── Mobile overlay + drawer ───────────────────────────────── */}
          {mobileOpen && (
            <div
              className="fixed inset-0 z-40 bg-black/40 md:hidden"
              onClick={() => setMobileOpen(false)}
              aria-hidden="true"
            />
          )}
          <div
            className={`fixed inset-y-0 left-0 z-50 md:hidden transition-transform duration-200 ${
              mobileOpen ? 'translate-x-0' : '-translate-x-full'
            }`}
          >
            <Sidebar collapsed={false} onToggle={() => setMobileOpen(false)} />
            <button
              onClick={() => setMobileOpen(false)}
              className="absolute top-3 right-3 text-text-muted hover:text-text-primary"
              aria-label="Close navigation menu"
            >
              <X className="w-5 h-5" />
            </button>
          </div>

          {/* ── Main content ──────────────────────────────────────────── */}
          <main className="flex-1 flex flex-col min-w-0 overflow-hidden">
            {/* Mobile top bar */}
            <div className="md:hidden h-12 flex items-center px-4 border-b border-bg-border bg-bg-secondary sticky top-0 z-30 shrink-0">
              <MobileMenuButton onClick={() => setMobileOpen(true)} />
              <span className="text-sm font-semibold text-text-primary mx-auto">BD Automator</span>
            </div>

            {/* Desktop TopBar */}
            <div className="hidden md:block shrink-0">
              <TopBar />
            </div>

            {/* Page Content */}
            <div className="flex-1 overflow-y-auto p-6 md:p-8">
              {children}
            </div>
          </main>
        </div>
      </ConfirmDialogProvider>
    </ToastProvider>
  )
}
