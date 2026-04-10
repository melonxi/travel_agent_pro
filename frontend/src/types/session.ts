export interface SessionMeta {
  session_id: string
  title: string | null
  phase: number
  status: 'active' | 'archived' | 'deleted'
  updated_at: string
}

export interface SessionMessage {
  role: 'system' | 'user' | 'assistant' | 'tool'
  content: string | null
  tool_calls?: Array<{
    id: string
    name: string
    arguments: Record<string, unknown>
  }> | null
  tool_call_id?: string | null
  seq: number
}
