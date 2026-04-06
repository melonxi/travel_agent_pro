import { useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

interface Props {
  role: 'user' | 'assistant' | 'tool'
  content: string
  toolName?: string
  toolStatus?: 'pending' | 'success' | 'error' | 'skipped'
  toolArguments?: Record<string, unknown>
  toolResult?: unknown
  toolError?: string
  toolSuggestion?: string
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
}: Props) {
  const shouldExpandDetails =
    role === 'tool' && (toolStatus === 'pending' || toolStatus === 'error')
  const [detailsExpanded, setDetailsExpanded] = useState(shouldExpandDetails)

  useEffect(() => {
    if (role === 'tool') {
      setDetailsExpanded(shouldExpandDetails)
    }
  }, [role, shouldExpandDetails])

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
