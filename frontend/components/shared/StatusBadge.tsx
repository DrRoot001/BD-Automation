import { clsx } from 'clsx'

type StatusType = 
  | 'FOUND' 
  | 'ANALYZED' 
  | 'MATCHED' 
  | 'QUEUED' 
  | 'SUBMITTED' 
  | 'REJECTED' 
  | 'OFFER' 
  | 'INTERVIEW_R1' 
  | 'INTERVIEW_R2'

interface StatusBadgeProps {
  status: StatusType | string
  className?: string
}

export function StatusBadge({ status, className }: StatusBadgeProps) {
  let colorClass = 'bg-bg-hover text-text-muted border border-bg-border' // default

  switch (status) {
    case 'FOUND':
    case 'ANALYZED':
      colorClass = 'bg-bg-hover text-text-muted border border-bg-border'
      break
    case 'MATCHED':
      colorClass = 'bg-success/10 text-success border border-success/20'
      break
    case 'QUEUED':
      colorClass = 'bg-warning/10 text-warning border border-warning/20'
      break
    case 'SUBMITTED':
      colorClass = 'bg-accent/10 text-accent border border-accent/20'
      break
    case 'REJECTED':
    case 'FAILED':
    case 'BLOCKED':
      colorClass = 'bg-danger/10 text-danger border border-danger/20'
      break
    case 'OFFER':
      colorClass = 'bg-success/10 text-success border border-success/20'
      break
    case 'INTERVIEW_R1':
    case 'INTERVIEW_R2':
      colorClass = 'bg-purple/10 text-purple border border-purple/20'
      break
  }

  return (
    <span className={clsx('px-2.5 py-0.5 rounded-full text-[10px] font-semibold uppercase tracking-wider', colorClass, className)}>
      {status.replace('_', ' ')}
    </span>
  )
}
