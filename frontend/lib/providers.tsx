'use client'

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useState } from 'react'

// Module-level reference to the active QueryClient.
// This allows imperative cache clearing on logout (queryClient.clear())
// without needing to pass it through props or context.
let _queryClient: QueryClient | null = null

export function getQueryClient(): QueryClient | null {
  return _queryClient
}

export function ReactQueryProvider({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(() => {
    const client = new QueryClient({
      defaultOptions: {
        queries: {
          staleTime: 30_000,
          refetchOnWindowFocus: true,
        },
      },
    })
    // Store module-level reference so logout handler can clear cache
    _queryClient = client
    return client
  })

  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
}
