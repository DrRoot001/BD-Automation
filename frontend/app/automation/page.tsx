'use client'

import { ApplicationsQueue } from '@/components/ApplicationsQueue'

const IN_PROGRESS_STATUSES = ['QUEUED', 'APPLICATION_STARTED', 'FORM_COMPLETED']

export default function AutomationPage() {
  return (
    <div className="p-8">
      <div className="mb-8">
        <h1 className="text-base font-semibold text-text-primary">Browser Automation</h1>
        <p className="text-xs text-text-muted mt-1">
          Applications the Playwright bot is actively queued or working on right now.
        </p>
      </div>

      <div className="max-w-4xl space-y-6">
        <ApplicationsQueue
          statusFilter={IN_PROGRESS_STATUSES}
          emptyMessage="No applications currently in progress. Go to Candidates to start the auto-apply pipeline."
        />
      </div>
    </div>
  )
}
