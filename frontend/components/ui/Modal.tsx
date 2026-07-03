'use client'

import { useEffect, useRef, type ReactNode } from 'react'
import { X } from 'lucide-react'

interface ModalProps {
  open: boolean
  onClose: () => void
  title?: string
  description?: string
  children: ReactNode
  /** Max width class — defaults to 'max-w-md' */
  size?: 'sm' | 'md' | 'lg' | 'xl' | '2xl'
  /** Whether to show the close button in the header */
  showClose?: boolean
  /** Footer content (buttons) */
  footer?: ReactNode
}

const sizeMap = {
  sm: 'max-w-sm',
  md: 'max-w-md',
  lg: 'max-w-lg',
  xl: 'max-w-xl',
  '2xl': 'max-w-2xl',
}

export function Modal({
  open,
  onClose,
  title,
  description,
  children,
  size = 'md',
  showClose = true,
  footer,
}: ModalProps) {
  const overlayRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)

  // Close on Escape key
  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [open, onClose])

  // Lock body scroll when open
  useEffect(() => {
    if (open) {
      document.body.style.overflow = 'hidden'
    } else {
      document.body.style.overflow = ''
    }
    return () => { document.body.style.overflow = '' }
  }, [open])

  // Focus trap — focus the content on open
  useEffect(() => {
    if (open && contentRef.current) {
      contentRef.current.focus()
    }
  }, [open])

  if (!open) return null

  return (
    <div
      ref={overlayRef}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4 animate-fade-in"
      onClick={(e) => {
        if (e.target === overlayRef.current) onClose()
      }}
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div
        ref={contentRef}
        tabIndex={-1}
        className={`w-full ${sizeMap[size]} bg-bg-secondary rounded-2xl shadow-2xl border border-bg-border overflow-hidden animate-scale-up outline-none flex flex-col max-h-[85vh]`}
      >
        {/* Header */}
        {(title || showClose) && (
          <div className="p-6 border-b border-bg-border flex items-center justify-between shrink-0">
            <div>
              {title && (
                <h2 className="text-lg font-semibold text-text-primary">{title}</h2>
              )}
              {description && (
                <p className="text-xs text-text-muted mt-1">{description}</p>
              )}
            </div>
            {showClose && (
              <button
                onClick={onClose}
                className="text-text-muted hover:text-text-primary transition-colors rounded-lg p-1 hover:bg-bg-hover"
                aria-label="Close dialog"
              >
                <X className="w-5 h-5" />
              </button>
            )}
          </div>
        )}

        {/* Body */}
        <div className="p-6 overflow-y-auto flex-1">
          {children}
        </div>

        {/* Footer */}
        {footer && (
          <div className="px-6 py-4 border-t border-bg-border flex justify-end gap-3 shrink-0">
            {footer}
          </div>
        )}
      </div>
    </div>
  )
}
