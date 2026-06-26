import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'

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
      // Fetch user using server action to read httpOnly cookie
      return await getCurrentUserAction();
    },
    staleTime: 5 * 60 * 1000, // 5 minutes
    retry: false
  })
}
