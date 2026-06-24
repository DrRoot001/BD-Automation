import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'

export function useAdminJobs(skip: number = 0, limit: number = 100) {
  return useQuery({
    queryKey: ['adminJobs', skip, limit],
    queryFn: () => api.getJobs({ skip, limit }),
    staleTime: 5 * 60 * 1000, // 5 minutes
  })
}
