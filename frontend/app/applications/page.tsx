'use client'

import { ApplicationsQueue } from '@/components/dashboard/ApplicationsQueue'

export default function ApplicationsPage() {
  return (
    <div className="p-4 sm:p-8">
      <div className="mb-6">
        <h1 className="text-base font-semibold text-text-primary">Applications</h1>
        <p className="text-xs text-text-muted mt-1">All job applications across every candidate.</p>
      </div>
      <ApplicationsQueue />
    </div>
  )
}
