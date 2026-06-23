import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'

export function useApplicationHistory(applicationId: string | null) {
  return useQuery({
    queryKey: ['applicationHistory', applicationId],
    queryFn: () => api.getApplicationHistory(applicationId!),
    enabled: !!applicationId,
    staleTime: 60 * 1000,
  })
}
