# Internal Task Cross-Stream Merge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix `internal_task` cards so a pending card from one SSE stream is updated in place by a later stream's final event instead of leaving the old card stuck on `进行中` and inserting a duplicate completion card.

**Architecture:** Keep the existing per-stream `internalTaskMessageIds` map as the fast path, but add a message-level `internalTaskId` field and a fallback lookup over existing chat messages when a new stream receives an `internal_task` event for an already-rendered task. Cover the regression with an end-to-end test that simulates two streams reusing the same `task.id`.

**Tech Stack:** React 19, TypeScript, Vite, Playwright.

---

## File Structure

- Modify `frontend/src/components/ChatPanel.tsx`: add `internalTaskId` to `ChatMessage`, keep it on inserted internal-task messages, and change `internal_task` handling to fall back to existing rendered messages when the current stream map misses.
- Modify `e2e-retry-experience.spec.ts`: add a cross-stream regression test that first renders an internal task as pending, then resumes via `/continue` and verifies the same task card becomes success without leaving a pending duplicate behind.
- Do not modify `frontend/src/components/MessageBubble.tsx`: it already renders internal task status correctly once `ChatPanel` points updates at the right message.
- Do not modify backend files: the regression is caused by frontend merge scope, not backend event shape.

---

### Task 1: Add a Failing Cross-Stream Regression Test

**Files:**
- Modify: `e2e-retry-experience.spec.ts`
- Test: `e2e-retry-experience.spec.ts`

- [ ] **Step 1: Write the failing Playwright test**

Append this test inside `test.describe('重试与中断恢复体验', ...)` in `e2e-retry-experience.spec.ts`:

```ts
  test('继续生成时复用同一个 internal_task 卡片而不是新增完成卡片', async ({ page }) => {
    await page.route(`**/api/chat/${MOCK_SESSION_ID}`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
        body: createSseBody([
          {
            type: 'internal_task',
            task: {
              id: 'memory_recall:shared_task',
              kind: 'memory_recall',
              label: '记忆召回',
              status: 'pending',
              message: '正在检索本轮可用旅行记忆…',
              blocking: true,
              scope: 'turn',
              started_at: 1776614400,
            },
          },
          {
            type: 'error',
            message: '模型回复过程中连接中断。',
            error_code: 'LLM_STREAM_INTERRUPTED',
            retryable: true,
            can_continue: true,
            failure_phase: 'streaming',
          },
        ]),
      })
    })

    await page.route(`**/api/chat/${MOCK_SESSION_ID}/continue`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
        body: createSseBody([
          {
            type: 'internal_task',
            task: {
              id: 'memory_recall:shared_task',
              kind: 'memory_recall',
              label: '记忆召回',
              status: 'success',
              message: '本轮使用 2 条旅行记忆',
              blocking: true,
              scope: 'turn',
              started_at: 1776614400,
              ended_at: 1776614401,
              result: { count: 2, item_ids: ['mem_1', 'mem_2'] },
            },
          },
          { type: 'text_delta', content: '我已经结合历史偏好继续规划。' },
          { type: 'done' },
        ]),
      })
    })

    await page.locator('.input-bar input').fill('测试跨流 internal task 合并')
    await page.locator('.send-btn:not(.send-btn--hidden)').click()

    await expect(page.locator('.system-internal-task.pending')).toContainText('记忆召回')
    await expect(page.getByRole('button', { name: '继续生成' })).toBeVisible()

    await page.getByRole('button', { name: '继续生成' }).click()

    await expect(page.locator('.system-internal-task')).toHaveCount(1)
    await expect(page.locator('.system-internal-task.pending')).toHaveCount(0)
    await expect(page.locator('.system-internal-task.success')).toContainText('本轮使用 2 条旅行记忆')
    await expect(page.locator('.message.assistant .bubble').last()).toContainText('我已经结合历史偏好继续规划。')
  })
```

- [ ] **Step 2: Run the new E2E test and verify it fails for the right reason**

Run:

```bash
npx playwright test e2e-retry-experience.spec.ts --grep "继续生成时复用同一个 internal_task 卡片而不是新增完成卡片"
```

Expected: FAIL because the page shows two `.system-internal-task` cards or still leaves one `.system-internal-task.pending` behind after continue.

- [ ] **Step 3: Commit the failing test**

```bash
git add e2e-retry-experience.spec.ts
git commit -m "test: cover cross-stream internal task merge"
```

---

### Task 2: Implement Cross-Stream Internal Task Merge

