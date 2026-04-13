# Memory Center Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Memory Center drawer UI that surfaces the backend memory system as a user-facing, demo-ready feature — entry button with pending badge in SessionSidebar, right-sliding drawer with tabs (Active/Pending/Archived), memory cards with confirm/reject/delete actions, optimistic updates.

**Architecture:** A single `useMemory` hook is called in `SessionSidebar` and its return value is passed to `MemoryCenter` as props. The hook manages all API calls, state, and optimistic update rollback via a ref pattern. `MemoryCenter` is a self-contained drawer component with tab filtering, card rendering, skeleton loading, error/empty states. All styles live in a standalone CSS file using existing Solstice design tokens.

**Tech Stack:** React 19, TypeScript, CSS (Solstice design system) — zero new dependencies.

**Spec:** `docs/superpowers/specs/2026-04-13-memory-center-frontend-design.md`

**Worktree:** `.worktrees/memory-center-frontend` (branch `feature/memory-center-frontend`)

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `frontend/src/types/memory.ts` | Create | TypeScript interfaces matching backend memory API response |
| `frontend/src/hooks/useMemory.ts` | Create | Memory API calls, state, optimistic updates with rollback |
| `frontend/src/styles/memory-center.css` | Create | All Memory Center + sidebar button styles (Solstice tokens) |
| `frontend/src/components/MemoryCenter.tsx` | Create | Drawer + Tabs + MemoryCard + Skeleton/Error/Empty states |
| `frontend/src/components/SessionSidebar.tsx` | Modify | Add memory button with pending badge + render MemoryCenter |

---

## Backend API Contract Reference

These are the actual backend endpoints (from `backend/main.py`):

| Endpoint | Method | Request Body | Response |
|----------|--------|-------------|----------|
| `/api/memory/{user_id}` | GET | — | `{ items: MemoryItem[] }` |
| `/api/memory/{user_id}/confirm` | POST | `{ item_id: string }` | `{ item_id, status: "active" }` |
| `/api/memory/{user_id}/reject` | POST | `{ item_id: string }` | `{ item_id, status: "rejected" }` |
| `/api/memory/{user_id}/{item_id}` | DELETE | — | `{ item_id, status: "obsolete" }` |
| `/api/memory/{user_id}/episodes` | GET | — | `{ episodes: TripEpisode[] }` |

**MemoryItem fields** (from `backend/memory/models.py`): `id`, `user_id`, `type`, `domain`, `key`, `value` (any), `scope`, `polarity`, `confidence` (float 0-1), `status` ("active"|"pending"|"rejected"|"obsolete"), `source` (`{ kind, session_id, message_id?, tool_call_id?, quote? }`), `created_at`, `updated_at`, `expires_at?`, `destination?`, `session_id?`, `trip_id?`, `attributes` (dict).

**userId:** Always `"default_user"` (matches backend default).

---

## CSS Design Tokens Reference

Existing variables from `frontend/src/styles/index.css`:
- Backgrounds: `--bg-deep`, `--bg-surface`, `--bg-elevated`, `--bg-card`, `--bg-glass`
- Text: `--text-primary`, `--text-secondary`, `--text-muted`
- Accents: `--accent-amber`, `--accent-coral`, `--accent-teal`, `--accent-lavender`
- Borders: `--border-subtle`, `--border-accent`
- Radius: `--radius-sm` (8px), `--radius-md` (14px)
- Transition: `--transition-smooth` (0.35s ease-out-expo), `--ease-out-expo`
- Fonts: `--font-display` (Bodoni Moda), `--font-body` (Figtree)

---

### Task 1: Create Memory Types

**Files:**
- Create: `frontend/src/types/memory.ts`

Types must match the actual backend API response shape from `backend/memory/models.py`, not the simplified spec types.

- [ ] **Step 1: Create the types file**

```typescript
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
```

- [ ] **Step 2: Verify types compile**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add frontend/src/types/memory.ts
git commit -m "feat(memory-center): add memory type definitions

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 2: Create useMemory Hook

