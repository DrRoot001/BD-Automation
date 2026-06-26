'use client'

import { useEffect, useState } from 'react'
import { CheckCircle, AlertTriangle, XCircle, X, Info } from 'lucide-react'
import { clsx } from 'clsx'

export type ToastType = 'success' | 'error' | 'warning' | 'info'

export interface ToastMessage {
  id: string
  type: ToastType
  title: string
  message?: string
  duration?: number
}

interface ToastItemProps {
  toast: ToastMessage
  onDismiss: (id: string) => void
}

function ToastItem({ toast, onDismiss }: ToastItemProps) {
  useEffect(() => {
    const timer = setTimeout(() => onDismiss(toast.id), toast.duration ?? 4000)
    return () => clearTimeout(timer)
  }, [toast, onDismiss])

  const icons = {
    success: <CheckCircle className="w-4 h-4 text-success shrink-0" />,
    error:   <XCircle className="w-4 h-4 text-danger shrink-0" />,
    warning: <AlertTriangle className="w-4 h-4 text-warning shrink-0" />,
    info:    <Info className="w-4 h-4 text-info shrink-0" />,
  }

  const borders = {
    success: 'border-success/20 bg-success/8',
    error:   'border-danger/20 bg-danger/8',
    warning: 'border-warning/20 bg-warning/8',
    info:    'border-info/20 bg-info/8',
  }

  return (
    <div
      className={clsx(
        'flex items-start gap-3 px-4 py-3 rounded-lg border shadow-card text-sm animate-slide-up min-w-[280px] max-w-sm bg-bg-card',
        borders[toast.type],
      )}
    >
      {icons[toast.type]}
      <div className="flex-1 min-w-0">
        <p className="font-medium text-text-primary">{toast.title}</p>
        {toast.message && <p className="text-text-muted text-xs mt-0.5">{toast.message}</p>}
      </div>
      <button
        onClick={() => onDismiss(toast.id)}
        className="text-text-muted hover:text-text-primary transition-colors"
      >
        <X className="w-3.5 h-3.5" />
      </button>
    </div>
  )
}

export function ToastContainer({ toasts, onDismiss }: { toasts: ToastMessage[]; onDismiss: (id: string) => void }) {
  return (
    <div className="fixed bottom-5 right-5 z-[9999] flex flex-col gap-2 items-end pointer-events-none">
      {toasts.map((t) => (
        <div key={t.id} className="pointer-events-auto">
          <ToastItem toast={t} onDismiss={onDismiss} />
        </div>
      ))}
    </div>
  )
}

// Hook
export function useToast() {
  const [toasts, setToasts] = useState<ToastMessage[]>([])

  const dismiss = (id: string) => setToasts((prev) => prev.filter((t) => t.id !== id))

  const toast = (type: ToastType, title: string, message?: string, duration?: number) => {
    const id = Math.random().toString(36).slice(2)
    setToasts((prev) => [...prev, { id, type, title, message, duration }])
  }

  return {
    toasts,
    dismiss,
    success: (title: string, message?: string) => toast('success', title, message),
    error:   (title: string, message?: string) => toast('error', title, message),
    warning: (title: string, message?: string) => toast('warning', title, message),
    info:    (title: string, message?: string) => toast('info', title, message),
  }
}
