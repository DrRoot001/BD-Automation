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
  let colorClass = 'bg-zinc-800 text-zinc-300' // default / FOUND

  switch (status) {
    case 'FOUND':
      colorClass = 'bg-zinc-800 text-zinc-300' // gray
      break
    case 'ANALYZED':
      colorClass = 'bg-blue-900/50 text-blue-400' // blue
      break
    case 'MATCHED':
      colorClass = 'bg-green-900/50 text-green-400' // green
      break
    case 'QUEUED':
      colorClass = 'bg-yellow-900/50 text-yellow-500' // yellow
      break
    case 'SUBMITTED':
      colorClass = 'bg-teal-900/50 text-teal-400' // teal
      break
    case 'REJECTED':
      colorClass = 'bg-red-900/50 text-red-400' // red
      break
    case 'OFFER':
      colorClass = 'bg-amber-700/50 text-amber-400' // gold
      break
    case 'INTERVIEW_R1':
    case 'INTERVIEW_R2':
      colorClass = 'bg-purple-900/50 text-purple-400' // purple
      break
  }

  return (
    <span className={clsx('px-2.5 py-0.5 rounded-full text-xs font-medium uppercase tracking-wider', colorClass, className)}>
      {status.replace('_', ' ')}
    </span>
  )
}