**Files:**
- Create: `frontend/src/hooks/useMemory.ts`
- Reference: `frontend/src/types/memory.ts` (from Task 1)

Key design decisions:
- Uses `useRef` for rollback snapshots so callbacks remain stable (only depend on `userId`)
- Auto-fetches on mount via `useEffect` (provides pending count for sidebar badge)
- Optimistic update: mutate state immediately, roll back + set error on API failure

- [ ] **Step 1: Create the hook file**

```typescript
// frontend/src/hooks/useMemory.ts

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
      const prev = memoriesRef.current.slice();
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
        setMemories(prev);
        setError('确认失败，请重试');
      }
    },
    [userId],
  );

  const rejectMemory = useCallback(
    async (itemId: string) => {
      const prev = memoriesRef.current.slice();
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
        setMemories(prev);
        setError('拒绝失败，请重试');
      }
    },
    [userId],
  );

  const deleteMemory = useCallback(
    async (itemId: string) => {
      const prev = memoriesRef.current.slice();
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
        setMemories(prev);
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
```

- [ ] **Step 2: Verify types compile**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add frontend/src/hooks/useMemory.ts
git commit -m "feat(memory-center): add useMemory hook with optimistic updates

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 3: Create Memory Center Styles

**Files:**
- Create: `frontend/src/styles/memory-center.css`

All styles for the drawer, tabs, cards, badges, states, skeleton, and sidebar button. Uses only existing Solstice design tokens — no hardcoded colors.

- [ ] **Step 1: Create the CSS file**

