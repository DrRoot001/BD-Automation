'use client'

import CandidateProfileForm from '@/components/CandidateProfileForm'

export default function CandidatesPage() {
  return (
    <div className="p-8">
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-text-primary">Candidate Profile</h1>
        <p className="text-text-muted text-sm mt-0.5">
          Manage resume data, technical skills, and auto-apply settings.
        </p>
      </div>

      <div className="max-w-3xl">
        <CandidateProfileForm onSuccess={() => {}} />
      </div>
    </div>
  )
}
