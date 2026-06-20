'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { clsx } from 'clsx'

const MODULES = [
  { id: 'dashboard', label: 'Dashboard', path: '/dashboard', icon: '📊', description: 'Module 5: Analytics' },
  { id: 'candidates', label: 'Candidates', path: '/candidates', icon: '👤', description: 'Module 1: Orchestration' },
  { id: 'jobs', label: 'Job Discovery', path: '/jobs', icon: '🔍', description: 'Module 2: Scraper' },
  { id: 'matches', label: 'AI Matches', path: '/matches', icon: '🧠', description: 'Module 3: Intelligence' },
  { id: 'automation', label: 'Automation', path: '/automation', icon: '🤖', description: 'Module 4: Playwright' },
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
                'flex items-center gap-3 px-3 py-2.5 rounded-lg transition-colors duration-150',
                isActive
                  ? 'bg-accent/10 text-accent font-medium'
                  : 'text-text-secondary hover:text-text-primary hover:bg-bg-hover'
              )}
            >
              <span className="text-lg">{mod.icon}</span>
              <div className="flex flex-col">
                <span className="text-sm">{mod.label}</span>
                <span className="text-[10px] text-text-muted opacity-80">{mod.description}</span>
              </div>
            </Link>
          )
        })}
      </nav>

      {/* User Area */}
      <div className="p-4 border-t border-bg-border">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-full bg-accent flex items-center justify-center text-white text-xs font-bold">
            SH
          </div>
          <div className="flex flex-col">
            <span className="text-sm font-medium text-text-primary">Sabih Haider</span>
            <span className="text-xs text-text-muted">Administrator</span>
          </div>
        </div>
      </div>
    </aside>
  )
}