```css
/* frontend/src/styles/memory-center.css */

/* ===== Overlay & Drawer ===== */

.memory-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  z-index: 1000;
  opacity: 0;
  visibility: hidden;
  transition: opacity var(--transition-smooth),
    visibility var(--transition-smooth);
}

.memory-overlay.is-open {
  opacity: 1;
  visibility: visible;
}

.memory-drawer {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  width: 480px;
  background: var(--bg-surface);
  backdrop-filter: blur(24px);
  border-left: 1px solid var(--border-subtle);
  box-shadow: -8px 0 32px rgba(0, 0, 0, 0.3);
  z-index: 1001;
  display: flex;
  flex-direction: column;
  transform: translateX(100%);
  transition: transform 0.35s var(--ease-out-expo);
}

.memory-overlay.is-open .memory-drawer {
  transform: translateX(0);
}

@media (max-width: 768px) {
  .memory-drawer {
    width: 100%;
  }
}

/* ===== Header ===== */

.memory-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 20px 24px 16px;
  border-bottom: 1px solid var(--border-subtle);
}

.memory-header h2 {
  font-family: var(--font-display);
  font-size: 1.25rem;
  color: var(--text-primary);
  margin: 0;
  font-weight: 500;
}

.memory-close-btn {
  background: none;
  border: none;
  color: var(--text-secondary);
  font-size: 1.5rem;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: var(--radius-sm);
  transition: all var(--transition-smooth);
  line-height: 1;
}

.memory-close-btn:hover {
  color: var(--text-primary);
  background: rgba(255, 255, 255, 0.05);
}

/* ===== Tabs ===== */

.memory-tabs {
  display: flex;
  gap: 4px;
  padding: 12px 24px;
  border-bottom: 1px solid var(--border-subtle);
}

.memory-tab {
  flex: 1;
  padding: 8px 12px;
  background: none;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
  font-family: var(--font-body);
  font-size: 0.85rem;
  cursor: pointer;
  transition: all var(--transition-smooth);
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
}

.memory-tab:hover {
  color: var(--text-primary);
  background: rgba(255, 255, 255, 0.03);
}

.memory-tab.is-active {
  color: var(--accent-amber);
  border-color: var(--border-accent);
  background: rgba(212, 162, 76, 0.08);
}

.memory-tab-count {
  font-size: 0.75rem;
  background: rgba(255, 255, 255, 0.08);
  padding: 1px 6px;
  border-radius: 10px;
  min-width: 20px;
  text-align: center;
}

.memory-tab.is-active .memory-tab-count {
  background: rgba(212, 162, 76, 0.15);
}

/* ===== Memory List ===== */

.memory-list {
  flex: 1;
  overflow-y: auto;
  padding: 16px 24px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* ===== Memory Card ===== */

.memory-card {
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  padding: 16px;
  transition: all var(--transition-smooth);
}

.memory-card:hover {
  border-color: rgba(255, 255, 255, 0.1);
}

.memory-card.is-pending {
  border-left: 3px solid var(--accent-amber);
}

.memory-card.is-archived {
  opacity: 0.6;
}

/* Card body */

.memory-card-body {
  margin-bottom: 12px;
}

.memory-domain-tag {
  display: inline-block;
  font-size: 0.7rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  padding: 2px 8px;
  border-radius: 4px;
  margin-bottom: 8px;
  background: rgba(139, 126, 200, 0.15);
  color: var(--accent-lavender);
}

.memory-content {
  color: var(--text-primary);
  font-size: 0.9rem;
  line-height: 1.5;
  margin-bottom: 6px;
}

.memory-source {
  color: var(--text-muted);
  font-size: 0.78rem;
  font-style: italic;
  line-height: 1.4;
}

.memory-source::before {
  content: '来源：';
}

/* Card metadata */

.memory-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
}

.memory-badge {
  font-size: 0.7rem;
  padding: 2px 8px;
  border-radius: 10px;
  font-weight: 500;
}

.memory-badge.scope-global {
  background: rgba(139, 126, 200, 0.15);
  color: var(--accent-lavender);
}

.memory-badge.scope-trip {
  background: rgba(59, 169, 156, 0.15);
  color: var(--accent-teal);
}

.memory-badge.domain {
  background: rgba(255, 255, 255, 0.06);
  color: var(--text-secondary);
}

.memory-confidence {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 0.7rem;
  color: var(--text-muted);
}

.memory-confidence-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
}

.memory-confidence-dot.high {
  background: var(--accent-teal);
}

.memory-confidence-dot.medium {
  background: var(--accent-amber);
}

.memory-confidence-dot.low {
  background: var(--accent-coral);
}

.memory-trip-id,
.memory-time {
  font-size: 0.7rem;
  color: var(--text-muted);
}

/* Card actions */

.memory-actions {
  display: flex;
  gap: 8px;
  padding-top: 12px;
  border-top: 1px solid var(--border-subtle);
}

.memory-action-btn {
  flex: 1;
  padding: 6px 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  font-family: var(--font-body);
  font-size: 0.8rem;
  cursor: pointer;
  transition: all var(--transition-smooth);
  background: none;
  color: var(--text-secondary);
}

.memory-action-btn.confirm {
  border-color: rgba(59, 169, 156, 0.3);
  color: var(--accent-teal);
}

.memory-action-btn.confirm:hover {
  background: rgba(59, 169, 156, 0.12);
  border-color: rgba(59, 169, 156, 0.5);
}

.memory-action-btn.reject {
  border-color: rgba(217, 106, 75, 0.3);
  color: var(--accent-coral);
}

.memory-action-btn.reject:hover {
  background: rgba(217, 106, 75, 0.12);
  border-color: rgba(217, 106, 75, 0.5);
}

.memory-action-btn.delete {
  border-color: rgba(217, 106, 75, 0.2);
  color: var(--text-muted);
}

.memory-action-btn.delete:hover {
  color: var(--accent-coral);
  border-color: rgba(217, 106, 75, 0.4);
  background: rgba(217, 106, 75, 0.08);
}

/* ===== Empty State ===== */

.memory-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 48px 24px;
  text-align: center;
  flex: 1;
}

.memory-empty-icon {
  font-size: 2.5rem;
  margin-bottom: 16px;
  opacity: 0.4;
}

.memory-empty-text {
  color: var(--text-muted);
  font-size: 0.85rem;
  line-height: 1.6;
  max-width: 280px;
}

/* ===== Error State ===== */

.memory-error {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  margin: 12px 24px 0;
  background: rgba(217, 106, 75, 0.1);
  border: 1px solid rgba(217, 106, 75, 0.25);
  border-radius: var(--radius-sm);
  color: var(--accent-coral);
  font-size: 0.85rem;
}

.memory-error-text {
  flex: 1;
}

.memory-retry-btn {
  background: none;
  border: 1px solid rgba(217, 106, 75, 0.3);
  color: var(--accent-coral);
  padding: 4px 12px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-size: 0.8rem;
  font-family: var(--font-body);
  transition: all var(--transition-smooth);
  white-space: nowrap;
}

.memory-retry-btn:hover {
  background: rgba(217, 106, 75, 0.15);
}

/* ===== Skeleton Loading ===== */

.memory-skeleton {
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  padding: 16px;
  animation: memory-pulse 1.5s ease-in-out infinite;
}

.memory-skeleton-line {
  height: 12px;
  background: rgba(255, 255, 255, 0.06);
  border-radius: 4px;
  margin-bottom: 10px;
}

.memory-skeleton-line.short {
  width: 40%;
}

.memory-skeleton-line.medium {
  width: 70%;
}

.memory-skeleton-line.long {
  width: 100%;
}

@keyframes memory-pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.5;
  }
}

/* ===== Delete Confirmation ===== */

.memory-delete-confirm {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-top: 12px;
  border-top: 1px solid var(--border-subtle);
}

.memory-delete-confirm span {
  font-size: 0.8rem;
  color: var(--text-secondary);
  flex: 1;
}

.memory-delete-confirm button {
  padding: 4px 10px;
  border-radius: var(--radius-sm);
  font-size: 0.78rem;
  font-family: var(--font-body);
  cursor: pointer;
  transition: all var(--transition-smooth);
  border: 1px solid var(--border-subtle);
  background: none;
  color: var(--text-secondary);
}

.memory-delete-confirm .confirm-yes {
  border-color: rgba(217, 106, 75, 0.3);
  color: var(--accent-coral);
}

.memory-delete-confirm .confirm-yes:hover {
  background: rgba(217, 106, 75, 0.12);
}

/* ===== Sidebar Memory Button ===== */

.sidebar-memory-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  width: calc(100% - 24px);
  margin: 8px 12px 12px;
  padding: 10px 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  background: linear-gradient(
    135deg,
    rgba(139, 126, 200, 0.08),
    rgba(139, 126, 200, 0.04)
  );
  color: var(--text-secondary);
  font-family: var(--font-body);
  font-size: 0.85rem;
  cursor: pointer;
  transition: all var(--transition-smooth);
}

.sidebar-memory-btn:hover {
  border-color: rgba(139, 126, 200, 0.3);
  color: var(--text-primary);
  background: linear-gradient(
    135deg,
    rgba(139, 126, 200, 0.12),
    rgba(139, 126, 200, 0.08)
  );
}

.sidebar-memory-badge {
  background: var(--accent-coral);
  color: white;
  font-size: 0.65rem;
  font-weight: 700;
  min-width: 18px;
  height: 18px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 10px;
  padding: 0 5px;
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/styles/memory-center.css
git commit -m "feat(memory-center): add Solstice-themed styles

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 4: Create MemoryCenter Component

**Files:**
- Create: `frontend/src/components/MemoryCenter.tsx`
- Reference: `frontend/src/types/memory.ts` (Task 1), `frontend/src/hooks/useMemory.ts` (Task 2), `frontend/src/styles/memory-center.css` (Task 3)

Component structure:
- `MemoryCenter` — main drawer with overlay, header, tabs, list
- `MemoryCard` — single memory item with domain tag, content, source, metadata, actions
- `SkeletonCards` — 3 placeholder cards for loading state
- `formatRelativeTime` — helper for "2 小时前" style timestamps
- `confidenceLevel` — maps 0-1 float to high/medium/low
- `DOMAIN_LABELS` — maps backend domain strings to Chinese labels

- [ ] **Step 1: Create the component file**

```tsx
// frontend/src/components/MemoryCenter.tsx

