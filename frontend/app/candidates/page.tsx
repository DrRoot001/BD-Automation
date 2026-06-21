'use client'

import CandidateProfileForm from '@/components/CandidateProfileForm'

export default function CandidatesPage() {
  return (
    <div className="max-w-screen-xl mx-auto px-6 py-8">
      <div className="mb-6">
        <h1 className="text-base font-semibold text-text-primary">Candidate Profile</h1>
        <p className="text-xs text-text-muted mt-1">Manage resume data, skills, and auto-apply settings.</p>
      </div>
      <CandidateProfileForm onSuccess={() => {}} />
    </div>
  )
}
