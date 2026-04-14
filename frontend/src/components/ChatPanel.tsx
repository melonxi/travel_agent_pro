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
  completed: boolean
  failed: boolean
}

type StreamFeedbackKind = 'waiting' | 'continue' | 'retry' | 'fatal' | 'stopped'
type StreamFeedbackTone = 'muted' | 'warning' | 'error'

interface StreamFeedback {
  kind: StreamFeedbackKind
  tone: StreamFeedbackTone
  message: string
  detail?: string
  action?: 'continue' | 'retry'
}

const FAILURE_PHASE_LABELS: Record<string, string> = {
  connection: '连接阶段',
  streaming: '回复阶段',
  parsing: '解析阶段',
  cancelled: '取消阶段',
}

function formatFailureDetail(message?: string, failurePhase?: string) {
  if (!message && !failurePhase) return undefined
  const phase = failurePhase ? FAILURE_PHASE_LABELS[failurePhase] ?? failurePhase : ''
  if (!phase) return message
  if (!message) return phase
  return `${phase}：${message}`
}

function createWaitingFeedback(): StreamFeedback {
  return {
    kind: 'waiting',
    tone: 'warning',
    message: '连接似乎不稳定，正在等待模型继续响应。',
    detail: '如果长时间没有恢复，可先停止，再重新发送上一条消息。',
  }
}

function createStoppedFeedback(): StreamFeedback {
  return {
    kind: 'stopped',
    tone: 'muted',
    message: '已停止生成。',
    detail: '可以重新发送上一条消息，或修改内容后再发。',
    action: 'retry',
  }
}

function createUnexpectedEndFeedback(): StreamFeedback {
  return {
    kind: 'retry',
    tone: 'error',
    message: '连接已提前结束，可重新发送上一条消息。',
    detail: '如果问题反复出现，建议稍后再试。',
    action: 'retry',
  }
}

