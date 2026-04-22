// frontend/src/types/memory.ts

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

export interface ArchivedTripEpisode {
  id: string;
  user_id: string;
  session_id: string;
  trip_id?: string | null;
  destination?: string | null;
  dates?: Record<string, unknown> | null;
  travelers?: Record<string, unknown> | null;
  budget?: Record<string, unknown> | null;
  selected_skeleton?: Record<string, unknown> | null;
  selected_transport?: Record<string, unknown> | null;
  accommodation?: Record<string, unknown> | null;
  daily_plan_summary: Array<Record<string, unknown>>;
  final_plan_summary: string;
  decision_log: Array<Record<string, unknown>>;
  lesson_log: Array<Record<string, unknown>>;
  created_at: string;
  completed_at: string;
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
  workingMemory: SessionWorkingMemory;
  episodes: ArchivedTripEpisode[];
  episodeSlices: EpisodeSlice[];
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
