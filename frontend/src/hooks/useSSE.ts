import { useCallback, useRef } from 'react'
import type { SSEEvent } from '../types/plan'

export function useSSE() {
  const chatAbortRef = useRef<AbortController | null>(null)

  const streamSSE = async (
    url: string,
    fetchOptions: RequestInit,
    onEvent: (event: SSEEvent) => void,
    controller: AbortController,
  ) => {
    const response = await fetch(url, {
      ...fetchOptions,
      signal: controller.signal,
    })

    if (!response.ok || !response.body) return

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try {
              const event: SSEEvent = JSON.parse(line.slice(6))
              onEvent(event)
            } catch {
              // skip malformed events
            }
          }
        }
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') {
        return
      }
      throw err
    }
  }

  const sendMessage = useCallback(
    async (
      sessionId: string,
      message: string,
      onEvent: (event: SSEEvent) => void,
    ) => {
      const controller = new AbortController()
      chatAbortRef.current = controller
      await streamSSE(
        `/api/chat/${sessionId}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message }),
        },
        onEvent,
        controller,
      )
    },
    [],
  )

  const subscribe = useCallback((
    url: string,
    onEvent: (event: SSEEvent) => void,
    onError?: (error: unknown) => void,
  ) => {
    const source = new EventSource(url)

    source.onmessage = (message) => {
      try {
        const event: SSEEvent = JSON.parse(message.data)
        onEvent(event)
      } catch {
        // skip malformed events
      }
    }

    source.onerror = (error) => {
      onError?.(error)
    }

    return () => {
      source.close()
    }
  }, [])

  const cancel = useCallback(async (sessionId: string) => {
    chatAbortRef.current?.abort()
    chatAbortRef.current = null
    try {
      await fetch(`/api/chat/${sessionId}/cancel`, { method: 'POST' })
    } catch {
      // cancel 请求失败不阻塞 UI
    }
  }, [])

  const continueGeneration = useCallback(
    async (
      sessionId: string,
      onEvent: (event: SSEEvent) => void,
    ) => {
      const controller = new AbortController()
      chatAbortRef.current = controller
      await streamSSE(
        `/api/chat/${sessionId}/continue`,
        { method: 'POST' },
        onEvent,
        controller,
      )
    },
    [],
  )

  return { sendMessage, subscribe, cancel, continueGeneration }
}
