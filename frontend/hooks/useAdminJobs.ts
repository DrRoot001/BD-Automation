import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'

export function useAdminJobs(
  skip: number = 0, 
  limit: number = 100,
  filters?: { search?: string; source?: string; jobType?: string; timeFilter?: string }
) {
  return useQuery({
    queryKey: ['adminJobs', skip, limit, filters],
    queryFn: () => api.getJobs({ skip, limit, ...filters }),
    staleTime: 5 * 60 * 1000, // 5 minutes
  })
}

export function useAdminJobsCount(
  filters?: { search?: string; source?: string; jobType?: string; timeFilter?: string }
) {
  return useQuery({
    queryKey: ['adminJobsCount', filters],
    queryFn: () => api.getJobsCount(filters),
    staleTime: 5 * 60 * 1000, // 5 minutes
  })
}
