'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { clsx } from 'clsx'
import {
  LayoutDashboard, Users, FileText, Briefcase,
  ChevronLeft, ChevronRight, Menu, Search,
} from 'lucide-react'

import { useCurrentUser } from '@/hooks/useCurrentUser'

type NavItem = {
  id: string
  label: string
  path: string
  icon: React.ElementType
}

const ADMIN_NAV_ITEMS: NavItem[] = [
  { id: 'admin-overview',   label: 'Overview',         path: '/admin',              icon: LayoutDashboard },
  { id: 'admin-discovery',  label: 'Job Discovery',    path: '/admin/discovery',    icon: Search },
  { id: 'admin-users',      label: 'User Management',  path: '/admin/users',        icon: Users },
  { id: 'admin-jobs',       label: 'Job Management',   path: '/admin/jobs',         icon: Briefcase },
  { id: 'admin-apps',       label: 'All Applications', path: '/applications',       icon: FileText },
  { id: 'admin-candidates', label: 'Candidates',       path: '/candidates',         icon: Users },
]

const BD_USER_NAV_ITEMS: NavItem[] = [
  { id: 'bd-dashboard',  label: 'Dashboard',       path: '/dashboard',              icon: LayoutDashboard },
  { id: 'bd-jobs',       label: 'Jobs Feed',       path: '/dashboard/jobs',         icon: Briefcase },
  { id: 'bd-apps',       label: 'My Applications', path: '/dashboard/applications', icon: FileText },
  { id: 'bd-interviews', label: 'Interviews',      path: '/dashboard/interviews',   icon: Users },
  { id: 'bd-candidates', label: 'Candidates',      path: '/candidates',             icon: Users },
]

interface SidebarProps {
  collapsed: boolean
  onToggle: () => void
}

export function Sidebar({ collapsed, onToggle }: SidebarProps) {
  const pathname = usePathname()
  const { data: user } = useCurrentUser()

  const navItems = user?.role === 'admin' ? ADMIN_NAV_ITEMS : BD_USER_NAV_ITEMS

  return (
    <aside
      className={clsx(
        'bg-bg-secondary border-r border-bg-border flex flex-col h-screen sticky top-0 transition-all duration-200 shrink-0',
        collapsed ? 'w-14' : 'w-56',
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
      <nav className="flex-1 overflow-y-auto py-3 px-2 space-y-0.5">
        {navItems.map((mod) => {
          const isActive = (mod.path === '/dashboard' || mod.path === '/admin')
            ? pathname === mod.path
            : pathname.startsWith(mod.path)
          const Icon = mod.icon
          return (
            <Link
              key={mod.id}
              href={mod.path}
              title={collapsed ? mod.label : undefined}
              className={clsx(
                'flex items-center gap-3 px-2.5 py-2 rounded-lg transition-colors duration-150',
                collapsed ? 'justify-center' : '',
                isActive
                  ? 'bg-accent/10 text-accent font-medium'
                  : 'text-text-secondary hover:text-text-primary hover:bg-bg-hover',
              )}
            >
              <Icon className="w-4 h-4 shrink-0" />
              {!collapsed && <span className="text-sm truncate">{mod.label}</span>}
            </Link>
          )
        })}
      </nav>

      {!collapsed && (
        <div className="p-4 border-t border-bg-border text-center">
          <span className="text-[10px] text-text-muted">v1.0.0 (Beta)</span>
        </div>
      )}
    </aside>
  )
}

/* Mobile hamburger trigger — rendered inline inside ClientLayout on small screens */
export function MobileMenuButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="fixed top-3 left-3 z-50 md:hidden w-9 h-9 rounded-lg bg-bg-card border border-bg-border shadow flex items-center justify-center text-text-secondary hover:text-text-primary"
      aria-label="Open menu"
    >
      <Menu className="w-5 h-5" />
    </button>
  )
}
