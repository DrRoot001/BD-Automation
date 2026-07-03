import { useQuery } from '@tanstack/react-query'

import { getCurrentUserAction } from '@/app/actions/auth'

export interface CurrentUser {
  id: string
  email: string
  name: string
  full_name?: string
  role: string
}

export function useCurrentUser() {
  return useQuery<CurrentUser | null>({
    queryKey: ['currentUser'],
    queryFn: async () => {
      return await getCurrentUserAction()
    },
    staleTime: 5 * 60_000,  // Identity/role rarely changes mid-session — avoid a server-action round-trip per navigation
    gcTime: 5 * 60_000,     // Keep cached for 5 minutes after all subscribers unmount
    refetchOnWindowFocus: true,
    // The action now THROWS on backend hiccups (timeout/5xx) instead of
    // returning null, so a failed refetch keeps the last-known identity
    // (React Query retains previous data on error) rather than demoting an
    // admin to the BD-user nav. null is reserved for "actually logged out".
    retry: 2,
    retryDelay: 1500,
  })
}
