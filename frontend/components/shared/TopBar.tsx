'use client'

import { useState, useRef, useEffect, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Bell, Search, LogOut, User as UserIcon, ChevronDown,
  CheckCircle2, XCircle, CalendarClock, Mail, Briefcase,
  AlertTriangle, Trophy, Clock, TrendingUp, Users,
} from 'lucide-react'
import Link from 'next/link'
import { useCurrentUser } from '@/hooks/useCurrentUser'
import { logoutAction } from '@/app/actions/auth'
import { api, ActivityEvent, InterviewSummary } from '@/lib/api'
import { formatDistanceToNow } from '@/lib/utils'

// ── Notification shape ────────────────────────────────────────────────────────

interface Notification {
  id: string
  title: string
  message: string
  time: string
  read: boolean
  icon: React.ElementType
  iconColor: string
  href?: string
}

// ── Map raw event_type / status → icon + colours ─────────────────────────────

const STATUS_META: Record<string, { label: string; icon: React.ElementType; color: string }> = {
  SUBMITTED:    { label: 'Applied',          icon: CheckCircle2,  color: 'text-accent'   },
  CONFIRMED:    { label: 'Confirmed',         icon: CheckCircle2,  color: 'text-success'  },
  REJECTED:     { label: 'Rejected',          icon: XCircle,       color: 'text-danger'   },
  OFFER:        { label: '🎉 Offer received', icon: Trophy,        color: 'text-amber-400'},
  INTERVIEW_R1: { label: 'Interview R1',      icon: CalendarClock, color: 'text-accent'   },
  INTERVIEW_R2: { label: 'Interview R2',      icon: CalendarClock, color: 'text-accent'   },
  INTERVIEW_R3: { label: 'Interview R3',      icon: CalendarClock, color: 'text-accent'   },
  INTERVIEW_R4: { label: 'Interview R4',      icon: CalendarClock, color: 'text-accent'   },
  ASSESSMENT:   { label: 'Assessment',        icon: TrendingUp,    color: 'text-purple-400'},
  QUEUED:       { label: 'Queued',            icon: Clock,         color: 'text-text-muted'},
  FOUND:        { label: 'Job found',         icon: Briefcase,     color: 'text-text-muted'},
  FAILED:       { label: 'Failed',            icon: AlertTriangle, color: 'text-danger'   },
}

function activityToNotification(event: ActivityEvent, idx: number): Notification {
  const parts = event.summary.split(' — ')
  const company = parts[0] ?? ''
  const status  = parts[1]?.toUpperCase() ?? ''
  const meta    = STATUS_META[status]

  if (event.event_type === 'email.classified') {
    // "hr@company.com: OFFER" or "noreply@...: REJECTION"
    const [from, classification] = event.summary.split(': ')
    const cls = classification?.toUpperCase() ?? ''
    const isGood = cls.includes('OFFER') || cls.includes('INTERVIEW')
    return {
      id: `evt-${idx}`,
      title: isGood ? '📨 Positive email received' : 'Email classified',
      message: `From ${from || 'recruiter'} — ${cls || classification}`,
      time: formatDistanceToNow(event.timestamp) + ' ago',
      read: false,
      icon: Mail,
      iconColor: isGood ? 'text-success' : 'text-text-muted',
      href: event.application_id ? `/applications/${event.application_id}` : undefined,
    }
  }

  return {
    id: `evt-${idx}`,
    title: meta ? meta.label : `Status: ${status}`,
    message: company ? `${company}` : event.summary,
    time: formatDistanceToNow(event.timestamp) + ' ago',
    read: status === 'QUEUED' || status === 'FOUND',
    icon: meta?.icon ?? CheckCircle2,
    iconColor: meta?.color ?? 'text-text-muted',
    href: event.application_id ? `/candidates` : undefined,
  }
}

