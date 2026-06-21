'use client'

import { ApplicationsQueue } from '@/components/ApplicationsQueue'

export default function ApplicationsPage() {
  return (
    <div className="p-8">
      <div className="mb-8">
        <h1 className="text-base font-semibold text-text-primary">Applications</h1>
        <p className="text-xs text-text-muted mt-1">All job applications across every candidate.</p>
      </div>
      <ApplicationsQueue />
    </div>
  )
}
