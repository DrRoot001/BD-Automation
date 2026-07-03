export default function Loading() {
  return (
    <div className="space-y-6 max-w-7xl mx-auto p-2 animate-pulse">
      <div className="flex justify-between items-center pb-4 border-b border-bg-border">
        <div className="space-y-2">
          <div className="h-7 bg-bg-border rounded w-48" />
          <div className="h-4 bg-bg-border rounded w-72" />
        </div>
        <div className="h-9 bg-bg-border rounded w-32" />
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="h-28 bg-bg-border rounded-xl" />
        ))}
      </div>

      <div className="h-80 bg-bg-border rounded-xl" />
    </div>
  )
}
