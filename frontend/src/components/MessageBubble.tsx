import { useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { InternalTaskEvent } from '../types/plan'

interface CompressionInfo {
  message_count_before: number
  message_count_after: number
  must_keep_count: number
  compressed_count: number
  estimated_tokens_before: number
  reason: string
}

interface StateChange {
  icon: string
  label: string
  value: string
}

const PHASE_LABELS: Record<number, string> = {
  1: '灵感与目的地',
  3: '日期与住宿',
  5: '行程组装',
  7: '出发前查漏',
}

const STEP_LABELS: Record<string, string> = {
  brief: '旅行画像',
  candidate: '候选筛选',
  skeleton: '骨架方案',
  lock: '锁定交通与住宿',
}

interface Props {
  role: 'user' | 'assistant' | 'tool' | 'system'
  content: string
  toolName?: string
  humanLabel?: string
  startedAt?: number
  endedAt?: number
  toolStatus?: 'pending' | 'success' | 'error' | 'skipped'
  toolArguments?: Record<string, unknown>
  toolResult?: unknown
  toolError?: string
  toolSuggestion?: string
  stateChanges?: StateChange[]
  phaseTransition?: {
    to_phase: number
    to_step?: string | null
  }
  compressionInfo?: CompressionInfo
  internalTask?: InternalTaskEvent
  staleness?: 'normal' | 'minor' | 'waiting'
  memoryChip?: { count: number }
}

function formatJson(value: unknown) {
  return JSON.stringify(value, null, 2)
}

export default function MessageBubble({
  role,
  content,
  toolName,
  humanLabel,
  startedAt,
  endedAt,
  toolStatus,
  toolArguments,
  toolResult,
  toolError,
  toolSuggestion,
  stateChanges,
  phaseTransition,
  compressionInfo,
  internalTask,
  staleness,
  memoryChip,
}: Props) {
  const [detailsExpanded, setDetailsExpanded] = useState(false)
  const [now, setNow] = useState(Date.now())

  useEffect(() => {
    const internalTaskPending = role === 'system' && internalTask?.status === 'pending'
    if ((role !== 'tool' || toolStatus !== 'pending') && !internalTaskPending) return undefined

    const timer = window.setInterval(() => {
      setNow(Date.now())
    }, 500)

    return () => window.clearInterval(timer)
  }, [internalTask?.status, role, toolStatus])

  if (role === 'system' && memoryChip) {
    return (
      <button
        type="button"
        className="message system-memory-chip"
        onClick={() => window.dispatchEvent(new CustomEvent('openMemoryCenter'))}
      >
        💭 本轮使用 {memoryChip.count} 条旅行记忆
      </button>
    )
  }

  if (role === 'system' && phaseTransition) {
    const phaseLabel = PHASE_LABELS[phaseTransition.to_phase] ?? `Phase ${phaseTransition.to_phase}`
    const stepLabel = phaseTransition.to_step
      ? (STEP_LABELS[phaseTransition.to_step] ?? phaseTransition.to_step)
      : null
    const message = `已进入${phaseLabel}${stepLabel ? ` · ${stepLabel}` : ''}`

    return (
      <div className="message system-phase-transition">
        <div className="phase-transition-card">
          <div className="phase-transition-header">
            <span className="phase-transition-marker" aria-hidden="true" />
            <span className="phase-transition-title">阶段推进</span>
          </div>
          <div className="phase-transition-text">{message}</div>
        </div>
      </div>
    )
  }

  if (role === 'system' && stateChanges && stateChanges.length > 0) {
    return (
      <div className="message system-state-update">
        <div className="state-update-card">
          <div className="state-update-header">
            <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M14 8a6 6 0 1 1-2-4.5" />
              <path d="M14 2v4h-4" />
            </svg>
            <span className="state-update-title">旅行计划已更新</span>
          </div>
          <div className="state-update-chips">
            {stateChanges.map((change, i) => (
              <span key={i} className="state-chip">
                <span className="state-chip-icon">{change.icon}</span>
                <span className="state-chip-label">{change.label}</span>
                <span className="state-chip-value">{change.value}</span>
              </span>
            ))}
          </div>
        </div>
      </div>
    )
  }

  if (role === 'system' && compressionInfo) {
    return (
      <div className="message system-compression">
        <div className="compression-card">
          <div className="compression-header">
            <span className="compression-icon">
              <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M4 2v4l4 2-4 2v4" />
                <path d="M12 2v4l-4 2 4 2v4" />
              </svg>
            </span>
            <span className="compression-title">上下文压缩</span>
            <span className="compression-badge">{content}</span>
          </div>
          <div className="compression-reason">{compressionInfo.reason}</div>
          <div className="compression-details">
            <span>保留关键消息 {compressionInfo.must_keep_count} 条</span>
            <span className="compression-sep" />
            <span>压缩 {compressionInfo.compressed_count} 条</span>
          </div>
        </div>
      </div>
    )
  }

  if (role === 'system' && internalTask) {
    const statusLabel =
      internalTask.status === 'pending'
        ? '进行中'
        : internalTask.status === 'success'
          ? '完成'
          : internalTask.status === 'warning'
            ? '需注意'
            : internalTask.status === 'skipped'
              ? '已跳过'
              : '失败'
    const startedAtMs = internalTask.started_at
      ? (internalTask.started_at > 1_000_000_000_000 ? internalTask.started_at : internalTask.started_at * 1000)
      : startedAt
    const endedAtMs = internalTask.ended_at
      ? (internalTask.ended_at > 1_000_000_000_000 ? internalTask.ended_at : internalTask.ended_at * 1000)
      : endedAt
    const elapsedMs = startedAtMs
      ? Math.max(0, (endedAtMs ?? (internalTask.status === 'pending' ? now : Date.now())) - startedAtMs)
      : null
    const elapsedLabel = elapsedMs !== null ? `${(elapsedMs / 1000).toFixed(1)}s` : null
    const detail = internalTask.error ?? internalTask.message ?? content
    const hasDetails = internalTask.result !== undefined || Boolean(internalTask.error)

    return (
      <div className={`message system-internal-task ${internalTask.status}`}>
        <div className="internal-task-card">
          <div className="internal-task-header">
            <div className="internal-task-title-group">
              <span className="internal-task-pulse" aria-hidden="true" />
              <div>
                <div className="internal-task-kicker">系统内部任务</div>
                <div className="internal-task-title">{internalTask.label}</div>
              </div>
            </div>
            <div className="internal-task-meta">
              {elapsedLabel && <span className="internal-task-elapsed">{elapsedLabel}</span>}
              <span className={`internal-task-status ${internalTask.status}`}>{statusLabel}</span>
            </div>
          </div>
          {detail && <div className="internal-task-message">{detail}</div>}
          <div className="internal-task-footer">
            <span>{internalTask.kind}</span>
            {internalTask.related_tool_call_id && <span>关联工具 {internalTask.related_tool_call_id}</span>}
            {internalTask.blocking && <span>阻塞当前回复</span>}
          </div>
          {hasDetails && (
            <button
              type="button"
              className="internal-task-details-toggle"
              onClick={() => setDetailsExpanded((value) => !value)}
              aria-expanded={detailsExpanded}
            >
              详情{detailsExpanded ? '收起' : '展开'}
            </button>
          )}
          {detailsExpanded && hasDetails && (
            <div className="internal-task-details">
              {internalTask.result !== undefined && (
                <pre className="tool-json">{formatJson(internalTask.result)}</pre>
              )}
              {internalTask.error && <div className="tool-error">{internalTask.error}</div>}
            </div>
          )}
        </div>
      </div>
    )
  }

  if (role === 'tool') {
    const statusLabel =
      toolStatus === 'error' ? '失败' : toolStatus === 'success' ? '成功' : '执行中'
    const resolvedStatusLabel = toolStatus === 'skipped' ? '已跳过' : statusLabel
    const elapsedMs = startedAt
      ? Math.max(0, (endedAt ?? (toolStatus === 'pending' ? now : Date.now())) - startedAt)
      : null
    const elapsedLabel = elapsedMs !== null ? `${(elapsedMs / 1000).toFixed(1)}s` : null
    const isLongRunning = toolStatus === 'pending' && elapsedMs !== null && elapsedMs >= 8000
    const subtitleBase = humanLabel ?? toolName ?? null
    const subtitleLabel = subtitleBase
      ? `${subtitleBase}${isLongRunning ? '（运行较久，请稍候）' : ''}`
      : null

    return (
      <div className={`message tool ${toolStatus ?? 'pending'}`}>
        <div className="tool-card">
          <div className="tool-card-header">
            <div className="tool-card-meta">
              <span className="tool-badge">{toolName}</span>
              {(subtitleLabel || elapsedLabel) && (
                <div className={`tool-subtitle ${isLongRunning ? 'long-running' : ''}`}>
                  {subtitleLabel && <span>{subtitleLabel}</span>}
                  {elapsedLabel && <span className="tool-elapsed">{elapsedLabel}</span>}
                  {staleness === 'minor' && <span className="breath-dot">⋯</span>}
                </div>
              )}
            </div>
            <div className="tool-card-actions">
              <span className={`tool-status ${toolStatus ?? 'pending'}`}>{resolvedStatusLabel}</span>
              {(toolArguments || toolResult !== undefined) && (
                <button
                  type="button"
                  className="tool-details-toggle"
                  onClick={() => setDetailsExpanded((value) => !value)}
                  aria-expanded={detailsExpanded}
                >
                  详情{detailsExpanded ? '收起' : '展开'}
                </button>
              )}
            </div>
          </div>
          {detailsExpanded && (toolArguments || toolResult !== undefined) && (
            <div className="tool-section">
              {toolArguments && (
                <div className="tool-section-detail">
                  <div className="tool-section-title">输入</div>
                  <pre className="tool-json">{formatJson(toolArguments)}</pre>
                </div>
              )}
              {toolResult !== undefined && (
                <div className="tool-section-detail">
                  <div className="tool-section-title">输出</div>
                  <pre className="tool-json">{formatJson(toolResult)}</pre>
                </div>
              )}
            </div>
          )}
          {toolError && <div className="tool-error">{toolError}</div>}
          {toolSuggestion && <div className="tool-suggestion">{toolSuggestion}</div>}
        </div>
      </div>
    )
  }
  return (
    <div className={`message ${role}`}>
      <div className="bubble">
        {role === 'assistant' ? (
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
        ) : (
          content
        )}
      </div>
    </div>
  )
}
