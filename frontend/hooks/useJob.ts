import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'

export function useJob(jobId: string | null) {
  return useQuery({
    queryKey: ['job', jobId],
    queryFn: () => api.getJob(jobId!),
    enabled: !!jobId,
    staleTime: 60 * 1000,
  })
}
