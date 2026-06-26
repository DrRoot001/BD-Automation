'use client'

import { useEffect, useRef, useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { getWebSocketConnectionDetailsAction } from '@/app/actions/auth'

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
  const activeTokenRef = useRef(0)
  
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

  useEffect(() => {
    const token = ++activeTokenRef.current

    const connect = async () => {
      if (wsRef.current && (wsRef.current.readyState === WebSocket.CONNECTING || wsRef.current.readyState === WebSocket.OPEN)) {
        return
      }

      try {
        const details = await getWebSocketConnectionDetailsAction()
        if (token !== activeTokenRef.current) return

        if (!details || !details.token) return

        let wsUrl = details.wsUrl
        try {
          const resolvedWsUrl = new URL(details.wsUrl)
          if (typeof window !== 'undefined') {
            resolvedWsUrl.hostname = window.location.hostname
          }
          wsUrl = resolvedWsUrl.toString()
        } catch (e) {
          // Fallback to config URL
        }

        const ws = new WebSocket(`${wsUrl}?token=${details.token}`)
        wsRef.current = ws

        ws.onopen = () => {
          if (token !== activeTokenRef.current) {
            ws.close()
            return
          }
          console.log('[WebSocket] Connected successfully to', wsUrl)
          delayRef.current = RECONNECT_DELAY_MS // reset backoff
        }

        ws.onmessage = (ev) => {
          if (token !== activeTokenRef.current) return
          try {
            const parsed: WSEvent = JSON.parse(ev.data as string)
            if (parsed.event === 'ping') return
            console.log('[WebSocket] Received event:', parsed.event, parsed.data)

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
          console.warn('[WebSocket] Connection closed.')
          if (token !== activeTokenRef.current) return
          onEventRef.current?.({ event: 'disconnected' })
          timerRef.current = setTimeout(() => {
            if (token !== activeTokenRef.current) return
            delayRef.current = Math.min(delayRef.current * 2, MAX_RECONNECT_DELAY_MS)
            console.log('[WebSocket] Reconnecting in', delayRef.current, 'ms')
            connect()
          }, delayRef.current)
        }

        ws.onerror = () => {
          ws.close()
        }
      } catch (err) {
        console.error('Failed to connect WebSocket:', err)
        if (token !== activeTokenRef.current) return
        timerRef.current = setTimeout(() => {
          if (token !== activeTokenRef.current) return
          connect()
        }, delayRef.current)
      }
    }

    connect()

    return () => {
      // Invalidate the current token to ignore any ongoing connection processes
      activeTokenRef.current = 0
      if (timerRef.current) clearTimeout(timerRef.current)
      if (wsRef.current) {
        wsRef.current.close()
        wsRef.current = null
      }
    }
  }, [queryClient, invalidateAll])

  return wsRef
}
