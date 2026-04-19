import { useState, useMemo } from 'react'
import { useTrace } from '../hooks/useTrace'
import type {
  TraceIteration,
  TraceToolCall,
  StateChange,
  TraceSummary,
  PhaseGroup,
  PhaseGroupStats,
  PhaseEvent,
} from '../types/trace'
import '../styles/trace-viewer.css'

/* ── Constants ─── */

const PHASE_LABELS: Record<number, string> = {
  1: '目的地收敛',
  3: '行程框架',
  5: '逐日落地',
  7: '出发清单',
}

/* ── Formatters (reused from V1) ─── */

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

/* ── Grouping utilities ─── */

function computePhaseStats(iterations: TraceIteration[]): PhaseGroupStats {
  let tokens = 0
  let cost = 0
  let duration = 0
  let llmCalls = 0
  let toolCalls = 0
  for (const iter of iterations) {
    if (iter.llm_call) {
      tokens += iter.llm_call.input_tokens + iter.llm_call.output_tokens
      cost += iter.llm_call.cost_usd
      duration += iter.llm_call.duration_ms
      llmCalls++
    }
    toolCalls += iter.tool_calls.length
    for (const tc of iter.tool_calls) {
      duration += tc.duration_ms
    }
  }
  return { tokens, cost_usd: cost, duration_ms: duration, llm_call_count: llmCalls, tool_call_count: toolCalls }
}

function groupByPhase(iterations: TraceIteration[]): PhaseGroup[] {
  const groups = new Map<number, TraceIteration[]>()
  for (const iter of iterations) {
    const phase = iter.phase
    if (!groups.has(phase)) groups.set(phase, [])
    groups.get(phase)!.push(iter)
  }
  return Array.from(groups.entries())
    .sort(([a], [b]) => a - b)
    .map(([phase, iters]) => ({
      phase,
      label: PHASE_LABELS[phase] ?? `Phase ${phase}`,
      iterations: iters,
      stats: computePhaseStats(iters),
    }))
}

function buildPhaseEvents(iterations: TraceIteration[]): PhaseEvent[] {
  const events: PhaseEvent[] = []
  let thinkingBatch: TraceIteration[] = []

  const flushThinking = () => {
    if (thinkingBatch.length === 0) return
    let tokens = 0
    let duration = 0
    for (const iter of thinkingBatch) {
      if (iter.llm_call) {
        tokens += iter.llm_call.input_tokens + iter.llm_call.output_tokens
        duration += iter.llm_call.duration_ms
      }
    }
    events.push({
      type: 'thinking_summary',
      count: thinkingBatch.length,
      tokens,
      duration_ms: duration,
      indices: thinkingBatch.map((i) => i.index),
    })
    thinkingBatch = []
  }

  for (const iter of iterations) {
    if (iter.significance === 'none') {
      thinkingBatch.push(iter)
    } else {
      flushThinking()
      events.push({ type: 'iteration', iteration: iter })
    }
  }
  flushThinking()
  return events
}

/* ── Sub-components (kept from V1) ─── */

interface TraceViewerProps {
  sessionId: string | null
  refreshTrigger?: number
}

function SummaryBar({ summary }: { summary: TraceSummary }) {
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
      {tool.parallel_group != null && (
        <span className="tool-parallel-badge" title={`并行组 ${tool.parallel_group}`}>P</span>
      )}
      <div className="tool-bar-container">
        <div
          className={`tool-bar status-${tool.status}`}
          style={{ width: `${Math.max(widthPct, 2)}%` }}
        />
      </div>
      <span className="tool-duration">{formatDuration(tool.duration_ms)}</span>
      {tool.validation_errors && tool.validation_errors.length > 0 && (
        <div className="tool-validation-errors">
          {tool.validation_errors.map((err, i) => (
            <div key={i} className="validation-error-item">{err}</div>
          ))}
        </div>
      )}
      {tool.judge_scores && (
        <div className="tool-judge-scores">
          {Object.entries(tool.judge_scores).map(([key, val]) => (
            <span key={key} className="judge-score-tag">{key}: {val}</span>
          ))}
        </div>
      )}
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
                <span className="state-arrow">&rarr;</span>
              </>
            )}
            <span className="state-value after">{JSON.stringify(change.after)}</span>
          </div>
        )
      })}
    </div>
  )
}

/* ── New V2 components ─── */

function CollapsedThinkingRow({ count, tokens, duration_ms }: { count: number; tokens: number; duration_ms: number }) {
  return (
    <div className="trace-thinking-summary">
      <span className="thinking-icon">&#8943;</span>
      <span className="thinking-text">
        {count} thinking step{count > 1 ? 's' : ''}
      </span>
      <span className="thinking-stats">
        {formatTokens(tokens)} tokens &middot; {formatDuration(duration_ms)}
      </span>
    </div>
  )
}

