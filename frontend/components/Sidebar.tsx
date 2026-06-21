'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { clsx } from 'clsx'

const MODULES = [
  { id: 'dashboard', label: 'Dashboard', path: '/dashboard' },
  { id: 'candidates', label: 'Candidates', path: '/candidates' },
  { id: 'applications', label: 'Applications', path: '/applications' },
  { id: 'jobs', label: 'Job Discovery', path: '/jobs' },
  { id: 'matches', label: 'AI Matches', path: '/matches' },
  { id: 'automation', label: 'Automation', path: '/automation' },
]

export function Sidebar() {
  const pathname = usePathname()

  return (
    <aside className="w-64 bg-bg-secondary border-r border-bg-border flex flex-col h-screen sticky top-0">
      {/* Logo Area */}
      <div className="h-14 flex items-center px-6 border-b border-bg-border gap-3 flex-shrink-0">
        <div className="w-8 h-8 rounded-lg bg-accent flex items-center justify-center text-white text-sm font-bold shadow-accent">
          BD
        </div>
        <span className="font-semibold text-text-primary">
          BD Automator
        </span>
      </div>

      {/* Navigation Links */}
      <nav className="flex-1 overflow-y-auto py-4 px-3 space-y-1">
        {MODULES.map((mod) => {
          const isActive = pathname.startsWith(mod.path)
          return (
            <Link
              key={mod.id}
              href={mod.path}
              className={clsx(
                'flex items-center px-3 py-2.5 rounded-lg transition-colors duration-150',
                isActive
                  ? 'bg-accent/10 text-accent font-medium'
                  : 'text-text-secondary hover:text-text-primary hover:bg-bg-hover'
              )}
            >
              <span className="text-sm">{mod.label}</span>
            </Link>
          )
        })}
      </nav>

      <div className="p-4 border-t border-bg-border text-center">
        <span className="text-[10px] text-text-muted">v1.0.0 (Beta)</span>
      </div>
    </aside>
  )
}
