import { useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

interface CompressionInfo {
  message_count_before: number
  message_count_after: number
  must_keep_count: number
  compressed_count: number
  estimated_tokens_before: number
  reason: string
}

interface Props {
  role: 'user' | 'assistant' | 'tool' | 'system'
  content: string
  toolName?: string
  toolStatus?: 'pending' | 'success' | 'error' | 'skipped'
  toolArguments?: Record<string, unknown>
  toolResult?: unknown
  toolError?: string
  toolSuggestion?: string
  compressionInfo?: CompressionInfo
}

function formatJson(value: unknown) {
  return JSON.stringify(value, null, 2)
}

export default function MessageBubble({
  role,
  content,
  toolName,
  toolStatus,
  toolArguments,
  toolResult,
  toolError,
  toolSuggestion,
  compressionInfo,
}: Props) {
  const [detailsExpanded, setDetailsExpanded] = useState(false)

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

  if (role === 'tool') {
    const statusLabel =
      toolStatus === 'error' ? '失败' : toolStatus === 'success' ? '成功' : '执行中'
    const resolvedStatusLabel = toolStatus === 'skipped' ? '已跳过' : statusLabel

    return (
      <div className={`message tool ${toolStatus ?? 'pending'}`}>
        <div className="tool-card">
          <div className="tool-card-header">
            <span className="tool-badge">{toolName}</span>
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
