'use client'

import { ApplicationsQueue } from '@/components/ApplicationsQueue'

export default function AutomationPage() {
  return (
    <div className="p-8">
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-text-primary">Browser Automation</h1>
        <p className="text-text-muted text-sm mt-0.5">
          Monitor the Playwright bot's active job submission queue.
        </p>
      </div>

      <div className="max-w-4xl space-y-6">
        <ApplicationsQueue />
      </div>
    </div>
  )
}
