'use client'

import { useState, useRef, useEffect } from 'react'
import { useRouter } from 'next/navigation'
import { Bell, Search, LogOut, User as UserIcon } from 'lucide-react'
import { useCurrentUser } from '@/hooks/useCurrentUser'
import { logoutAction } from '@/app/actions/auth'

export function TopBar() {
  const router = useRouter()
  const { data: user, isLoading } = useCurrentUser()
  const [showNotifications, setShowNotifications] = useState(false)
  const notificationsRef = useRef<HTMLDivElement>(null)

  const handleLogout = async () => {
    await logoutAction()
    router.push('/login')
  }

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (notificationsRef.current && !notificationsRef.current.contains(event.target as Node)) {
        setShowNotifications(false)
      }
    }
    document.addEventListener("mousedown", handleClickOutside)
    return () => document.removeEventListener("mousedown", handleClickOutside)
  }, [])

  const mockNotifications = [
    { id: 1, title: 'System Update', message: 'The platform has been updated to the latest version.', time: 'Just now', read: false },
    { id: 2, title: 'New Feature', message: 'Check out the new analytics dashboard.', time: '2h ago', read: false },
    { id: 3, title: 'Welcome', message: 'Welcome to BD Automator Dashboard!', time: '1d ago', read: true },
  ]

  return (
    <header className="h-16 border-b border-bg-border bg-bg-primary flex items-center justify-between px-6 sticky top-0 z-20">
      <div className="flex items-center text-text-muted">
        <Search className="w-5 h-5 mr-3" />
        <input
          type="text"
          placeholder="Search candidates, jobs..."
          className="bg-transparent border-none outline-none text-sm w-64 placeholder:text-text-muted text-text-primary"
        />
      </div>

      <div className="flex items-center gap-6">
        <div className="relative" ref={notificationsRef}>
          <button 
            className="relative text-text-muted hover:text-text-primary transition-colors focus:outline-none"
            onClick={() => setShowNotifications(!showNotifications)}
          >
            <Bell className="w-5 h-5" />
            {mockNotifications.some(n => !n.read) && (
              <span className="absolute -top-0.5 -right-0.5 w-2 h-2 bg-accent rounded-full border border-bg-primary"></span>
            )}
          </button>

          {showNotifications && (
            <div className="absolute right-0 mt-3 w-80 bg-bg-primary border border-bg-border rounded-xl shadow-xl overflow-hidden z-50">
              <div className="flex items-center justify-between px-4 py-3 border-b border-bg-border bg-bg-secondary/50">
                <h3 className="font-semibold text-text-primary text-sm">Notifications</h3>
                <button 
                  className="text-xs text-accent font-medium hover:opacity-80"
                  onClick={(e) => {
                    e.stopPropagation();
                    alert("All notifications marked as read.");
                  }}
                >
                  Mark all as read
                </button>
              </div>
              <div className="max-h-80 overflow-y-auto">
                {mockNotifications.map(notification => (
                  <div key={notification.id} className={`px-4 py-3 border-b border-bg-border last:border-0 hover:bg-bg-secondary transition-colors cursor-pointer flex gap-3 ${!notification.read ? 'bg-bg-secondary/30' : ''}`}>
                    <div className="mt-1">
                      <div className={`w-2 h-2 rounded-full ${!notification.read ? 'bg-accent' : 'bg-transparent'}`}></div>
                    </div>
                    <div>
                      <p className="text-sm font-medium text-text-primary mb-0.5">{notification.title}</p>
                      <p className="text-xs text-text-muted mb-1">{notification.message}</p>
                      <p className="text-[10px] text-text-muted opacity-70">{notification.time}</p>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        <div className="h-6 w-px bg-bg-border"></div>

        <div className="flex items-center gap-3">
          <div className="flex flex-col items-end">
            <span className="text-sm font-medium text-text-primary">
              {isLoading ? 'Loading...' : user?.email || 'Unknown User'}
            </span>
            <span className="text-xs text-text-muted capitalize">
              {user?.role === 'admin' ? 'Administrator' : 'Candidate Profile'}
            </span>
          </div>
          <div className="w-9 h-9 rounded-full bg-bg-secondary flex items-center justify-center text-text-secondary border border-bg-border">
            <UserIcon className="w-5 h-5" />
          </div>
          <button
            onClick={handleLogout}
            className="ml-2 text-text-muted hover:text-danger transition-colors p-2 rounded-md hover:bg-bg-hover"
            title="Log out"
          >
            <LogOut className="w-4 h-4" />
          </button>
        </div>
      </div>
    </header>
  )
}