import { useState, useEffect, useMemo, useCallback } from 'react';
import type { MemoryItem, UseMemoryReturn } from '../types/memory';
import '../styles/memory-center.css';

type TabKey = 'active' | 'pending' | 'archived';

interface MemoryCenterProps {
  open: boolean;
  onClose: () => void;
  memory: UseMemoryReturn;
}

const DOMAIN_LABELS: Record<string, string> = {
  destination: '目的地偏好',
  hotel: '住宿风格',
  food: '饮食限制',
  budget: '预算偏好',
  transport: '交通方式',
  activity: '活动偏好',
  general: '通用偏好',
};

function formatRelativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const min = Math.floor(diff / 60000);
  if (min < 1) return '刚刚';
  if (min < 60) return `${min} 分钟前`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} 小时前`;
  const day = Math.floor(hr / 24);
  if (day < 30) return `${day} 天前`;
  return `${Math.floor(day / 30)} 个月前`;
}

function confidenceLevel(c: number): 'high' | 'medium' | 'low' {
  if (c >= 0.7) return 'high';
  if (c >= 0.4) return 'medium';
  return 'low';
}

function MemoryCard({
  item,
  onConfirm,
  onReject,
  onDelete,
}: {
  item: MemoryItem;
  onConfirm?: (id: string) => void;
  onReject?: (id: string) => void;
  onDelete?: (id: string) => void;
}) {
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  const cardClass = [
    'memory-card',
    item.status === 'pending' && 'is-pending',
    (item.status === 'rejected' || item.status === 'obsolete') && 'is-archived',
  ]
    .filter(Boolean)
    .join(' ');

  const level = confidenceLevel(item.confidence);
  const domainLabel = DOMAIN_LABELS[item.domain] ?? item.domain;

  return (
    <div className={cardClass}>
      <div className="memory-card-body">
        <span className="memory-domain-tag">{domainLabel}</span>
        <div className="memory-content">{String(item.value)}</div>
        {item.source?.quote && (
          <div className="memory-source">{item.source.quote}</div>
        )}
      </div>

      <div className="memory-meta">
        <span
          className={`memory-badge scope-${item.scope === 'trip' ? 'trip' : 'global'}`}
        >
          {item.scope === 'trip' ? '旅程' : '全局'}
        </span>
        <span className="memory-badge domain">{item.domain}</span>
        <span className="memory-confidence">
          <span className={`memory-confidence-dot ${level}`} />
          {level === 'high' ? '高' : level === 'medium' ? '中' : '低'}
        </span>
        {item.scope === 'trip' && item.trip_id && (
          <span className="memory-trip-id">
            Trip: {item.trip_id.slice(0, 8)}
          </span>
        )}
        <span className="memory-time">
          {formatRelativeTime(item.created_at)}
        </span>
      </div>

      {item.status === 'pending' && (
        <div className="memory-actions">
          <button
            className="memory-action-btn confirm"
            onClick={() => onConfirm?.(item.id)}
          >
            ✓ 确认
          </button>
          <button
            className="memory-action-btn reject"
            onClick={() => onReject?.(item.id)}
          >
            ✗ 拒绝
          </button>
        </div>
      )}

      {item.status === 'active' && !confirmingDelete && (
        <div className="memory-actions">
          <button
            className="memory-action-btn delete"
            onClick={() => setConfirmingDelete(true)}
          >
            删除
          </button>
        </div>
      )}

      {item.status === 'active' && confirmingDelete && (
        <div className="memory-delete-confirm">
          <span>确定删除此记忆？</span>
          <button
            className="confirm-yes"
            onClick={() => {
              onDelete?.(item.id);
              setConfirmingDelete(false);
            }}
          >
            确定
          </button>
          <button onClick={() => setConfirmingDelete(false)}>取消</button>
        </div>
      )}
    </div>
  );
}

function SkeletonCards() {
  return (
    <>
      {[0, 1, 2].map((i) => (
        <div key={i} className="memory-skeleton">
          <div className="memory-skeleton-line short" />
          <div className="memory-skeleton-line long" />
          <div className="memory-skeleton-line medium" />
        </div>
      ))}
    </>
  );
}

export default function MemoryCenter({
  open,
  onClose,
  memory,
}: MemoryCenterProps) {
  const [activeTab, setActiveTab] = useState<TabKey>('active');

  const {
    memories,
    loading,
    error,
    fetchMemories,
    confirmMemory,
    rejectMemory,
    deleteMemory,
  } = memory;

  // Fetch fresh data when drawer opens
  useEffect(() => {
    if (open) {
      fetchMemories();
    }
  }, [open, fetchMemories]);

  // ESC to close
  useEffect(() => {
    if (!open) return;
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handleKey);
    return () => document.removeEventListener('keydown', handleKey);
  }, [open, onClose]);

  // Lock body scroll when open
  useEffect(() => {
    if (open) {
      document.body.style.overflow = 'hidden';
    } else {
      document.body.style.overflow = '';
    }
    return () => {
      document.body.style.overflow = '';
    };
  }, [open]);

  const grouped = useMemo(() => {
    const active = memories.filter((m) => m.status === 'active');
    const pending = memories.filter((m) => m.status === 'pending');
    const archived = memories.filter(
      (m) => m.status === 'rejected' || m.status === 'obsolete',
    );
    return { active, pending, archived };
  }, [memories]);

  const currentItems = grouped[activeTab];

  const handleOverlayClick = useCallback(
    (e: React.MouseEvent) => {
      if (e.target === e.currentTarget) onClose();
    },
    [onClose],
  );

  const tabs: Array<{ key: TabKey; label: string; count: number }> = [
    { key: 'active', label: '活跃', count: grouped.active.length },
    { key: 'pending', label: '待确认', count: grouped.pending.length },
    { key: 'archived', label: '已归档', count: grouped.archived.length },
  ];

  return (
    <div
      className={`memory-overlay${open ? ' is-open' : ''}`}
      onClick={handleOverlayClick}
    >
      <div className="memory-drawer">
        <div className="memory-header">
          <h2>记忆管理</h2>
          <button className="memory-close-btn" onClick={onClose} aria-label="关闭">
            ×
          </button>
        </div>

        <div className="memory-tabs">
          {tabs.map(({ key, label, count }) => (
            <button
              key={key}
              className={`memory-tab${activeTab === key ? ' is-active' : ''}`}
              onClick={() => setActiveTab(key)}
            >
              {label}
              <span className="memory-tab-count">{count}</span>
            </button>
          ))}
        </div>

        {error && (
          <div className="memory-error">
            <span className="memory-error-text">{error}</span>
            <button className="memory-retry-btn" onClick={fetchMemories}>
              重试
            </button>
          </div>
        )}

        <div className="memory-list">
          {loading && <SkeletonCards />}

          {!loading && !error && currentItems.length === 0 && (
            <div className="memory-empty">
              <div className="memory-empty-icon">🧠</div>
              <div className="memory-empty-text">
                暂无
                {activeTab === 'active'
                  ? '活跃'
                  : activeTab === 'pending'
                    ? '待确认'
                    : '已归档'}
                记忆数据。与 Agent 对话后，系统会自动提取和保存用户偏好。
              </div>
            </div>
          )}

          {!loading &&
            currentItems.map((item) => (
              <MemoryCard
                key={item.id}
                item={item}
                onConfirm={confirmMemory}
                onReject={rejectMemory}
                onDelete={deleteMemory}
              />
            ))}
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Verify types compile**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/MemoryCenter.tsx
git commit -m "feat(memory-center): add MemoryCenter drawer component

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 5: Modify SessionSidebar

**Files:**
- Modify: `frontend/src/components/SessionSidebar.tsx`

Changes:
1. Add imports for `useState`, `MemoryCenter`, `useMemory`
2. Add `memoryOpen` state and call `useMemory('default_user')`
3. Add memory button at bottom of expanded sidebar (before `confirmDelete` toast)
4. Render `MemoryCenter` drawer

**Current file structure** (for orientation):
- Line 1: `import { useEffect, useState } from 'react'`
- Line 2-3: SessionItem, SessionMeta imports
- Line 5-11: Props interface
- Line 13-19: Component function start, destructured props
- Line 20-21: `collapsed` and `confirmDelete` state
- Line 41-55: Collapsed sidebar return
- Line 57-108: Expanded sidebar return
- Line 104: Closing `</div>` of sidebar-list
- Line 106: confirmDelete toast
- Line 107: Closing `</aside>`

- [ ] **Step 1: Add imports**

At the top of `frontend/src/components/SessionSidebar.tsx`, change the imports:

Replace line 1-3:
```typescript
import { useEffect, useState } from 'react'
import SessionItem from './SessionItem'
import type { SessionMeta } from '../types/session'
```

With:
```typescript
import { useEffect, useState } from 'react'
import SessionItem from './SessionItem'
import MemoryCenter from './MemoryCenter'
import { useMemory } from '../hooks/useMemory'
import type { SessionMeta } from '../types/session'
```

- [ ] **Step 2: Add state and hook**

After line 21 (`const [confirmDelete, setConfirmDelete] = useState<string | null>(null)`), add:

```typescript
  const [memoryOpen, setMemoryOpen] = useState(false)
  const memory = useMemory('default_user')
