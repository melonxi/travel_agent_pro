import { useState, useRef, useEffect } from 'react'
import MessageBubble from './MessageBubble'
import { useSSE } from '../hooks/useSSE'
import type { SSEEvent, TravelPlanState } from '../types/plan'
import type { SessionMessage } from '../types/session'

interface StateChange {
  icon: string
  label: string
  value: string
}

interface ChatMessage {
  id: string
  role: 'user' | 'assistant' | 'tool' | 'system'
  content: string
  toolCallId?: string
  toolName?: string
  toolStatus?: 'pending' | 'success' | 'error' | 'skipped'
  toolArguments?: Record<string, unknown>
  toolResult?: unknown
  toolError?: string
  toolSuggestion?: string
  stateChanges?: StateChange[]
  compressionInfo?: {
    message_count_before: number
    message_count_after: number
    must_keep_count: number
    compressed_count: number
    estimated_tokens_before: number
    reason: string
  }
}

const PHASE_NAMES: Record<number, string> = {
  1: '需求收集', 2: '信息探索', 3: '方案设计', 4: '精细规划', 5: '最终确认',
}

function computeStateChanges(
  prev: TravelPlanState | null,
  next: TravelPlanState,
): StateChange[] {
  const changes: StateChange[] = []

  if (next.phase !== prev?.phase) {
    changes.push({ icon: '🔄', label: '阶段', value: PHASE_NAMES[next.phase] ?? `Phase ${next.phase}` })
  }
  if (next.destination && next.destination !== prev?.destination) {
    changes.push({ icon: '📍', label: '目的地', value: next.destination })
  }
  if (next.dates && JSON.stringify(next.dates) !== JSON.stringify(prev?.dates)) {
    changes.push({ icon: '📅', label: '日期', value: `${next.dates.start} → ${next.dates.end}` })
  }
  if (next.budget && next.budget.total !== prev?.budget?.total) {
    changes.push({ icon: '💰', label: '预算', value: `¥${next.budget.total.toLocaleString()}` })
  }
  if (next.travelers && JSON.stringify(next.travelers) !== JSON.stringify(prev?.travelers)) {
    const t = next.travelers
    const parts: string[] = []
    if (t.adults) parts.push(`${t.adults}成人`)
    if (t.children) parts.push(`${t.children}儿童`)
    if (parts.length) changes.push({ icon: '👥', label: '旅行者', value: parts.join(' ') })
  }
  if (next.accommodation && JSON.stringify(next.accommodation) !== JSON.stringify(prev?.accommodation)) {
    changes.push({ icon: '🏨', label: '住宿', value: next.accommodation.hotel ?? next.accommodation.area })
  }

  const countField = (
    field: 'candidate_pool' | 'shortlist' | 'skeleton_plans' | 'daily_plans' | 'risks' | 'constraints' | 'preferences',
    icon: string,
    label: string,
    unit: string,
  ) => {
    const prevLen = (prev?.[field] as unknown[] | undefined)?.length ?? 0
    const nextLen = (next[field] as unknown[] | undefined)?.length ?? 0
    if (nextLen > 0 && nextLen !== prevLen) {
      changes.push({ icon, label, value: `${nextLen} ${unit}` })
    }
  }
  countField('candidate_pool', '🎯', '候选景点', '个')
  countField('shortlist', '⭐', '精选景点', '个')
  countField('skeleton_plans', '📋', '方案草案', '个')
  countField('daily_plans', '🗓️', '每日行程', '天')
  countField('risks', '⚠️', '风险提示', '项')
  countField('constraints', '📌', '约束条件', '项')
  countField('preferences', '💡', '偏好', '项')

  return changes
}

interface Props {
  sessionId: string
  onPlanUpdate: (plan: TravelPlanState) => void
  onMemoryRecall?: (itemIds: string[]) => void
}

interface EventHandlerState {
  currentAssistantId: string
  assistantContent: string
  toolMessageIds: Map<string, string>
}

