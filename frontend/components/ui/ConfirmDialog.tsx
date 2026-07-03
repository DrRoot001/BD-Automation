'use client'

import { useState, createContext, useContext, useCallback, type ReactNode } from 'react'
import { Modal } from './Modal'
import { AlertTriangle, Info, Loader2 } from 'lucide-react'

interface ConfirmOptions {
  title: string
  message: string
  confirmLabel?: string
  cancelLabel?: string
  variant?: 'danger' | 'warning' | 'default'
  /** If true, shows a loading spinner on the confirm button and waits for the callback to complete */
  async?: boolean
}

interface ConfirmContextType {
  confirm: (options: ConfirmOptions) => Promise<boolean>
}

const ConfirmContext = createContext<ConfirmContextType | null>(null)

export function useConfirm() {
  const ctx = useContext(ConfirmContext)
  if (!ctx) throw new Error('useConfirm must be used within ConfirmDialogProvider')
  return ctx.confirm
}

export function ConfirmDialogProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<{
    open: boolean
    options: ConfirmOptions
    resolve: ((value: boolean) => void) | null
  }>({
    open: false,
    options: { title: '', message: '' },
    resolve: null,
  })

  const confirm = useCallback((options: ConfirmOptions): Promise<boolean> => {
    return new Promise((resolve) => {
      setState({ open: true, options, resolve })
    })
  }, [])

  const handleConfirm = () => {
    state.resolve?.(true)
    setState((s) => ({ ...s, open: false, resolve: null }))
  }

  const handleCancel = () => {
    state.resolve?.(false)
    setState((s) => ({ ...s, open: false, resolve: null }))
  }

  const { options } = state
  const variant = options.variant || 'default'
  const Icon = variant === 'danger' || variant === 'warning' ? AlertTriangle : Info

  const iconColor = variant === 'danger'
    ? 'text-danger'
    : variant === 'warning'
    ? 'text-warning'
    : 'text-accent'

  const confirmBtnClass = variant === 'danger'
    ? 'bg-danger hover:bg-danger/90 text-white'
    : variant === 'warning'
    ? 'bg-warning hover:bg-warning/90 text-white'
    : 'bg-accent hover:bg-accent/90 text-white'

  return (
    <ConfirmContext.Provider value={{ confirm }}>
      {children}
      <Modal
        open={state.open}
        onClose={handleCancel}
        size="sm"
        showClose={false}
      >
        <div className="flex gap-4">
          <div className={`shrink-0 w-10 h-10 rounded-full flex items-center justify-center ${
            variant === 'danger' ? 'bg-danger/10' : variant === 'warning' ? 'bg-warning/10' : 'bg-accent/10'
          }`}>
            <Icon className={`w-5 h-5 ${iconColor}`} />
          </div>
          <div className="flex-1 min-w-0">
            <h3 className="text-base font-semibold text-text-primary">{options.title}</h3>
            <p className="text-sm text-text-secondary mt-1.5 leading-relaxed">{options.message}</p>
          </div>
        </div>
        <div className="flex justify-end gap-3 mt-6">
          <button
            onClick={handleCancel}
            className="px-4 py-2 bg-bg-primary border border-bg-border hover:bg-bg-hover text-text-primary rounded-lg font-medium text-sm transition-colors"
          >
            {options.cancelLabel || 'Cancel'}
          </button>
          <button
            onClick={handleConfirm}
            className={`px-4 py-2 rounded-lg font-medium text-sm transition-colors ${confirmBtnClass}`}
          >
            {options.confirmLabel || 'Confirm'}
          </button>
        </div>
      </Modal>
    </ConfirmContext.Provider>
  )
}
