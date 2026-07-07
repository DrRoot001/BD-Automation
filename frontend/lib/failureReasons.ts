// Human-readable copy for every backend FailureReason code — raw enum values
// and internal error strings must never reach the user.
export const FAILURE_INFO: Record<string, { title: string; description: string }> = {
  JOB_EXPIRED: {
    title: 'Job Posting Expired',
    description: 'This job posting has been removed or expired. The position is no longer available.',
  },
  ROBOTS_BLOCKED: {
    title: 'Robots Policy Block',
    description: 'This job portal does not permit automated access. Please apply manually.',
  },
  EMAIL_VERIFICATION: {
    title: 'Submitted — Email Verification Timed Out',
    description: 'The application form was submitted, but the post-submit email verification step timed out. Check the portal or candidate inbox to confirm receipt before re-applying — re-submitting may create a duplicate application.',
  },
  LOGIN_REQUIRED: {
    title: 'Portal Login Required',
    description: 'This job portal requires an account login that is not configured yet. Add portal credentials or apply manually.',
  },
  BOT_DETECTED: {
    title: 'Automation Detected by Portal',
    description: 'The job portal blocked automated access for this posting. You can submit the application manually.',
  },
  FORM_INCOMPLETE: {
    title: 'Form Could Not Be Completed',
    description: 'Some required form fields could not be filled automatically. A retry may succeed, or you can apply manually.',
  },
  INFRA_ERROR: {
    title: 'System Error',
    description: 'A system error interrupted this application after several attempts. Use Retry to run it again, or apply manually.',
  },
  QUALIFICATION_MISMATCH: {
    title: 'Qualification Mismatch',
    description: 'The portal indicated the candidate does not meet a hard requirement for this role.',
  },
  SPAM_FLAGGED: {
    title: 'Rate Limit Cooldown',
    description: 'The portal flagged high application volume. New applications to this portal are paused temporarily; this application will not be retried automatically — apply manually if the posting is still open.',
  },
  ALREADY_APPLIED: {
    title: 'Already Applied',
    description: 'The portal reports an application already exists for this candidate and job.',
  },
  GMAIL_NOT_CONNECTED: {
    title: 'Gmail Not Connected',
    description: 'This portal requires an email verification step, but the candidate has no connected Gmail account. Connect Gmail from the candidate profile.',
  },
}
