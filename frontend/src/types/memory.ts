// frontend/src/types/memory.ts

export interface MemorySource {
  kind: string;
  session_id: string;
  message_id?: string | null;
  tool_call_id?: string | null;
  quote?: string | null;
}

// Legacy v2 memory item kept for pending/profile compatibility actions.
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

export interface MemoryProfileItem {
  id: string;
  domain: string;
  key: string;
  value: unknown;
  polarity: string;
  stability: string;
  confidence: number;
  status: string;
  context: Record<string, unknown>;
  applicability: string;
  recall_hints: Record<string, unknown>;
  source_refs: Array<Record<string, unknown>>;
  created_at: string;
  updated_at: string;
}

export interface UserMemoryProfile {
  schema_version: 3;
  user_id: string;
  constraints: MemoryProfileItem[];
  rejections: MemoryProfileItem[];
  stable_preferences: MemoryProfileItem[];
  preference_hypotheses: MemoryProfileItem[];
}

export interface WorkingMemoryItem {
  id: string;
  phase: number;
  kind: string;
  domains: string[];
  content: string;
  reason: string;
  status: string;
  expires: Record<string, boolean>;
  created_at: string;
}

export interface SessionWorkingMemory {
  schema_version: number;
  user_id: string;
  session_id: string;
  trip_id?: string | null;
  items: WorkingMemoryItem[];
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

export interface EpisodeSlice {
  id: string;
  user_id: string;
  source_episode_id: string;
  source_trip_id?: string | null;
  slice_type: string;
  domains: string[];
  entities: Record<string, unknown>;
  keywords: string[];
  content: string;
  applicability: string;
  created_at: string;
}

export interface MemoryProfileBuckets {
  constraints: MemoryProfileItem[];
  rejections: MemoryProfileItem[];
  stable_preferences: MemoryProfileItem[];
  preference_hypotheses: MemoryProfileItem[];
}

export interface MemoryActions {
  fetchMemories: () => Promise<void>;
  confirmMemory: (itemId: string) => Promise<void>;
  rejectMemory: (itemId: string) => Promise<void>;
  deleteMemory: (itemId: string) => Promise<void>;
}

export interface UseMemoryReturn {
  profile: UserMemoryProfile;
  profileBuckets: MemoryProfileBuckets;
  sessionWorkingMemory: SessionWorkingMemory;
  episodes: TripEpisode[];
  slices: EpisodeSlice[];
  legacyMemories: MemoryItem[];
  pendingMemories: MemoryItem[];
  loading: boolean;
  error: string | null;
  actions: MemoryActions;
  pendingCount: number;
}

export const EMPTY_MEMORY_PROFILE: UserMemoryProfile = {
  schema_version: 3,
  user_id: '',
  constraints: [],
  rejections: [],
  stable_preferences: [],
  preference_hypotheses: [],
};
