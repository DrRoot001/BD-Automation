import { clsx } from 'clsx'

export type StatusType = 
  | 'FOUND' 
  | 'ANALYZED' 
  | 'MATCHED' 
  | 'QUEUED'
  | 'APPLICATION_STARTED'
  | 'FORM_COMPLETED'
  | 'RESUME_UPDATED'
  | 'COVER_LETTER_CREATED'
  | 'SUBMITTED' 
  | 'CONFIRMED'
  | 'INTERVIEW_R1' 
  | 'INTERVIEW_R2'
  | 'INTERVIEW_R3'
  | 'INTERVIEW_R4'
  | 'ASSESSMENT'
  | 'OFFER' 
  | 'REJECTED' 
  | 'FAILED'
  | 'BLOCKED'
  | 'GHOSTED'
  | 'WITHDRAWN'

interface StatusBadgeProps {
  status: StatusType | string
  className?: string
}

export function StatusBadge({ status, className }: StatusBadgeProps) {
  let colorClass = 'bg-bg-secondary text-text-muted border border-bg-border'

  const normalized = (status || '').toUpperCase()

  switch (normalized) {
    case 'FOUND':
    case 'ANALYZED':
      colorClass = 'bg-bg-secondary text-text-muted border border-bg-border'
      break
    case 'MATCHED':
      colorClass = 'bg-success/10 text-success border border-success/20'
      break
    case 'QUEUED':
    case 'APPLICATION_STARTED':
    case 'FORM_COMPLETED':
    case 'RESUME_UPDATED':
    case 'COVER_LETTER_CREATED':
      colorClass = 'bg-warning/10 text-warning border border-warning/20'
      break
    case 'SUBMITTED':
      colorClass = 'bg-info/10 text-info border border-info/20'
      break
    case 'CONFIRMED':
      colorClass = 'bg-success/10 text-success border border-success/20'
      break
    case 'REJECTED':
    case 'FAILED':
    case 'BLOCKED':
    case 'GHOSTED':
    case 'WITHDRAWN':
      colorClass = 'bg-danger/10 text-danger border border-danger/20'
      break
    case 'OFFER':
      colorClass = 'bg-amber-500/10 text-amber-500 border border-amber-500/20 font-bold'
      break
    case 'INTERVIEW_R1':
    case 'INTERVIEW_R2':
    case 'INTERVIEW_R3':
    case 'INTERVIEW_R4':
    case 'ASSESSMENT':
      colorClass = 'bg-purple/10 text-purple border border-purple/20 font-bold'
      break
  }

  return (
    <span className={clsx('badge', colorClass, className)}>
      {normalized.replace(/_/g, ' ')}
    </span>
  )
}
