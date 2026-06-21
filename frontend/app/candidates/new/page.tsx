'use client'

import CandidateProfileForm from '@/components/CandidateProfileForm'

export default function NewCandidatePage() {
  return (
    <div className="max-w-screen-xl mx-auto px-6 py-8">
      <div className="mb-6">
        <h1 className="text-base font-semibold text-text-primary">Create Candidate</h1>
        <p className="text-xs text-text-muted mt-1">Add a new candidate to the system.</p>
      </div>
      <CandidateProfileForm onSuccess={() => {
        window.location.href = '/candidates'
      }} />
    </div>
  )
}
