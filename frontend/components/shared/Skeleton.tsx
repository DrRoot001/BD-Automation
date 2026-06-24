import { clsx } from 'clsx'

function Skeleton({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={clsx("skeleton", className)}
      {...props}
    />
  )
}

export { Skeleton }
