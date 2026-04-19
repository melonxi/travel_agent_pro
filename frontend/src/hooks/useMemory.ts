import { useState, useCallback, useMemo, useEffect, useRef } from 'react';
import type {
  EpisodeSlice,
  MemoryItem,
  MemoryProfileItem,
  MemoryProfileBuckets,
  SessionWorkingMemory,
  TripEpisode,
  UseMemoryReturn,
  UserMemoryProfile,
} from '../types/memory';
import { EMPTY_MEMORY_PROFILE } from '../types/memory';

const EMPTY_SESSION_WORKING_MEMORY: SessionWorkingMemory = {
  schema_version: 1,
  user_id: '',
  session_id: '',
  trip_id: null,
  items: [],
};

export function useMemory(userId: string, sessionId: string | null, refreshKey = 0): UseMemoryReturn {
  const [profile, setProfile] = useState<UserMemoryProfile>({
    ...EMPTY_MEMORY_PROFILE,
    user_id: userId,
  });
  const [sessionWorkingMemory, setSessionWorkingMemory] = useState<SessionWorkingMemory>({
    ...EMPTY_SESSION_WORKING_MEMORY,
    user_id: userId,
    session_id: sessionId ?? '',
  });
  const [episodes, setEpisodes] = useState<TripEpisode[]>([]);
  const [slices, setSlices] = useState<EpisodeSlice[]>([]);
  const [legacyMemories, setLegacyMemories] = useState<MemoryItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const memoriesRef = useRef(legacyMemories);
  memoriesRef.current = legacyMemories;

  const updateProfileItem = useCallback(
    (
      itemId: string,
      updater: (bucket: keyof MemoryProfileBuckets, item: MemoryProfileItem) => MemoryProfileItem | null,
    ) => {
      let previousItem: MemoryProfileItem | null = null;
      let previousBucket: keyof MemoryProfileBuckets | null = null;

      setProfile((current) => {
        let changed = false;
        const nextProfile: UserMemoryProfile = {
          ...current,
          constraints: [...current.constraints],
          rejections: [...current.rejections],
          stable_preferences: [...current.stable_preferences],
          preference_hypotheses: [...current.preference_hypotheses],
        };

        for (const bucket of [
          'constraints',
          'rejections',
          'stable_preferences',
          'preference_hypotheses',
        ] as const) {
          const items = nextProfile[bucket];
          const index = items.findIndex((item) => item.id === itemId);
          if (index === -1) continue;
          previousItem = items[index];
          previousBucket = bucket;
          const updated = updater(bucket, items[index]);
          if (updated === null) {
            items.splice(index, 1);
          } else {
            items[index] = updated;
          }
          changed = true;
          break;
        }

        return changed ? nextProfile : current;
      });

      return { previousItem, previousBucket };
    },
    [],
  );

  const restoreProfileItem = useCallback(
    (
      previousBucket: keyof MemoryProfileBuckets | null,
      previousItem: MemoryProfileItem | null,
    ) => {
      if (!previousBucket || !previousItem) return;
      setProfile((current) => {
        const nextProfile: UserMemoryProfile = {
          ...current,
          constraints: [...current.constraints],
          rejections: [...current.rejections],
          stable_preferences: [...current.stable_preferences],
          preference_hypotheses: [...current.preference_hypotheses],
        };
        const bucketItems = nextProfile[previousBucket];
        const existingIndex = bucketItems.findIndex((item) => item.id === previousItem.id);
        if (existingIndex === -1) {
          bucketItems.push(previousItem);
        } else {
          bucketItems[existingIndex] = previousItem;
        }
        return nextProfile;
      });
    },
    [],
  );

  const loadLegacyMemories = useCallback(async () => {
    try {
      const res = await fetch(`/api/memory/${userId}`);
      if (!res.ok) {
        return [];
      }
      const data = await res.json();
      return Array.isArray(data.items) ? (data.items as MemoryItem[]) : [];
    } catch {
      return [];
    }
  }, [userId]);

  const fetchMemories = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const workingMemoryPromise = sessionId
        ? fetch(`/api/memory/${userId}/sessions/${sessionId}/working-memory`)
        : Promise.resolve(null);

      const [profileRes, episodesRes, slicesRes, workingMemoryRes, legacyItems] = await Promise.all([
        fetch(`/api/memory/${userId}/profile`),
        fetch(`/api/memory/${userId}/episodes`),
        fetch(`/api/memory/${userId}/episode-slices`),
        workingMemoryPromise,
        loadLegacyMemories(),
      ]);

      if (!profileRes.ok) throw new Error(`画像请求失败 (${profileRes.status})`);
      if (!episodesRes.ok) throw new Error(`历史旅行请求失败 (${episodesRes.status})`);
      if (!slicesRes.ok) throw new Error(`历史切片请求失败 (${slicesRes.status})`);
      if (workingMemoryRes && !workingMemoryRes.ok) {
        throw new Error(`工作记忆请求失败 (${workingMemoryRes.status})`);
      }

      const [profileData, episodesData, slicesData, workingMemoryData] = await Promise.all([
        profileRes.json(),
        episodesRes.json(),
        slicesRes.json(),
        workingMemoryRes ? workingMemoryRes.json() : Promise.resolve(null),
      ]);

      setProfile({
        ...EMPTY_MEMORY_PROFILE,
        ...profileData,
        user_id: profileData.user_id ?? userId,
      });
      setSessionWorkingMemory(
        workingMemoryData
          ? {
              ...EMPTY_SESSION_WORKING_MEMORY,
              ...workingMemoryData,
              user_id: workingMemoryData.user_id ?? userId,
              session_id: workingMemoryData.session_id ?? sessionId ?? '',
            }
          : {
              ...EMPTY_SESSION_WORKING_MEMORY,
              user_id: userId,
              session_id: sessionId ?? '',
            },
      );
      setEpisodes(Array.isArray(episodesData.episodes) ? episodesData.episodes : []);
      setSlices(Array.isArray(slicesData.slices) ? slicesData.slices : []);
      setLegacyMemories(legacyItems);
    } catch (e) {
      setError(e instanceof Error ? e.message : '未知错误');
    } finally {
      setLoading(false);
    }
  }, [loadLegacyMemories, sessionId, userId]);

  const confirmMemory = useCallback(
    async (itemId: string) => {
      const target = memoriesRef.current.find((m) => m.id === itemId);
      const profileResult = updateProfileItem(itemId, (_bucket, item) => ({
        ...item,
        status: 'active',
      }));

      if (!target && !profileResult.previousItem) return;

      const prevStatus = target?.status;
      if (target) {
        setLegacyMemories((ms) =>
          ms.map((m) =>
            m.id === itemId ? { ...m, status: 'active' as const } : m,
          ),
        );
      }
      try {
        const res = await fetch(`/api/memory/${userId}/confirm`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ item_id: itemId }),
        });
        if (!res.ok) throw new Error();
      } catch {
        if (target && prevStatus) {
          setLegacyMemories((ms) =>
            ms.map((m) =>
              m.id === itemId ? { ...m, status: prevStatus } : m,
            ),
          );
        }
        restoreProfileItem(profileResult.previousBucket, profileResult.previousItem);
        setError('确认失败，请重试');
      }
    },
    [restoreProfileItem, updateProfileItem, userId],
  );

  const rejectMemory = useCallback(
    async (itemId: string) => {
      const target = memoriesRef.current.find((m) => m.id === itemId);
      const profileResult = updateProfileItem(itemId, (_bucket, item) => {
        if (_bucket === 'preference_hypotheses') {
          return null;
        }
        return {
          ...item,
          status: 'rejected',
        };
      });

      if (!target && !profileResult.previousItem) return;

      const prevStatus = target?.status;
      if (target) {
        setLegacyMemories((ms) =>
          ms.map((m) =>
            m.id === itemId ? { ...m, status: 'rejected' as const } : m,
          ),
        );
      }
      try {
        const res = await fetch(`/api/memory/${userId}/reject`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ item_id: itemId }),
        });
        if (!res.ok) throw new Error();
      } catch {
        if (target && prevStatus) {
          setLegacyMemories((ms) =>
            ms.map((m) =>
              m.id === itemId ? { ...m, status: prevStatus } : m,
            ),
          );
        }
        restoreProfileItem(profileResult.previousBucket, profileResult.previousItem);
        setError('拒绝失败，请重试');
      }
    },
    [restoreProfileItem, updateProfileItem, userId],
  );

  const deleteMemory = useCallback(
    async (itemId: string) => {
      const target = memoriesRef.current.find((m) => m.id === itemId);
      const profileResult = updateProfileItem(itemId, () => null);

      if (!target && !profileResult.previousItem) return;

      const prevStatus = target?.status;
      if (target) {
        setLegacyMemories((ms) =>
          ms.map((m) =>
            m.id === itemId ? { ...m, status: 'obsolete' as const } : m,
          ),
        );
      }
      try {
        const res = await fetch(`/api/memory/${userId}/${itemId}`, {
          method: 'DELETE',
        });
        if (!res.ok) throw new Error();
      } catch {
        if (target && prevStatus) {
          setLegacyMemories((ms) =>
            ms.map((m) =>
              m.id === itemId ? { ...m, status: prevStatus } : m,
            ),
          );
        }
        restoreProfileItem(profileResult.previousBucket, profileResult.previousItem);
        setError('删除失败，请重试');
      }
    },
    [restoreProfileItem, updateProfileItem, userId],
  );

  const profileBuckets = useMemo(
    () => ({
      constraints: profile.constraints ?? [],
      rejections: profile.rejections ?? [],
      stable_preferences: profile.stable_preferences ?? [],
      preference_hypotheses: profile.preference_hypotheses ?? [],
    }),
    [profile],
  );

  const pendingMemories = useMemo(
    () => legacyMemories.filter((m) => m.status === 'pending'),
    [legacyMemories],
  );

  const pendingProfileCount = useMemo(
    () =>
      [
        ...profileBuckets.constraints,
        ...profileBuckets.rejections,
        ...profileBuckets.stable_preferences,
        ...profileBuckets.preference_hypotheses,
      ].filter((item) => item.status === 'pending').length,
    [
      profileBuckets.constraints,
      profileBuckets.preference_hypotheses,
      profileBuckets.rejections,
      profileBuckets.stable_preferences,
    ],
  );

  const pendingCount = useMemo(
    () => pendingProfileCount + pendingMemories.length,
    [pendingMemories.length, pendingProfileCount],
  );

  const actions = useMemo(
    () => ({
      fetchMemories,
      confirmMemory,
      rejectMemory,
      deleteMemory,
    }),
    [fetchMemories, confirmMemory, rejectMemory, deleteMemory],
  );

  useEffect(() => {
    void fetchMemories();

    if (refreshKey === 0) {
      return undefined;
    }

    const retryTimers = [1000, 3000].map((delayMs) =>
      window.setTimeout(() => {
        void fetchMemories();
      }, delayMs),
    );

    return () => {
      retryTimers.forEach((timer) => window.clearTimeout(timer));
    };
  }, [fetchMemories, refreshKey]);

  return {
    profile,
    profileBuckets,
    sessionWorkingMemory,
    episodes,
    slices,
    legacyMemories,
    pendingMemories,
    loading,
    error,
    actions,
    pendingCount,
  };
}
