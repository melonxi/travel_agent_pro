export interface Location {
  lat: number
  lng: number
  name: string
}

export interface DateRange {
  start: string
  end: string
}

export interface Budget {
  total: number
  currency: string
}

export interface Travelers {
  adults: number
  children: number
}

export interface Accommodation {
  area: string
  hotel: string | null
}

export interface Constraint {
  type: string
  description: string
}

export interface Preference {
  key: string
  value: string
}

export interface CandidateItem {
  name?: string
  title?: string
  category?: string
  bucket?: string
  why?: string
  why_not?: string
  time_cost?: string | number
  area?: string
  theme?: string
  [key: string]: unknown
}

export interface SkeletonPlan {
  id?: string
  title?: string
  name?: string
  style?: string
  summary?: string
  fatigue?: string
  budget_level?: string
  tradeoffs?: string[] | string
  days?: Array<Record<string, unknown>>
  [key: string]: unknown
}

export interface PlanRisk {
  title?: string
  name?: string
  level?: string
  description?: string
  summary?: string
  mitigation?: string
  [key: string]: unknown
}

export interface Activity {
  name: string
  location: Location
  start_time: string
  end_time: string
  category: string
  cost: number
  transport_from_prev: string | null
  transport_duration_min: number
}

export interface DayPlan {
  day: number
  date: string
  activities: Activity[]
  notes: string
}

export interface BacktrackEvent {
  from_phase: number
  to_phase: number
  reason: string
  timestamp: string
}

export interface TravelPlanState {
  session_id: string
  phase: number
  destination: string | null
  dates: DateRange | null
  phase3_step?: 'brief' | 'candidate' | 'skeleton' | 'lock'
  trip_brief?: Record<string, unknown>
  candidate_pool?: CandidateItem[]
  shortlist?: CandidateItem[]
  skeleton_plans?: SkeletonPlan[]
  selected_skeleton_id?: string | null
  transport_options?: Array<Record<string, unknown>>
  selected_transport?: Record<string, unknown> | null
  accommodation_options?: Array<Record<string, unknown>>
  budget: Budget | null
  travelers?: Travelers | null
  accommodation: Accommodation | null
  constraints?: Constraint[]
  preferences?: Preference[]
  risks?: PlanRisk[]
  alternatives?: Array<Record<string, unknown>>
  daily_plans: DayPlan[]
  backtrack_history: BacktrackEvent[]
}

export interface ToolCallEvent {
  id: string
  name: string
  arguments: Record<string, unknown>
}

export interface ToolResultEvent {
  tool_call_id: string
  status: 'success' | 'error' | 'skipped'
  data?: unknown
  error?: string | null
  error_code?: string | null
  suggestion?: string | null
}

export interface CompressionInfo {
  message_count_before: number
  message_count_after: number
  must_keep_count: number
  compressed_count: number
  estimated_tokens_before: number
  reason: string
}

export interface SSEEvent {
  type: 'text_delta' | 'tool_call' | 'tool_result' | 'state_update' | 'context_compression' | 'done'
  content?: string
  tool_call?: ToolCallEvent
  tool_result?: ToolResultEvent
  plan?: TravelPlanState
  compression_info?: CompressionInfo
}
