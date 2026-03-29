import { useState, useRef, useEffect } from 'react'
import MessageBubble from './MessageBubble'
import { useSSE } from '../hooks/useSSE'
import type { SSEEvent, TravelPlanState } from '../types/plan'

interface ChatMessage {
  role: 'user' | 'assistant' | 'tool'
  content: string
  toolName?: string
}

interface Props {
  sessionId: string
  onPlanUpdate: (plan: TravelPlanState) => void
}

export default function ChatPanel({ sessionId, onPlanUpdate }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)
  const { sendMessage } = useSSE()

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const handleSend = async () => {
    if (!input.trim() || streaming) return
    const userMsg = input.trim()
    setInput('')
    setMessages((prev) => [...prev, { role: 'user', content: userMsg }])
    setStreaming(true)

    let assistantContent = ''

    await sendMessage(sessionId, userMsg, (event: SSEEvent) => {
      if (event.type === 'text_delta' && event.content) {
        assistantContent += event.content
        setMessages((prev) => {
          const copy = [...prev]
          const last = copy[copy.length - 1]
          if (last?.role === 'assistant') {
            copy[copy.length - 1] = { ...last, content: assistantContent }
          } else {
            copy.push({ role: 'assistant', content: assistantContent })
          }
          return copy
        })
      } else if (event.type === 'tool_call' && event.tool_call) {
        setMessages((prev) => [
          ...prev,
          { role: 'tool', content: '', toolName: event.tool_call!.name },
        ])
      } else if (event.type === 'state_update' && event.plan) {
        onPlanUpdate(event.plan)
      }
    })

    setStreaming(false)
  }

  return (
    <div className="chat-panel">
      <div className="messages">
        {messages.map((m, i) => (
          <MessageBubble key={i} role={m.role} content={m.content} toolName={m.toolName} />
        ))}
        <div ref={bottomRef} />
      </div>
      <div className="input-bar">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleSend()}
          placeholder="输入你的旅行想法..."
          disabled={streaming}
        />
        <button onClick={handleSend} disabled={streaming}>
          发送
        </button>
      </div>
    </div>
  )
}
