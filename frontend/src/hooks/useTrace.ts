import { useState, useEffect, useCallback } from 'react'
import type { SessionTrace } from '../types/trace'

export function useTrace(sessionId: string | null, refreshTrigger?: number) {
  const [trace, setTrace] = useState<SessionTrace | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    if (!sessionId) {
      setTrace(null)
      return
    }
    setLoading(true)
    setError(null)
    try {
      const resp = await fetch(`/api/sessions/${sessionId}/trace`)
      if (!resp.ok) {
        throw new Error(`Failed to fetch trace: ${resp.status}`)
      }
      const data = (await resp.json()) as SessionTrace
      setTrace(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Unknown error')
      setTrace(null)
    } finally {
      setLoading(false)
    }
  }, [sessionId])

  useEffect(() => {
    void refresh()
  }, [refresh, refreshTrigger])

  return { trace, loading, error, refresh }
}