function createErrorFeedback(event: SSEEvent): StreamFeedback {
  if (event.run_status === 'cancelled') {
    return createStoppedFeedback()
  }

  if (event.can_continue) {
    return {
      kind: 'continue',
      tone: 'warning',
      message: '回复已中断，可从当前位置继续生成。',
      detail: formatFailureDetail(event.message, event.failure_phase),
      action: 'continue',
    }
  }

  if (event.retryable) {
    return {
      kind: 'retry',
      tone: 'error',
      message: '本轮生成失败，可重新发送上一条消息。',
      detail: formatFailureDetail(event.message, event.failure_phase),
      action: 'retry',
    }
  }

  return {
    kind: 'fatal',
    tone: 'error',
    message: '本轮生成未完成，请调整后重新发送。',
    detail: formatFailureDetail(event.message, event.failure_phase),
  }
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
  const [streamFeedback, setStreamFeedback] = useState<StreamFeedback | null>(null)
  const { sendMessage, cancel, continueGeneration } = useSSE()
  const lastUserMessageRef = useRef('')
  const userStoppedRef = useRef(false)

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
        setStreamFeedback((prev) => (prev && prev.kind !== 'waiting' ? prev : createWaitingFeedback()))
      }
    }, KEEPALIVE_CHECK_INTERVAL_MS)
    return () => clearInterval(timer)
  }, [streaming])

  useEffect(() => {
    let cancelled = false
    setStreamFeedback(null)
    lastUserMessageRef.current = ''
    userStoppedRef.current = false

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
    if (!streaming) return
    userStoppedRef.current = true
    try {
      await cancel(sessionId)
    } finally {
      setStreaming(false)
      setStreamFeedback(createStoppedFeedback())
    }
  }

  const createEventHandler = (state: EventHandlerState) => (event: SSEEvent) => {
    lastEventTimeRef.current = Date.now()
    setStreamFeedback((prev) => (prev?.kind === 'waiting' ? null : prev))

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
      state.failed = true
      setStreamFeedback(createErrorFeedback(event))
    } else if (event.type === 'done') {
      state.completed = true
      setStreamFeedback(null)
    }
  }

  const finishStream = (state: EventHandlerState) => {
    setStreaming(false)
    if (!state.completed && !state.failed && !userStoppedRef.current) {
      setStreamFeedback(createUnexpectedEndFeedback())
    }

    const lastId = state.currentAssistantId
    setMessages((prev) => prev.filter((message) =>
      !(message.id === lastId && message.role === 'assistant' && !message.content.trim())
    ))
  }

  const startMessageStream = async (userMsg: string, clearInput: boolean) => {
    if (!userMsg.trim() || streaming) return

    userStoppedRef.current = false
    lastUserMessageRef.current = userMsg
    lastEventTimeRef.current = Date.now()
    setStreamFeedback(null)
    const state: EventHandlerState = {
      currentAssistantId: createMessageId(),
      assistantContent: '',
      toolMessageIds: new Map<string, string>(),
      completed: false,
      failed: false,
    }

    if (clearInput) {
      setInput('')
    }
    setMessages((prev) => [
      ...prev,
      { id: createMessageId(), role: 'user', content: userMsg },
      { id: state.currentAssistantId, role: 'assistant', content: '' },
    ])
    setStreaming(true)

    try {
      await sendMessage(sessionId, userMsg, createEventHandler(state))
    } finally {
      finishStream(state)
    }
  }

  const handleRetry = async () => {
    const lastUserMessage = lastUserMessageRef.current.trim()
    if (!lastUserMessage || streaming) return
    await startMessageStream(lastUserMessage, false)
  }

  const handleSend = async () => {
    const userMsg = input.trim()
    await startMessageStream(userMsg, true)
  }

  const lastMsg = messages[messages.length - 1]

  const handleContinue = async () => {
    if (streaming) return
    userStoppedRef.current = false
    lastEventTimeRef.current = Date.now()
    setStreamFeedback(null)

    const state: EventHandlerState = {
      currentAssistantId: createMessageId(),
      assistantContent: '',
      toolMessageIds: new Map<string, string>(),
      completed: false,
      failed: false,
    }
    setStreaming(true)
    setMessages((prev) => [
      ...prev,
      { id: state.currentAssistantId, role: 'assistant' as const, content: '' },
    ])

    try {
      await continueGeneration(sessionId, createEventHandler(state))
    } finally {
      finishStream(state)
    }
  }

  const feedbackAction = streamFeedback?.action === 'continue'
    ? {
        label: '继续生成',
        onClick: () => void handleContinue(),
        className: 'chat-status-btn',
      }
    : streamFeedback?.action === 'retry'
      ? {
          label: '重新发送',
          onClick: () => void handleRetry(),
          className: 'chat-status-btn chat-status-btn--danger',
        }
      : null

  const feedbackIcon = streamFeedback?.tone === 'error'
    ? '!'
    : streamFeedback?.tone === 'warning'
      ? '...'
      : '||'

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
        {streamFeedback && (
          <div className={`chat-status chat-status--${streamFeedback.tone}`} aria-live="polite">
            <div className="chat-status-main">
              <span className="chat-status-icon" aria-hidden="true">{feedbackIcon}</span>
              <div className="chat-status-copy">
                <div className="chat-status-message">{streamFeedback.message}</div>
                {streamFeedback.detail && (
                  <div className="chat-status-detail">{streamFeedback.detail}</div>
                )}
              </div>
            </div>
            {feedbackAction && (
              <div className="chat-status-actions">
                <button type="button" className={feedbackAction.className} onClick={feedbackAction.onClick}>
                  {feedbackAction.label}
                </button>
              </div>
            )}
          </div>
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
            placeholder="告诉我你想去哪里…（Enter 发送）"
            disabled={streaming}
          />
        </div>
        <button
          type="button"
          className={`send-btn ${streaming ? 'send-btn--hidden' : ''}`}
          onClick={() => void handleSend()}
          disabled={!input.trim()}
          aria-label="发送消息"
          title={!input.trim() ? '请输入内容' : '发送'}
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="22" y1="2" x2="11" y2="13" />
            <polygon points="22 2 15 22 11 13 2 9 22 2" />
          </svg>
        </button>
        <button
          type="button"
          className={`stop-btn ${!streaming ? 'stop-btn--hidden' : ''}`}
          onClick={() => void handleStop()}
          aria-label="停止生成"
          title="停止生成"
        >
          <svg viewBox="0 0 24 24" fill="currentColor" width="18" height="18">
            <rect x="6" y="6" width="12" height="12" rx="2" />
          </svg>
        </button>
      </div>
    </div>
  )
}
