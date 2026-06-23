import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'

interface UseJobsOptions {
  skip?: number
  limit?: number
}

export function useJobs(options: UseJobsOptions = {}) {
  return useQuery({
    queryKey: ['jobs', options],
    queryFn: () => api.getJobs(options),
    staleTime: 60 * 1000,
  })
}
