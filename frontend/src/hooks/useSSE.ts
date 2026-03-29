import { useCallback, useRef } from 'react'
import type { SSEEvent } from '../types/plan'

export function useSSE() {
  const readerRef = useRef<ReadableStreamDefaultReader | null>(null)

  const sendMessage = useCallback(
    async (
      sessionId: string,
      message: string,
      onEvent: (event: SSEEvent) => void,
    ) => {
      const response = await fetch(`/api/chat/${sessionId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message }),
      })

      if (!response.ok || !response.body) return

      const reader = response.body.getReader()
      readerRef.current = reader
      const decoder = new TextDecoder()
      let buffer = ''

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
    },
    [],
  )

  return { sendMessage }
}
