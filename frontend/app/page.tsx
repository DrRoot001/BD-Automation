import { redirect } from 'next/navigation'

/**
 * Root page — middleware handles role-based redirect for authenticated users.
 * This fallback only triggers if middleware lets an unauthenticated request through
 * (shouldn't happen in production, but safety net).
 */
export default function Home() {
  redirect('/login')
}
