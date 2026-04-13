export interface TraceToolCall {
  name: string
  duration_ms: number
  status: 'success' | 'error' | 'skipped'
  side_effect: 'read' | 'write'
  arguments_preview: string
  result_preview: string
  parallel_group: number | null
  validation_errors: string[] | null
  judge_scores: Record<string, number> | null
}

export interface StateChange {
  field: string
  before: unknown
  after: unknown
}

export interface MemoryHit {
  item_ids: string[]
  core: number
  trip: number
  phase: number
}

export interface TraceIteration {
  index: number
  phase: number
  llm_call: {
    provider: string
    model: string
    input_tokens: number
    output_tokens: number
    duration_ms: number
    cost_usd: number
  } | null
  tool_calls: TraceToolCall[]
  state_changes: StateChange[]
  compression_event: string | null
  memory_hits: MemoryHit | null
}

export interface TraceSummary {
  total_input_tokens: number
  total_output_tokens: number
  total_llm_duration_ms: number
  total_tool_duration_ms: number
  estimated_cost_usd: number
  llm_call_count: number
  tool_call_count: number
  by_model: Record<string, {
    calls: number
    input_tokens: number
    output_tokens: number
    cost_usd: number
  }>
  by_tool: Record<string, {
    calls: number
    total_duration_ms: number
    avg_duration_ms: number
  }>
}

export interface SessionTrace {
  session_id: string
  total_iterations: number
  summary: TraceSummary
  iterations: TraceIteration[]
}
