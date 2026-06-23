import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'

interface UseApplicationsOptions {
  candidateId?: string
  status?: string
  limit?: number
  offset?: number
}

export function useApplications(options: UseApplicationsOptions = {}) {
  return useQuery({
    queryKey: ['applications', options],
    queryFn: () => api.getApplications(options),
    staleTime: 60 * 1000,
  })
}