```

- [ ] **Step 3: Add memory button and MemoryCenter to expanded sidebar**

In the expanded sidebar return block, insert the memory button and MemoryCenter before the closing `</aside>`. Replace the section at lines 104-108:

```typescript
      </div>

      {confirmDelete && <div className="sidebar-confirm-toast">再次点击确认删除</div>}
    </aside>
  )
}
```

With:

```typescript
      </div>

      <button
        type="button"
        className="sidebar-memory-btn"
        onClick={() => setMemoryOpen(true)}
      >
        🧠 记忆管理
        {memory.pendingCount > 0 && (
          <span className="sidebar-memory-badge">{memory.pendingCount}</span>
        )}
      </button>

      {confirmDelete && <div className="sidebar-confirm-toast">再次点击确认删除</div>}
      <MemoryCenter open={memoryOpen} onClose={() => setMemoryOpen(false)} memory={memory} />
    </aside>
  )
}
```

- [ ] **Step 4: Verify types compile**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 5: Verify build**

Run: `cd frontend && npm run build`
Expected: Build succeeds with no errors

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/SessionSidebar.tsx
git commit -m "feat(memory-center): add memory button and drawer to sidebar

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 6: Final Verification

- [ ] **Step 1: Full type check**

Run: `cd frontend && npx tsc --noEmit`
Expected: Zero errors

- [ ] **Step 2: Production build**

Run: `cd frontend && npm run build`
Expected: Build succeeds. Output shows CSS and JS bundles.

- [ ] **Step 3: Verify no unintended file changes**

Run: `git --no-pager diff --stat HEAD~5` (or however many commits were made)
Expected: Only these files changed:
- `frontend/src/types/memory.ts` (new)
- `frontend/src/hooks/useMemory.ts` (new)
- `frontend/src/styles/memory-center.css` (new)
- `frontend/src/components/MemoryCenter.tsx` (new)
- `frontend/src/components/SessionSidebar.tsx` (modified)

No changes to: `App.tsx`, `index.css`, `ChatPanel.tsx`, right panel components, or any backend files.

- [ ] **Step 4: Update PROJECT_OVERVIEW.md**

Add a "Memory Center UI" section to `PROJECT_OVERVIEW.md` under the frontend section, noting:
- New Memory Center drawer accessible from sidebar
- Uses existing memory API endpoints
- Solstice design system, zero new dependencies
