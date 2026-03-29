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

export interface Accommodation {
  area: string
  hotel: string | null
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
  budget: Budget | null
  accommodation: Accommodation | null
  daily_plans: DayPlan[]
  backtrack_history: BacktrackEvent[]
}

export interface SSEEvent {
  type: 'text_delta' | 'tool_call' | 'state_update' | 'done'
  content?: string
  tool_call?: { name: string; arguments: Record<string, unknown> }
  plan?: TravelPlanState
}
