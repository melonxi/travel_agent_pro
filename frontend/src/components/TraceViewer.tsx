import { useState } from 'react'
import { useTrace } from '../hooks/useTrace'
import type { TraceIteration, TraceToolCall, StateChange } from '../types/trace'
import '../styles/trace-viewer.css'

interface TraceViewerProps {
  sessionId: string | null
  refreshTrigger?: number
}

function formatTokens(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
  return String(n)
}

function formatDuration(ms: number): string {
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.round(ms)}ms`
}

function formatCost(usd: number): string {
  if (usd < 0.001) return '<$0.001'
  return `$${usd.toFixed(3)}`
}

function getProviderClass(provider: string): string {
  const p = provider.toLowerCase()
  if (p.includes('openai') || p === 'openai') return 'provider-openai'
  if (p.includes('anthropic') || p === 'anthropic') return 'provider-anthropic'
  return 'provider-default'
}

function SummaryBar({ summary }: { summary: { total_input_tokens: number; total_output_tokens: number; total_llm_duration_ms: number; total_tool_duration_ms: number; estimated_cost_usd: number; llm_call_count: number; tool_call_count: number } }) {
  return (
    <div className="trace-summary">
      <div className="trace-summary-card">
        <div className="card-value">{formatTokens(summary.total_input_tokens + summary.total_output_tokens)}</div>
        <div className="card-label">Tokens</div>
      </div>
      <div className="trace-summary-card">
        <div className="card-value">{formatCost(summary.estimated_cost_usd)}</div>
        <div className="card-label">Cost</div>
      </div>
      <div className="trace-summary-card">
        <div className="card-value">{formatDuration(summary.total_llm_duration_ms + summary.total_tool_duration_ms)}</div>
        <div className="card-label">Duration</div>
      </div>
      <div className="trace-summary-card">
        <div className="card-value">{summary.llm_call_count}</div>
        <div className="card-label">LLM</div>
      </div>
      <div className="trace-summary-card">
        <div className="card-value">{summary.tool_call_count}</div>
        <div className="card-label">Tools</div>
      </div>
    </div>
  )
}

function ToolCallRow({ tool, maxDuration }: { tool: TraceToolCall; maxDuration: number }) {
  const widthPct = maxDuration > 0 ? (tool.duration_ms / maxDuration) * 100 : 0
  return (
    <div className="trace-tool-row">
      <span className="tool-name">{tool.name}</span>
      <span className={`tool-side-effect ${tool.side_effect}`}>{tool.side_effect}</span>
      <div className="tool-bar-container">
        <div
          className={`tool-bar status-${tool.status}`}
          style={{ width: `${Math.max(widthPct, 2)}%` }}
        />
      </div>
      <span className="tool-duration">{formatDuration(tool.duration_ms)}</span>
    </div>
  )
}

function StateDiffPanel({ changes }: { changes: StateChange[] }) {
  if (changes.length === 0) return null
  return (
    <div className="trace-state-diff">
      <div className="trace-state-diff-title">State Changes</div>
      {changes.map((change, i) => {
        const isNew = change.before === null || change.before === undefined
        return (
          <div key={i} className={`state-change-row ${isNew ? 'added' : 'modified'}`}>
            <span className="state-field">{change.field}</span>
            {!isNew && (
              <>
                <span className="state-value before">{JSON.stringify(change.before)}</span>
                <span className="state-arrow">→</span>
              </>
            )}
            <span className="state-value after">{JSON.stringify(change.after)}</span>
          </div>
        )
      })}
    </div>
  )
}

function IterationRow({ iteration, maxLLMDuration }: { iteration: TraceIteration; maxLLMDuration: number }) {
  const [expanded, setExpanded] = useState(false)
  const llm = iteration.llm_call
  const barPct = llm && maxLLMDuration > 0 ? (llm.duration_ms / maxLLMDuration) * 100 : 0
  const maxToolDuration = Math.max(...iteration.tool_calls.map((t) => t.duration_ms), 1)

  return (
    <div className="trace-iteration">
      <div className="trace-iteration-header" onClick={() => setExpanded(!expanded)}>
        <span className="iter-index">#{iteration.index}</span>
        <span className="iter-phase">P{iteration.phase}</span>
        {llm && (
          <>
            <div className="iter-bar-container">
              <div
                className={`iter-bar ${getProviderClass(llm.provider)}`}
                style={{ width: `${Math.max(barPct, 3)}%` }}
              />
            </div>
            <span className="iter-model">{llm.model}</span>
            <span className="iter-tokens">{formatTokens(llm.input_tokens + llm.output_tokens)}</span>
            <span className="iter-cost">{formatCost(llm.cost_usd)}</span>
          </>
        )}
        <span className={`iter-expand-icon ${expanded ? 'expanded' : ''}`}>▶</span>
      </div>
      {expanded && (
        <div className="trace-iteration-detail">
          {iteration.tool_calls.length > 0 ? (
            <div className="trace-tool-list">
              {iteration.tool_calls.map((tool, i) => (
                <ToolCallRow key={i} tool={tool} maxDuration={maxToolDuration} />
              ))}
            </div>
          ) : (
            <div className="trace-no-tools">No tool calls</div>
          )}
          <StateDiffPanel changes={iteration.state_changes} />
        </div>
      )}
    </div>
  )
}

export default function TraceViewer({ sessionId, refreshTrigger }: TraceViewerProps) {
  const { trace, loading, error } = useTrace(sessionId, refreshTrigger)

  if (loading) {
    return <div className="trace-viewer"><div className="trace-loading">Loading trace…</div></div>
  }

  if (error) {
    return <div className="trace-viewer"><div className="trace-error">{error}</div></div>
  }

  if (!trace || trace.total_iterations === 0) {
    return <div className="trace-viewer"><div className="trace-empty">暂无 trace 数据</div></div>
  }

  const maxLLMDuration = Math.max(
    ...trace.iterations.map((it) => it.llm_call?.duration_ms ?? 0),
    1,
  )

  return (
    <div className="trace-viewer">
      <SummaryBar summary={trace.summary} />
      <div className="trace-iterations">
        {trace.iterations.map((iteration) => (
          <IterationRow
            key={iteration.index}
            iteration={iteration}
            maxLLMDuration={maxLLMDuration}
          />
        ))}
      </div>
    </div>
  )
}