function interviewToNotification(iv: InterviewSummary, idx: number): Notification {
  const when = iv.scheduled_at
    ? new Date(iv.scheduled_at).toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
    : 'TBD'
  const hoursAway = iv.scheduled_at
    ? Math.round((new Date(iv.scheduled_at).getTime() - Date.now()) / 36e5)
    : null
  const urgent = hoursAway !== null && hoursAway <= 48
  return {
    id: `iv-${idx}`,
    title: urgent ? '⚡ Interview coming up!' : `Interview Round ${iv.round}`,
    message: `${iv.company} · ${iv.type} · ${when}`,
    time: hoursAway !== null ? `In ${hoursAway}h` : 'Scheduled',
    read: false,
    icon: CalendarClock,
    iconColor: urgent ? 'text-amber-400' : 'text-accent',
    href: `/dashboard/interviews`,
  }
}

// ── Component ─────────────────────────────────────────────────────────────────

export function TopBar() {
  const { data: user, isLoading } = useCurrentUser()
  const isAdmin = user?.role === 'admin'

  const [showNotifications, setShowNotifications] = useState(false)
  const [showProfileMenu, setShowProfileMenu]     = useState(false)
  const [readIds, setReadIds]                     = useState<Set<string>>(new Set())

  const notificationsRef = useRef<HTMLDivElement>(null)
  const profileMenuRef   = useRef<HTMLDivElement>(null)

  // ── Fetch real data ─────────────────────────────────────────────────────────

  const { data: activityFeed = [] } = useQuery({
    queryKey: ['activity-feed-notifs'],
    queryFn: () => api.getActivityFeed(15),
    refetchInterval: 30_000,
    enabled: !!user,
  })

  const { data: upcomingInterviews = [] } = useQuery({
    queryKey: ['interviews-notifs'],
    queryFn: () => api.getInterviews(true),
    refetchInterval: 60_000,
    enabled: !!user,
  })

  const { data: kpis } = useQuery({
    queryKey: ['kpis-notifs'],
    queryFn: () => api.getKPIs(),
    refetchInterval: 60_000,
    enabled: !!user,
  })

  // ── Build notification list ─────────────────────────────────────────────────

  const notifications = useMemo<Notification[]>(() => {
    const list: Notification[] = []

    // 1. Upcoming interviews (highest priority — always at top)
    upcomingInterviews.slice(0, 3).forEach((iv, i) => list.push(interviewToNotification(iv, i)))

    // 2. Pending offers (pull from KPIs as a summary alert)
    if (kpis && kpis.total_offers > 0) {
      list.push({
        id: 'kpi-offers',
        title: `🎉 ${kpis.total_offers} Offer${kpis.total_offers > 1 ? 's' : ''} received!`,
        message: isAdmin
          ? 'Candidates have active job offers. Review now.'
          : 'You have active job offers. Congratulations!',
        time: 'Active',
        read: false,
        icon: Trophy,
        iconColor: 'text-amber-400',
        href: isAdmin ? '/admin/applications' : '/dashboard/applications',
      })
    }

    // 3. Recent activity events
    activityFeed.slice(0, 8).forEach((ev, i) => list.push(activityToNotification(ev, i)))

    // 4. Admin-specific: pending queue summary
    if (isAdmin && kpis && kpis.pending_in_queue > 0) {
      list.push({
        id: 'kpi-queue',
        title: `${kpis.pending_in_queue} applications queued`,
        message: 'Auto-apply pipeline has pending jobs in queue.',
        time: 'Now',
        read: true,
        icon: Clock,
        iconColor: 'text-text-muted',
        href: '/admin/applications',
      })
    }

    // 5. BD user: applied today
    if (!isAdmin && kpis && kpis.applied_today > 0) {
      list.push({
        id: 'kpi-today',
        title: `${kpis.applied_today} applications sent today`,
        message: 'Your auto-apply pipeline is active and running.',
        time: 'Today',
        read: true,
        icon: TrendingUp,
        iconColor: 'text-accent',
        href: '/dashboard/applications',
      })
    }

    return list
  }, [activityFeed, upcomingInterviews, kpis, isAdmin])

  const unreadCount = notifications.filter(n => !n.read && !readIds.has(n.id)).length

  // ── Close on outside click ──────────────────────────────────────────────────

  useEffect(() => {
    function onOutside(e: MouseEvent) {
      if (notificationsRef.current && !notificationsRef.current.contains(e.target as Node))
        setShowNotifications(false)
      if (profileMenuRef.current && !profileMenuRef.current.contains(e.target as Node))
        setShowProfileMenu(false)
    }
    document.addEventListener('mousedown', onOutside)
    return () => document.removeEventListener('mousedown', onOutside)
  }, [])

  const handleLogout = async () => {
    setShowProfileMenu(false)
    // Delete the auth cookie server-side, then hard-navigate to /login.
    // window.location.href forces a full page reload which wipes the Next.js
    // router cache, React Query cache, and all module-level state so the next
    // user session always starts completely clean.
    await logoutAction()
    window.location.href = '/login'
  }

  const markAllRead = () => {
    setReadIds(new Set(notifications.map(n => n.id)))
  }

  return (
    <header className="h-16 border-b border-bg-border bg-bg-primary flex items-center justify-between px-6 sticky top-0 z-20">
      {/* Search */}
      <div className="flex items-center text-text-muted">
        <Search className="w-5 h-5 mr-3" />
        <input
          type="text"
          placeholder="Search candidates, jobs..."
          className="bg-transparent border-none outline-none text-sm w-64 placeholder:text-text-muted text-text-primary"
        />
      </div>

      <div className="flex items-center gap-6">

        {/* ── Notifications ── */}
        <div className="relative" ref={notificationsRef}>
          <button
            className="relative text-text-muted hover:text-text-primary transition-colors focus:outline-none"
            onClick={() => setShowNotifications(v => !v)}
            aria-label="Notifications"
          >
            <Bell className="w-5 h-5" />
            {unreadCount > 0 && (
              <span className="absolute -top-1 -right-1 min-w-[16px] h-4 px-0.5 bg-accent text-white text-[9px] font-bold rounded-full flex items-center justify-center border border-bg-primary">
                {unreadCount > 9 ? '9+' : unreadCount}
              </span>
            )}
          </button>

          {showNotifications && (
            <div className="absolute right-0 mt-3 w-96 bg-bg-primary border border-bg-border rounded-xl shadow-2xl overflow-hidden z-50 animate-in fade-in slide-in-from-top-1 duration-150">
              {/* Header */}
              <div className="flex items-center justify-between px-4 py-3 border-b border-bg-border bg-bg-secondary/60">
                <div>
                  <h3 className="font-semibold text-text-primary text-sm">Notifications</h3>
                  {unreadCount > 0 && (
                    <p className="text-[10px] text-text-muted mt-0.5">{unreadCount} unread</p>
                  )}
                </div>
                {unreadCount > 0 && (
                  <button
                    onClick={markAllRead}
                    className="text-xs text-accent font-medium hover:opacity-75 transition-opacity"
                  >
                    Mark all read
                  </button>
                )}
              </div>

              {/* List */}
              <div className="max-h-[420px] overflow-y-auto divide-y divide-bg-border">
                {notifications.length === 0 ? (
                  <div className="px-4 py-10 text-center">
                    <Bell className="w-8 h-8 text-text-muted mx-auto mb-2 opacity-40" />
                    <p className="text-sm text-text-muted">No notifications yet</p>
                    <p className="text-xs text-text-muted/60 mt-1">Activity will appear here as jobs are applied to.</p>
                  </div>
                ) : (
                  notifications.map((n) => {
                    const isUnread = !n.read && !readIds.has(n.id)
                    const Icon = n.icon
                    const content = (
                      <div
                        key={n.id}
                        onClick={() => setReadIds(prev => new Set([...prev, n.id]))}
                        className={`px-4 py-3.5 hover:bg-bg-secondary transition-colors cursor-pointer flex gap-3 ${isUnread ? 'bg-accent/4' : ''}`}
                      >
                        {/* Icon */}
                        <div className={`mt-0.5 shrink-0 w-7 h-7 rounded-full bg-bg-secondary border border-bg-border flex items-center justify-center ${n.iconColor}`}>
                          <Icon className="w-3.5 h-3.5" />
                        </div>
                        {/* Text */}
                        <div className="flex-1 min-w-0">
                          <div className="flex items-start justify-between gap-2">
                            <p className={`text-sm font-medium leading-snug ${isUnread ? 'text-text-primary' : 'text-text-secondary'}`}>
                              {n.title}
                            </p>
                            <span className="text-[10px] text-text-muted whitespace-nowrap shrink-0 mt-0.5">{n.time}</span>
                          </div>
                          <p className="text-xs text-text-muted mt-0.5 leading-relaxed truncate">{n.message}</p>
                        </div>
                        {/* Unread dot */}
                        {isUnread && (
                          <div className="shrink-0 mt-2">
                            <div className="w-1.5 h-1.5 rounded-full bg-accent" />
                          </div>
                        )}
                      </div>
                    )

                    return n.href ? (
                      <Link key={n.id} href={n.href} onClick={() => setShowNotifications(false)}>
                        {content}
                      </Link>
                    ) : (
                      <div key={n.id}>{content}</div>
                    )
                  })
                )}
              </div>

              {/* Footer */}
              {notifications.length > 0 && (
                <div className="border-t border-bg-border">
                  <Link
                    href={isAdmin ? '/admin/applications' : '/dashboard/applications'}
                    onClick={() => setShowNotifications(false)}
                    className="block text-center text-xs text-accent font-medium py-3 hover:bg-bg-secondary transition-colors"
                  >
                    View all activity →
                  </Link>
                </div>
              )}
            </div>
          )}
        </div>

        <div className="h-6 w-px bg-bg-border" />

        {/* ── Profile dropdown ── */}
        <div className="relative" ref={profileMenuRef}>
          <button
            onClick={() => setShowProfileMenu(v => !v)}
            className="flex items-center gap-3 hover:opacity-80 transition-opacity focus:outline-none group"
            aria-label="Open profile menu"
          >
            <div className="flex flex-col items-end">
              <span className="text-sm font-medium text-text-primary">
                {isLoading ? 'Loading...' : user?.full_name || user?.name || user?.email || 'Unknown User'}
              </span>
              <span className="text-xs text-text-muted capitalize">
                {isAdmin ? 'Administrator' : 'BD User'}
              </span>
            </div>
            <div className="relative w-9 h-9 rounded-full bg-bg-secondary flex items-center justify-center text-text-secondary border border-bg-border group-hover:border-accent/50 transition-colors">
              <UserIcon className="w-5 h-5" />
              <span className="absolute -bottom-0.5 -right-0.5 w-3.5 h-3.5 rounded-full bg-bg-primary border border-bg-border flex items-center justify-center">
                <ChevronDown className="w-2 h-2 text-text-muted" />
              </span>
            </div>
          </button>

          {showProfileMenu && (
            <div className="absolute right-0 mt-3 w-52 bg-bg-primary border border-bg-border rounded-xl shadow-xl overflow-hidden z-50 animate-in fade-in slide-in-from-top-1 duration-150">
              <div className="px-4 py-3 border-b border-bg-border bg-bg-secondary/50">
                <p className="text-sm font-semibold text-text-primary truncate">
                  {user?.full_name || user?.name || user?.email || 'User'}
                </p>
                <p className="text-xs text-text-muted capitalize mt-0.5">
                  {isAdmin ? 'Administrator' : 'BD User'}
                </p>
              </div>
              <div className="px-3 py-2">
                <button
                  onClick={handleLogout}
                  className="w-full flex items-center gap-2.5 px-2.5 py-2 rounded-lg text-sm text-text-secondary hover:text-danger hover:bg-danger/8 transition-colors"
                >
                  <LogOut className="w-4 h-4 shrink-0" />
                  Log Out
                </button>
              </div>
            </div>
          )}
        </div>

      </div>
    </header>
  )
}
