import { useState, useCallback, useMemo, useEffect, useRef } from 'react';
import type { MemoryItem, UseMemoryReturn } from '../types/memory';

export function useMemory(userId: string): UseMemoryReturn {
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const memoriesRef = useRef(memories);
  memoriesRef.current = memories;

  const fetchMemories = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/memory/${userId}`);
      if (!res.ok) throw new Error(`请求失败 (${res.status})`);
      const data = await res.json();
      setMemories(data.items ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : '未知错误');
    } finally {
      setLoading(false);
    }
  }, [userId]);

  const confirmMemory = useCallback(
    async (itemId: string) => {
      const target = memoriesRef.current.find((m) => m.id === itemId);
      if (!target) return;
      const prevStatus = target.status;
      setMemories((ms) =>
        ms.map((m) =>
          m.id === itemId ? { ...m, status: 'active' as const } : m,
        ),
      );
      try {
        const res = await fetch(`/api/memory/${userId}/confirm`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ item_id: itemId }),
        });
        if (!res.ok) throw new Error();
      } catch {
        setMemories((ms) =>
          ms.map((m) =>
            m.id === itemId ? { ...m, status: prevStatus } : m,
          ),
        );
        setError('确认失败，请重试');
      }
    },
    [userId],
  );

  const rejectMemory = useCallback(
    async (itemId: string) => {
      const target = memoriesRef.current.find((m) => m.id === itemId);
      if (!target) return;
      const prevStatus = target.status;
      setMemories((ms) =>
        ms.map((m) =>
          m.id === itemId ? { ...m, status: 'rejected' as const } : m,
        ),
      );
      try {
        const res = await fetch(`/api/memory/${userId}/reject`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ item_id: itemId }),
        });
        if (!res.ok) throw new Error();
      } catch {
        setMemories((ms) =>
          ms.map((m) =>
            m.id === itemId ? { ...m, status: prevStatus } : m,
          ),
        );
        setError('拒绝失败，请重试');
      }
    },
    [userId],
  );

  const deleteMemory = useCallback(
    async (itemId: string) => {
      const target = memoriesRef.current.find((m) => m.id === itemId);
      if (!target) return;
      const prevStatus = target.status;
      setMemories((ms) =>
        ms.map((m) =>
          m.id === itemId ? { ...m, status: 'obsolete' as const } : m,
        ),
      );
      try {
        const res = await fetch(`/api/memory/${userId}/${itemId}`, {
          method: 'DELETE',
        });
        if (!res.ok) throw new Error();
      } catch {
        setMemories((ms) =>
          ms.map((m) =>
            m.id === itemId ? { ...m, status: prevStatus } : m,
          ),
        );
        setError('删除失败，请重试');
      }
    },
    [userId],
  );

  const pendingCount = useMemo(
    () => memories.filter((m) => m.status === 'pending').length,
    [memories],
  );

  useEffect(() => {
    fetchMemories();
  }, [fetchMemories]);

  return {
    memories,
    loading,
    error,
    fetchMemories,
    confirmMemory,
    rejectMemory,
    deleteMemory,
    pendingCount,
  };
}