function EventRow({ iteration }: { iteration: TraceIteration }) {
  const [expanded, setExpanded] = useState(false)
  const maxToolDuration = Math.max(...iteration.tool_calls.map((t) => t.duration_ms), 1)
  const memorySources = iteration.memory_hits?.sources ?? {}
  const memoryHitCount =
    iteration.memory_hits?.item_ids?.length ??
    [
      ...(iteration.memory_hits?.profile_ids ?? []),
      ...(iteration.memory_hits?.working_memory_ids ?? []),
      ...(iteration.memory_hits?.slice_ids ?? []),
    ].length

  return (
    <div className={`trace-event-row significance-${iteration.significance}`}>
      <div className="trace-event-header" onClick={() => setExpanded(!expanded)}>
        <span className="event-index">#{iteration.index}</span>
        {iteration.tool_calls.length > 0 && (
          <span className="event-tools">
            {iteration.tool_calls.map((tc) => tc.name).join(', ')}
          </span>
        )}
        {iteration.state_changes.length > 0 && (
          <span className="event-state-badge">{iteration.state_changes.length} changes</span>
        )}
        {iteration.compression_event && (
          <span className="iter-compression" title={iteration.compression_event}>C</span>
        )}
        {iteration.llm_call && (
          <span className="event-cost">{formatCost(iteration.llm_call.cost_usd)}</span>
        )}
        <span className={`iter-expand-icon ${expanded ? 'expanded' : ''}`}>&#9654;</span>
      </div>
      {expanded && (
        <div className="trace-event-detail">
          {iteration.llm_call && (
            <div className="event-llm-info">
              <span className={`event-provider-dot ${getProviderClass(iteration.llm_call.provider)}`} />
              <span className="event-model">{iteration.llm_call.model}</span>
              <span className="event-tokens">{formatTokens(iteration.llm_call.input_tokens + iteration.llm_call.output_tokens)}</span>
              <span className="event-duration">{formatDuration(iteration.llm_call.duration_ms)}</span>
            </div>
          )}
          {iteration.compression_event && (
            <div className="trace-compression-info">{iteration.compression_event}</div>
          )}
          {iteration.tool_calls.length > 0 && (
            <div className="trace-tool-list">
              {iteration.tool_calls.map((tool, i) => (
                <ToolCallRow key={i} tool={tool} maxDuration={maxToolDuration} />
              ))}
            </div>
          )}
          <StateDiffPanel changes={iteration.state_changes} />
          {iteration.memory_hits && (
            <div className="trace-memory-hits">
              命中 {memoryHitCount} 条记忆
              （profile {memorySources.profile_fixed ?? 0} / working {memorySources.working_memory ?? 0} / query {memorySources.query_profile ?? 0} / slice {memorySources.episode_slice ?? 0}）
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function PhaseGroupCard({ group, defaultExpanded }: { group: PhaseGroup; defaultExpanded: boolean }) {
  const [expanded, setExpanded] = useState(defaultExpanded)
  const events = useMemo(() => buildPhaseEvents(group.iterations), [group.iterations])

  return (
    <div className={`trace-phase-group phase-${group.phase}`}>
      <div className="trace-phase-header" onClick={() => setExpanded(!expanded)}>
        <span className={`phase-expand-icon ${expanded ? 'expanded' : ''}`}>&#9654;</span>
        <span className="phase-badge">P{group.phase}</span>
        <span className="phase-label">{group.label}</span>
        <div className="phase-stats">
          <span>{group.stats.llm_call_count} calls</span>
          <span>{group.stats.tool_call_count} tools</span>
          <span>{formatTokens(group.stats.tokens)}</span>
          <span>{formatCost(group.stats.cost_usd)}</span>
          <span>{formatDuration(group.stats.duration_ms)}</span>
        </div>
      </div>
      {expanded && (
        <div className="trace-event-timeline">
          {events.map((event, i) =>
            event.type === 'thinking_summary' ? (
              <CollapsedThinkingRow
                key={`thinking-${i}`}
                count={event.count}
                tokens={event.tokens}
                duration_ms={event.duration_ms}
              />
            ) : (
              <EventRow key={event.iteration.index} iteration={event.iteration} />
            ),
          )}
        </div>
      )}
    </div>
  )
}

/* ── Main component ─── */

export default function TraceViewer({ sessionId, refreshTrigger }: TraceViewerProps) {
  const { trace, loading, error } = useTrace(sessionId, refreshTrigger)
  const phaseGroups = useMemo(
    () => (trace ? groupByPhase(trace.iterations) : []),
    [trace?.iterations],
  )

  if (loading) {
    return <div className="trace-viewer"><div className="trace-loading">Loading trace&hellip;</div></div>
  }

  if (error) {
    return <div className="trace-viewer"><div className="trace-error">{error}</div></div>
  }

  if (!trace || trace.total_iterations === 0) {
    return <div className="trace-viewer"><div className="trace-empty">暂无 trace 数据</div></div>
  }

  return (
    <div className="trace-viewer">
      <SummaryBar summary={trace.summary} />
      <div className="trace-phase-groups">
        {phaseGroups.map((group, i) => (
          <PhaseGroupCard
            key={group.phase}
            group={group}
            defaultExpanded={i === phaseGroups.length - 1}
          />
        ))}
      </div>
    </div>
  )
}
