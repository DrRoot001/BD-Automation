'use client'

import { useEffect, useRef, useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'

const WS_URL =
  typeof window !== 'undefined'
    ? `ws://${window.location.hostname}:8002/ws/updates`
    : 'ws://localhost:8002/ws/updates'

const RECONNECT_DELAY_MS = 3_000
const MAX_RECONNECT_DELAY_MS = 30_000

interface WSEvent {
  event: string
  data?: Record<string, unknown>
  timestamp?: string
}

export function useWebSocket(onEvent?: (evt: WSEvent) => void) {
  const queryClient = useQueryClient()
  const wsRef = useRef<WebSocket | null>(null)
  const delayRef = useRef(RECONNECT_DELAY_MS)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const mountedRef = useRef(true)
  
  const onEventRef = useRef(onEvent)
  useEffect(() => {
    onEventRef.current = onEvent
  }, [onEvent])

  const invalidateAll = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['kpis'] })
    queryClient.invalidateQueries({ queryKey: ['applications'] })
    queryClient.invalidateQueries({ queryKey: ['interviews'] })
    queryClient.invalidateQueries({ queryKey: ['analytics'] })
    queryClient.invalidateQueries({ queryKey: ['activity-feed'] })
  }, [queryClient])

  const connect = useCallback(() => {
    if (!mountedRef.current) return

    const ws = new WebSocket(WS_URL)
    wsRef.current = ws

    ws.onopen = () => {
      delayRef.current = RECONNECT_DELAY_MS // reset backoff
    }

    ws.onmessage = (ev) => {
      try {
        const parsed: WSEvent = JSON.parse(ev.data as string)
        if (parsed.event === 'ping') return

        // Invalidate relevant queries based on event type
        switch (parsed.event) {
          case 'connected':
            break
          case 'application.created':
          case 'application.status_changed':
          case 'application.submitted':
          case 'application.failed':
            queryClient.invalidateQueries({ queryKey: ['kpis'] })
            queryClient.invalidateQueries({ queryKey: ['applications'] })
            queryClient.invalidateQueries({ queryKey: ['activity-feed'] })
            break
          case 'email.classified':
            queryClient.invalidateQueries({ queryKey: ['kpis'] })
            queryClient.invalidateQueries({ queryKey: ['applications'] })
            queryClient.invalidateQueries({ queryKey: ['activity-feed'] })
            break
          case 'interview.detected':
            queryClient.invalidateQueries({ queryKey: ['interviews'] })
            queryClient.invalidateQueries({ queryKey: ['kpis'] })
            queryClient.invalidateQueries({ queryKey: ['activity-feed'] })
            break
          default:
            invalidateAll()
        }

        onEventRef.current?.(parsed)
      } catch {
        // ignore parse errors
      }
    }

    ws.onclose = () => {
      if (!mountedRef.current) return
      onEventRef.current?.({ event: 'disconnected' })
      timerRef.current = setTimeout(() => {
        delayRef.current = Math.min(delayRef.current * 2, MAX_RECONNECT_DELAY_MS)
        connect()
      }, delayRef.current)
    }

    ws.onerror = () => {
      ws.close()
    }
  }, [queryClient, invalidateAll])

  useEffect(() => {
    mountedRef.current = true
    connect()
    return () => {
      mountedRef.current = false
      if (timerRef.current) clearTimeout(timerRef.current)
      wsRef.current?.close()
    }
  }, [connect])

  return wsRef
}
