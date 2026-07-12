'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { clsx } from 'clsx'
import {
  LayoutDashboard, Users, FileText, Briefcase,
  ChevronLeft, ChevronRight, Menu,
  Radar, CalendarClock, UserCog,
  Activity, Settings, BarChart3,
} from 'lucide-react'

import { useCurrentUser } from '@/hooks/useCurrentUser'

type NavItem = {
  id: string
  label: string
  path: string
  icon: React.ElementType
}

type NavSection = {
  title?: string
  items: NavItem[]
}

const ADMIN_NAV: NavSection[] = [
  {
    items: [
      { id: 'admin-overview',   label: 'Admin Overview',   path: '/admin',              icon: LayoutDashboard },
      { id: 'admin-dashboard',  label: 'BD Dashboard',     path: '/dashboard',          icon: LayoutDashboard },
    ],
  },
  {
    title: 'Management',
    items: [
      { id: 'admin-users',      label: 'User Management',  path: '/admin/users',        icon: UserCog },
      { id: 'admin-jobs',       label: 'Job Management',   path: '/admin/jobs',         icon: Briefcase },
      { id: 'admin-candidates', label: 'Candidates',       path: '/candidates',         icon: Users },
      { id: 'admin-apps',       label: 'Applications',     path: '/dashboard/applications', icon: FileText },
      { id: 'admin-interviews', label: 'Interviews',       path: '/dashboard/interviews',   icon: CalendarClock },
      { id: 'admin-analytics',  label: 'Analytics',        path: '/admin/analytics',    icon: BarChart3 },
    ],
  },
  {
    title: 'Automation & Tools',
    items: [
      { id: 'admin-discovery',  label: 'Job Discovery',    path: '/admin/discovery',    icon: Radar },
      { id: 'admin-ops',        label: 'Pipeline Ops',     path: '/admin/ops',          icon: Activity },
      { id: 'admin-settings',   label: 'Settings',         path: '/admin/settings',     icon: Settings },
    ],
  },
]

const BD_USER_NAV: NavSection[] = [
  {
    items: [
      { id: 'bd-dashboard',    label: 'Dashboard',       path: '/dashboard',              icon: LayoutDashboard },
    ],
  },
  {
    title: 'Pipeline',
    items: [
      { id: 'bd-jobs',         label: 'Jobs Feed',       path: '/dashboard/jobs',         icon: Briefcase },
      { id: 'bd-apps',         label: 'Applications',    path: '/dashboard/applications', icon: FileText },
      { id: 'bd-interviews',   label: 'Interviews',      path: '/dashboard/interviews',   icon: CalendarClock },
    ],
  },
  {
    title: 'Profiles',
    items: [
      { id: 'bd-candidates',   label: 'Candidates',      path: '/candidates',             icon: Users },
    ],
  },
]

interface SidebarProps {
  collapsed: boolean
  onToggle: () => void
}

export function Sidebar({ collapsed, onToggle }: SidebarProps) {
  const pathname = usePathname()
  const { data: user, isLoading } = useCurrentUser()

  const isAdmin = user?.role === 'admin' || pathname.startsWith('/admin')
  const navSections = isAdmin ? ADMIN_NAV : BD_USER_NAV

  const isActive = (path: string) => {
    // Exact match for root dashboard / admin pages
    if (path === '/dashboard' || path === '/admin') return pathname === path
    // Prefix match for sub-pages
    return pathname.startsWith(path)
  }

  return (
    <aside
      className={clsx(
        'bg-bg-secondary border-r border-bg-border flex flex-col h-screen sticky top-0 transition-all duration-200 shrink-0',
        collapsed ? 'w-[60px]' : 'w-56',
      )}
    >
      {/* Logo Area */}
      <div className="h-14 flex items-center border-b border-bg-border flex-shrink-0 relative">
        <Link
          href={user?.role === 'admin' ? '/admin' : '/dashboard'}
          className={clsx(
            'flex items-center gap-3 flex-1 min-w-0 px-3 hover:opacity-80 transition-opacity',
            collapsed && 'justify-center px-0',
          )}
        >
          <div className="w-8 h-8 rounded-lg bg-accent flex items-center justify-center text-white text-sm font-bold shadow-accent shrink-0">
            BD
          </div>
          {!collapsed && (
            <span className="font-semibold text-text-primary truncate text-sm">BD Automator</span>
          )}
        </Link>

        {/* Toggle button */}
        <button
          onClick={onToggle}
          className={clsx(
            'absolute -right-3 top-1/2 -translate-y-1/2 z-10',
            'w-6 h-6 rounded-full bg-bg-card border border-bg-border shadow-sm',
            'flex items-center justify-center text-text-muted hover:text-text-primary',
            'transition-colors duration-150',
          )}
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          {collapsed ? <ChevronRight className="w-3 h-3" /> : <ChevronLeft className="w-3 h-3" />}
        </button>
      </div>

      {/* Navigation Links */}
      <nav className="flex-1 overflow-y-auto py-3 px-2">
        {isLoading && (
          <div className="space-y-2 px-2.5 py-2" aria-hidden>
            {[...Array(5)].map((_, i) => (
              <div key={i} className="h-8 rounded-lg bg-bg-hover animate-pulse" />
            ))}
          </div>
        )}
        {!isLoading && navSections.map((section, sIdx) => (
          <div key={sIdx} className={sIdx > 0 ? 'mt-4' : ''}>
            {/* Section title */}
            {section.title && !collapsed && (
              <div className="px-2.5 mb-1.5">
                <span className="text-[10px] font-semibold uppercase tracking-wider text-text-muted/70">
                  {section.title}
                </span>
              </div>
            )}
            {section.title && collapsed && sIdx > 0 && (
              <div className="mx-2 mb-2 border-t border-bg-border" />
            )}

            <div className="space-y-0.5">
              {section.items.map((item) => {
                const active = isActive(item.path)
                const Icon = item.icon
                return (
                  <Link
                    key={item.id}
                    href={item.path}
                    title={collapsed ? item.label : undefined}
                    className={clsx(
                      'flex items-center gap-3 px-2.5 py-2 rounded-lg transition-colors duration-150',
                      collapsed ? 'justify-center' : '',
                      active
                        ? 'bg-accent/10 text-accent font-medium'
                        : 'text-text-secondary hover:text-text-primary hover:bg-bg-hover',
                    )}
                    aria-current={active ? 'page' : undefined}
                  >
                    <Icon className="w-4 h-4 shrink-0" />
                    {!collapsed && <span className="text-sm truncate">{item.label}</span>}
                  </Link>
                )
              })}
            </div>
          </div>
        ))}
      </nav>

      {/* Footer */}
      {!collapsed && (
        <div className="p-4 border-t border-bg-border">
          <div className="text-[10px] text-text-muted text-center">
            v1.0.0{user ? ` · ${user.role === 'admin' ? 'Admin' : 'BD User'}` : ''}
          </div>
        </div>
      )}
    </aside>
  )
}

/* Mobile hamburger trigger */
export function MobileMenuButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="w-9 h-9 rounded-lg bg-bg-card border border-bg-border shadow flex items-center justify-center text-text-secondary hover:text-text-primary transition-colors"
      aria-label="Open navigation menu"
    >
      <Menu className="w-5 h-5" />
    </button>
  )
}
