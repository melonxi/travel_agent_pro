import { useState, useRef, useEffect } from 'react'
import MessageBubble from './MessageBubble'
import { useSSE } from '../hooks/useSSE'
import type { SSEEvent, TravelPlanState } from '../types/plan'

interface ChatMessage {
  id: string
  role: 'user' | 'assistant' | 'tool'
  content: string
  toolCallId?: string
  toolName?: string
  toolStatus?: 'pending' | 'success' | 'error' | 'skipped'
  toolArguments?: Record<string, unknown>
  toolResult?: unknown
  toolError?: string
  toolSuggestion?: string
}

interface Props {
  sessionId: string
  onPlanUpdate: (plan: TravelPlanState) => void
}

export default function ChatPanel({ sessionId, onPlanUpdate }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [autoScroll, setAutoScroll] = useState(true)
  const bottomRef = useRef<HTMLDivElement>(null)
  const messagesRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const sendingRef = useRef(false)
  const { sendMessage } = useSSE()

  const createMessageId = () =>
    `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`

  useEffect(() => {
    if (autoScroll) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [messages, autoScroll])

  useEffect(() => {
    if (!streaming) {
      inputRef.current?.focus()
    }
  }, [streaming])

  const handleScroll = () => {
    if (!messagesRef.current) return
    const { scrollTop, scrollHeight, clientHeight } = messagesRef.current
    setAutoScroll(scrollHeight - scrollTop - clientHeight < 50)
  }

  const insertBeforeAssistant = (
    prev: ChatMessage[],
    assistantId: string,
    toolMessage: ChatMessage,
  ) => {
    const assistantIndex = prev.findIndex((message) => message.id === assistantId)
    if (assistantIndex === -1) {
      return [...prev, toolMessage]
    }
    return [
      ...prev.slice(0, assistantIndex),
      toolMessage,
      ...prev.slice(assistantIndex),
    ]
  }

  const handleSend = async () => {
    if (!input.trim() || sendingRef.current) return

    sendingRef.current = true
    const userMsg = input.trim()
    const assistantId = createMessageId()
    const toolMessageIds = new Map<string, string>()
    setInput('')
    setMessages((prev) => [
      ...prev,
      { id: createMessageId(), role: 'user', content: userMsg },
      { id: assistantId, role: 'assistant', content: '' },
    ])
    setStreaming(true)

    let assistantContent = ''

    try {
      await sendMessage(sessionId, userMsg, (event: SSEEvent) => {
        if (event.type === 'text_delta' && event.content) {
          assistantContent += event.content
          setMessages((prev) =>
            prev.map((message) =>
              message.id === assistantId
                ? { ...message, content: assistantContent }
                : message,
            ),
          )
        } else if (event.type === 'tool_call' && event.tool_call) {
          const toolCall = event.tool_call
          const toolMessageId = createMessageId()
          toolMessageIds.set(toolCall.id, toolMessageId)
          setMessages((prev) =>
            insertBeforeAssistant(prev, assistantId, {
              id: toolMessageId,
              role: 'tool',
              content: '',
              toolCallId: toolCall.id,
              toolName: toolCall.name,
              toolStatus: 'pending',
              toolArguments: toolCall.arguments,
            }),
          )
        } else if (event.type === 'tool_result' && event.tool_result) {
          const toolResult = event.tool_result
          const toolMessageId = toolMessageIds.get(toolResult.tool_call_id)
          setMessages((prev) => {
            if (!toolMessageId) {
              return insertBeforeAssistant(prev, assistantId, {
                id: createMessageId(),
                role: 'tool',
                content: '',
                toolCallId: toolResult.tool_call_id,
                toolName: toolResult.tool_call_id,
                toolStatus: toolResult.status,
                toolResult: toolResult.data,
                toolError: toolResult.error ?? undefined,
                toolSuggestion: toolResult.suggestion ?? undefined,
              })
            }

            return prev.map((message) =>
              message.id === toolMessageId
                ? {
                    ...message,
                    toolStatus: toolResult.status,
                    toolResult: toolResult.data,
                    toolError: toolResult.error ?? undefined,
                    toolSuggestion: toolResult.suggestion ?? undefined,
                  }
                : message,
            )
          })
        } else if (event.type === 'state_update' && event.plan) {
          onPlanUpdate(event.plan)
        }
      })
    } finally {
      sendingRef.current = false
      setStreaming(false)
      if (!assistantContent.trim()) {
        setMessages((prev) => prev.filter((message) => message.id !== assistantId))
      }
    }
  }

  const lastMsg = messages[messages.length - 1]

  return (
    <div className="chat-panel">
      <div className="messages" ref={messagesRef} onScroll={handleScroll}>
        {messages.map((m, i) => (
          <MessageBubble
            key={m.id || String(i)}
            role={m.role}
            content={m.content}
            toolName={m.toolName}
            toolStatus={m.toolStatus}
            toolArguments={m.toolArguments}
            toolResult={m.toolResult}
            toolError={m.toolError}
            toolSuggestion={m.toolSuggestion}
          />
        ))}
        {streaming && lastMsg?.role === 'assistant' && (
          <span className="streaming-cursor" />
        )}
        <div ref={bottomRef} />
      </div>
      <div className="input-bar">
        <div className="input-wrapper">
          <input
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key !== 'Enter' || e.nativeEvent.isComposing) return
              e.preventDefault()
              void handleSend()
            }}
            placeholder="告诉我你想去哪里..."
            disabled={streaming}
          />
        </div>
        <button type="button" className="send-btn" onClick={() => void handleSend()} disabled={streaming || !input.trim()}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="22" y1="2" x2="11" y2="13" />
            <polygon points="22 2 15 22 11 13 2 9 22 2" />
          </svg>
        </button>
      </div>
    </div>
  )
}