**Files:**
- Modify: `frontend/src/components/ChatPanel.tsx`
- Test: `e2e-retry-experience.spec.ts`

- [ ] **Step 1: Add a stable `internalTaskId` field to chat messages**

In the `ChatMessage` interface in `frontend/src/components/ChatPanel.tsx`, add this field next to `internalTask`:

```ts
  internalTask?: InternalTaskEvent
  internalTaskId?: string
  memoryChip?: { count: number }
```

- [ ] **Step 2: Replace the current `internal_task` merge block with fallback lookup logic**

In `createEventHandler` inside `frontend/src/components/ChatPanel.tsx`, replace the current `event.type === 'internal_task'` block with this implementation:

```ts
    } else if (event.type === 'internal_task' && event.task) {
      const task = event.task
      const mappedMessageId = state.internalTaskMessageIds.get(task.id)
      const startedAt = toClientTimestamp(task.started_at) ?? Date.now()
      const endedAt = toClientTimestamp(task.ended_at)

      setMessages((prev) => {
        const fallbackMessage = mappedMessageId
          ? undefined
          : prev.find((message) => message.role === 'system' && message.internalTaskId === task.id)
        const targetMessageId = mappedMessageId ?? fallbackMessage?.id

        if (targetMessageId) {
          state.internalTaskMessageIds.set(task.id, targetMessageId)
          return prev.map((message) =>
            message.id === targetMessageId
              ? {
                  ...message,
                  content: task.message ?? message.content,
                  startedAt: message.startedAt ?? startedAt,
                  endedAt: endedAt ?? (task.status === 'pending' ? undefined : Date.now()),
                  internalTask: task,
                  internalTaskId: task.id,
                }
              : message,
          )
        }

        const messageId = createMessageId()
        state.internalTaskMessageIds.set(task.id, messageId)
        return insertBeforeAssistant(prev, state.currentAssistantId, {
          id: messageId,
          role: 'system',
          content: task.message ?? '',
          startedAt,
          endedAt,
          internalTask: task,
          internalTaskId: task.id,
        })
      })
```

- [ ] **Step 3: Verify the fallback only targets real internal task messages**

Check that the fallback lookup remains exactly this predicate:

```ts
prev.find((message) => message.role === 'system' && message.internalTaskId === task.id)
```

This prevents merging by label or accidentally updating phase/state/compression system cards.

- [ ] **Step 4: Run the focused E2E test and verify it passes**

Run:

```bash
npx playwright test e2e-retry-experience.spec.ts --grep "继续生成时复用同一个 internal_task 卡片而不是新增完成卡片"
```

Expected: PASS. The UI shows one internal task card, zero pending task cards after continue, and one success task card with the completion message.

- [ ] **Step 5: Commit the minimal implementation**

```bash
git add frontend/src/components/ChatPanel.tsx
git commit -m "fix: merge internal task cards across streams"
```

---

### Task 3: Run Regression Coverage for Existing Internal Task Behavior

**Files:**
- Test: `e2e-retry-experience.spec.ts`
- Test: `e2e-waiting-experience.spec.ts`

- [ ] **Step 1: Run the existing same-stream internal task lifecycle test**

Run:

```bash
npx playwright test e2e-waiting-experience.spec.ts --grep "shows internal task instead of leaving tool card pending during soft judge"
```

Expected: PASS. Same-stream pending -> success behavior still updates in place.

- [ ] **Step 2: Run the continue/retry experience spec as a small regression batch**

Run:

```bash
npx playwright test e2e-retry-experience.spec.ts
```

Expected: PASS. The new cross-stream merge test and existing continue/retry flows all pass together.

- [ ] **Step 3: Update project overview wording if implementation changed terminology**

If the actual code names differ from the spec wording, update `PROJECT_OVERVIEW.md` so the internal task stream section still accurately describes the frontend merge model.

- [ ] **Step 4: Commit the regression-verification state**

```bash
git add PROJECT_OVERVIEW.md e2e-retry-experience.spec.ts e2e-waiting-experience.spec.ts
git commit -m "test: protect internal task lifecycle updates"
```

---

## Self-Review

- Spec coverage: covered the new message field, fallback merge logic, cross-stream regression test, and no-backend-change boundary.
- Placeholder scan: no `TODO`/`TBD`/implicit “test later” steps remain.
- Type consistency: the plan uses `internalTaskId`, `internalTaskMessageIds`, `ChatMessage`, and `InternalTaskEvent`, matching the current frontend naming.
