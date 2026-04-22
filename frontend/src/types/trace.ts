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
  item_ids?: string[]
  profile_ids: string[]
  working_memory_ids: string[]
  slice_ids: string[]
  matched_reasons?: string[]
  sources?: {
    query_profile?: number
    working_memory?: number
    episode_slice?: number
    [key: string]: number | undefined
  }
}

export interface MemoryRecallTelemetry {
  stage0_decision?: string
  stage0_reason?: string
  gate_needs_recall?: boolean | null
  gate_intent_type?: string
  final_recall_decision?: string
  fallback_used?: string
  candidate_count?: number
  reranker_selected_ids?: string[]
  reranker_final_reason?: string
  reranker_fallback?: string
}

export type Significance = 'high' | 'medium' | 'low' | 'none'

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
  memory_recall?: MemoryRecallTelemetry | null
  significance: Significance
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

/* ── Frontend-computed types (not from API) ─── */

export interface PhaseGroupStats {
  tokens: number
  cost_usd: number
  duration_ms: number
  llm_call_count: number
  tool_call_count: number
}

export interface PhaseGroup {
  phase: number
  label: string
  iterations: TraceIteration[]
  stats: PhaseGroupStats
}

export type PhaseEvent =
  | { type: 'iteration'; iteration: TraceIteration }
  | {
      type: 'thinking_summary'
      count: number
      tokens: number
      duration_ms: number
      indices: number[]
    }
