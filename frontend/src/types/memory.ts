// frontend/src/types/memory.ts

export interface MemorySource {
  kind: string;
  session_id: string;
  message_id?: string | null;
  tool_call_id?: string | null;
  quote?: string | null;
}

export interface MemoryItem {
  id: string;
  user_id: string;
  type: string;
  domain: string;
  key: string;
  value: unknown;
  scope: string;
  polarity: string;
  confidence: number;
  status: 'active' | 'pending' | 'rejected' | 'obsolete';
  source: MemorySource;
  created_at: string;
  updated_at: string;
  expires_at?: string | null;
  destination?: string | null;
  session_id?: string | null;
  trip_id?: string | null;
  attributes: Record<string, unknown>;
}

export interface TripEpisode {
  id: string;
  user_id: string;
  session_id: string;
  trip_id?: string | null;
  destination?: string | null;
  dates?: string | null;
  travelers?: Record<string, unknown> | null;
  budget?: Record<string, unknown> | null;
  selected_skeleton?: Record<string, unknown> | null;
  final_plan_summary: string;
  accepted_items: Array<Record<string, unknown>>;
  rejected_items: Array<Record<string, unknown>>;
  lessons: string[];
  satisfaction?: number | null;
  created_at: string;
}

export interface UseMemoryReturn {
  memories: MemoryItem[];
  loading: boolean;
  error: string | null;
  fetchMemories: () => Promise<void>;
  confirmMemory: (itemId: string) => Promise<void>;
  rejectMemory: (itemId: string) => Promise<void>;
  deleteMemory: (itemId: string) => Promise<void>;
  pendingCount: number;
}