export default function ChatPanel({ sessionId, onPlanUpdate, onMemoryRecall }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [autoScroll, setAutoScroll] = useState(true)
  const bottomRef = useRef<HTMLDivElement>(null)
  const messagesRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  
  const prevPlanRef = useRef<TravelPlanState | null>(null)
  const lastEventTimeRef = useRef<number>(Date.now())
  const [connectionWarning, setConnectionWarning] = useState(false)
  const { sendMessage, cancel, continueGeneration } = useSSE()
  const [canContinue, setCanContinue] = useState(false)

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

  const KEEPALIVE_TIMEOUT_MS = 30_000
  const KEEPALIVE_CHECK_INTERVAL_MS = 5_000

  useEffect(() => {
    if (!streaming) return
    const timer = setInterval(() => {
      if (Date.now() - lastEventTimeRef.current > KEEPALIVE_TIMEOUT_MS) {
        setConnectionWarning(true)
      }
    }, KEEPALIVE_CHECK_INTERVAL_MS)
    return () => clearInterval(timer)
  }, [streaming])

  useEffect(() => {
    let cancelled = false

    const restoreMessages = async () => {
      try {
        const response = await fetch(`/api/messages/${sessionId}`)
        if (!response.ok) {
          throw new Error(`Failed to load messages for ${sessionId}`)
        }

        const data = (await response.json()) as SessionMessage[]
        if (cancelled) return

        const toolNameByCallId = new Map<string, string>()
        for (const entry of data) {
          if (entry.role !== 'assistant' || !entry.tool_calls) continue
          for (const toolCall of entry.tool_calls) {
            toolNameByCallId.set(toolCall.id, toolCall.name)
          }
        }

        const restored: ChatMessage[] = []
        for (const entry of data) {
          if (entry.role === 'system') continue

          if (entry.role === 'tool') {
            const toolCallId = entry.tool_call_id ?? undefined
            const toolName = toolCallId
              ? (toolNameByCallId.get(toolCallId) ?? toolCallId)
              : 'tool'
            let toolResult: unknown = entry.content ?? ''
            if (entry.content) {
              try {
                toolResult = JSON.parse(entry.content)
              } catch {
                toolResult = entry.content
              }
            }

            restored.push({
              id: createMessageId(),
              role: 'tool',
              content: entry.content ?? '',
              toolCallId,
              toolName,
              toolStatus: 'success',
              toolResult,
            })
            continue
          }

          restored.push({
            id: createMessageId(),
            role: entry.role,
            content: entry.content ?? '',
          })
        }

        setMessages(restored)
        prevPlanRef.current = null
        setAutoScroll(true)
      } catch {
        if (!cancelled) {
          setMessages([])
          prevPlanRef.current = null
        }
      }
    }

    void restoreMessages()
    return () => {
      cancelled = true
    }
  }, [sessionId])

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

  const handleStop = async () => {
    try {
      await cancel(sessionId)
    } finally {
      setStreaming(false)
    }
  }

  const createEventHandler = (state: EventHandlerState) => (event: SSEEvent) => {
    lastEventTimeRef.current = Date.now()
    setConnectionWarning(false)

    if (event.type === 'text_delta' && event.content) {
      state.assistantContent += event.content
      const targetId = state.currentAssistantId
      setMessages((prev) => {
        const exists = prev.some((m) => m.id === targetId)
        if (!exists) {
          return [...prev, { id: targetId, role: 'assistant' as const, content: state.assistantContent }]
        }
        return prev.map((message) =>
          message.id === targetId
            ? { ...message, content: state.assistantContent }
            : message,
        )
      })
    } else if (event.type === 'tool_call' && event.tool_call) {
      const toolCall = event.tool_call
      const toolMessageId = createMessageId()
      state.toolMessageIds.set(toolCall.id, toolMessageId)
      const toolMsg: ChatMessage = {
        id: toolMessageId,
        role: 'tool',
        content: '',
        toolCallId: toolCall.id,
        toolName: toolCall.name,
        toolStatus: 'pending',
        toolArguments: toolCall.arguments,
      }

      if (state.assistantContent.trim()) {
        const newAssistantId = createMessageId()
        state.currentAssistantId = newAssistantId
        state.assistantContent = ''
        setMessages((prev) => [...prev, toolMsg])
      } else {
        setMessages((prev) =>
          insertBeforeAssistant(prev, state.currentAssistantId, toolMsg),
        )
      }
    } else if (event.type === 'tool_result' && event.tool_result) {
      const toolResult = event.tool_result
      const toolMessageId = state.toolMessageIds.get(toolResult.tool_call_id)
      setMessages((prev) => {
        if (!toolMessageId) {
          return insertBeforeAssistant(prev, state.currentAssistantId, {
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
    } else if (event.type === 'context_compression' && event.compression_info) {
      const info = event.compression_info
      setMessages((prev) =>
        insertBeforeAssistant(prev, state.currentAssistantId, {
          id: createMessageId(),
          role: 'system',
          content: `${info.message_count_before} 条 → ${info.message_count_after} 条`,
          compressionInfo: info,
        }),
      )
    } else if (event.type === 'state_update' && event.plan) {
      const changes = computeStateChanges(prevPlanRef.current, event.plan)
      prevPlanRef.current = event.plan
      onPlanUpdate(event.plan)
      if (changes.length > 0) {
        setMessages((prev) => [
          ...prev,
          {
            id: createMessageId(),
            role: 'system',
            content: '',
            stateChanges: changes,
          },
        ])
      }
    } else if (event.type === 'memory_recall' && event.item_ids) {
      onMemoryRecall?.(event.item_ids)
    } else if (event.type === 'error') {
      const message = event.message ?? '模型服务暂时不可用，请稍后重试。'
      const detail = event.error ? `\n\n${event.error}` : ''
      setMessages((prev) =>
        prev.map((item) =>
          item.id === state.currentAssistantId
            ? { ...item, content: `${message}${detail}` }
            : item,
        ),
      )
      if (event.can_continue) {
        setCanContinue(true)
      }
    }
  }

  const handleSend = async () => {
    if (!input.trim() || streaming) return

    lastEventTimeRef.current = Date.now()
    setConnectionWarning(false)
    setCanContinue(false)
    const userMsg = input.trim()
    const state: EventHandlerState = {
      currentAssistantId: createMessageId(),
      assistantContent: '',
      toolMessageIds: new Map<string, string>(),
    }
    setInput('')
    setMessages((prev) => [
      ...prev,
      { id: createMessageId(), role: 'user', content: userMsg },
      { id: state.currentAssistantId, role: 'assistant', content: '' },
    ])
    setStreaming(true)

    try {
      await sendMessage(sessionId, userMsg, createEventHandler(state))
    } finally {
      setStreaming(false)
      const lastId = state.currentAssistantId
      setMessages((prev) => prev.filter((message) =>
        !(message.id === lastId && message.role === 'assistant' && !message.content.trim())
      ))
    }
  }

  const lastMsg = messages[messages.length - 1]

  const handleContinue = async () => {
    if (streaming) return
    setCanContinue(false)
    setStreaming(true)
    lastEventTimeRef.current = Date.now()
    setConnectionWarning(false)

    const state: EventHandlerState = {
      currentAssistantId: createMessageId(),
      assistantContent: '',
      toolMessageIds: new Map<string, string>(),
    }
    setMessages((prev) => [
      ...prev,
      { id: state.currentAssistantId, role: 'assistant' as const, content: '' },
    ])

    try {
      await continueGeneration(sessionId, createEventHandler(state))
    } finally {
      setStreaming(false)
      const lastId = state.currentAssistantId
      setMessages((prev) => prev.filter((message) =>
        !(message.id === lastId && message.role === 'assistant' && !message.content.trim())
      ))
    }
  }

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
            stateChanges={m.stateChanges}
            compressionInfo={m.compressionInfo}
          />
        ))}
        {streaming && lastMsg?.role === 'assistant' && (
          <span className="streaming-cursor" />
        )}
        {connectionWarning && streaming && (
          <div className="connection-warning">
            连接可能已断开，可尝试停止后重新发送
          </div>
        )}
        {canContinue && !streaming && (
          <button className="continue-btn" onClick={() => void handleContinue()}>
            继续生成
          </button>
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
        {streaming ? (
          <button type="button" className="stop-btn" onClick={() => void handleStop()} title="停止生成">
            ■
          </button>
        ) : (
          <button type="button" className="send-btn" onClick={() => void handleSend()} disabled={!input.trim()}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="22" y1="2" x2="11" y2="13" />
              <polygon points="22 2 15 22 11 13 2 9 22 2" />
            </svg>
          </button>
        )}
      </div>
    </div>
  )
}
