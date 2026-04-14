# 发送按钮修复与适度增强 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 ChatPanel 发送/停止按钮的7个问题并适度增强交互体验

**Architecture:** 修改 ChatPanel.tsx 组件逻辑（合并状态源、无障碍增强、过渡动画）和 index.css 样式（新增 stop-btn、删除死代码、修复 disabled 态），新增 Playwright E2E 测试覆盖按钮行为

**Tech Stack:** React 19, TypeScript, CSS (Solstice 设计系统), Playwright

---

### Task 1: 删除 CSS 死代码

**Files:**
- Modify: `frontend/src/styles/index.css:1054-1077`

- [ ] **Step 1: 删除 `.send-btn.is-streaming` 和 `.send-spinner` 及 `@keyframes sendSpin`**

删除 `index.css` 第 1054-1077 行的以下块：

```css
.send-btn.is-streaming {
  opacity: 1;
  border-color: rgba(212, 162, 76, 0.3);
  background: linear-gradient(135deg, rgba(212, 162, 76, 0.1), rgba(212, 162, 76, 0.04));
  cursor: default;
}

.send-btn svg {
  width: 18px;
  height: 18px;
}

.send-spinner {
  width: 18px;
  height: 18px;
  border: 2px solid rgba(212, 162, 76, 0.2);
  border-top-color: var(--accent-amber);
  border-radius: 50%;
  animation: sendSpin 0.8s linear infinite;
}

@keyframes sendSpin {
  to { transform: rotate(360deg); }
}
```

保留 `.send-btn svg` 规则（后面 stop-btn 也需要 svg 尺寸控制），把它移到 `.send-btn:disabled` 块之后：

```css
.send-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

.send-btn svg {
  width: 18px;
  height: 18px;
}
```

- [ ] **Step 2: 验证前端构建通过**

Run: `cd /Users/zhaoxiwei/独立开发者的自我修养/travel_agent_pro/frontend && npm run build`

Expected: 构建成功，无 CSS 相关错误

- [ ] **Step 3: 提交**

```bash
git add frontend/src/styles/index.css
git commit -m "fix: 删除发送按钮死代码 CSS（is-streaming, send-spinner）"
```

---

### Task 2: 新增 `.stop-btn` 样式 + 修改 `.send-btn:disabled` opacity + 新增过渡动画类

**Files:**
- Modify: `frontend/src/styles/index.css` (紧接 `.send-btn svg` 之后)

- [ ] **Step 1: 修改 `.send-btn:disabled` opacity**

将 `opacity: 0.2` 改为 `opacity: 0.35`：

```css
.send-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}
```

- [ ] **Step 2: 在 `.send-btn svg` 块之后添加 `.stop-btn` 完整样式**

```css
.stop-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 46px;
  height: 46px;
  border-radius: var(--radius-md);
  border: 1px solid rgba(239, 68, 68, 0.4);
  background: linear-gradient(135deg, rgba(239, 68, 68, 0.12), rgba(239, 68, 68, 0.04));
  color: #f87171;
  cursor: pointer;
  transition: all var(--transition-smooth);
  flex-shrink: 0;
}

.stop-btn:hover {
  background: linear-gradient(135deg, rgba(239, 68, 68, 0.2), rgba(239, 68, 68, 0.08));
  box-shadow: 0 0 12px rgba(239, 68, 68, 0.15);
  transform: translateY(-2px);
}

.stop-btn:active {
  transform: translateY(0) scale(0.96);
}

.stop-btn svg {
  width: 18px;
  height: 18px;
}
```

- [ ] **Step 3: 在 `.stop-btn svg` 之后添加按钮过渡动画类**

```css
.send-btn--hidden,
.stop-btn--hidden {
  opacity: 0;
  pointer-events: none;
  position: absolute;
  transform: scale(0.8);
}

.send-btn,
.stop-btn {
  transition: opacity 0.15s ease, transform 0.15s ease;
}
```

注意：这会把 `.send-btn` 原有的 `transition: all var(--transition-smooth)` 覆盖。需要将 `.send-btn` 原有的 transition 保留为回退，新规则在更具体的位置补上。最终 `.send-btn` 的 transition 行保留为 `transition: all var(--transition-smooth);`，但加上 `opacity` 和 `transform` 细项确保过渡平滑。由于 `all` 已包含 `opacity` 和 `transform`，无需额外修改。但 `.stop-btn--hidden` 的 `position: absolute` 需要父容器 `.input-bar` 加 `position: relative`。

- [ ] **Step 4: 给 `.input-bar` 添加 `position: relative`**

在 `.input-bar` 规则中追加 `position: relative;`：

```css
.input-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 16px 28px 20px;
  background: var(--bg-glass);
  backdrop-filter: blur(20px) saturate(1.3);
  border-top: 1px solid var(--border-subtle);
  position: relative;
}
```

- [ ] **Step 5: 验证前端构建通过**

Run: `cd /Users/zhaoxiwei/独立开发者的自我修养/travel_agent_pro/frontend && npm run build`

- [ ] **Step 6: 提交**

```bash
git add frontend/src/styles/index.css
git commit -m "feat: 新增 stop-btn 样式、修复 disabled opacity、添加按钮过渡动画"
```

---

### Task 3: ChatPanel 组件逻辑修复

**Files:**
- Modify: `frontend/src/components/ChatPanel.tsx`

- [ ] **Step 1: 移除 `sendingRef`**

删除第 111 行：
```tsx
const sendingRef = useRef(false)
```

- [ ] **Step 2: 修改 `handleSend` 守卫和逻辑**

将 `handleSend` 中 `sendingRef.current` 引用替换为 `streaming` state 检查：

```tsx
const handleSend = async () => {
  if (!input.trim() || streaming) return

  lastEventTimeRef.current = Date.now()
  setConnectionWarning(false)
  setCanContinue(false)
  const userMsg = input.trim()
  // ... 以下不变
```

在 `finally` 块中移除 `sendingRef.current = false`，保留 `setStreaming(false)` 和空消息清理逻辑：

```tsx
  try {
    await sendMessage(sessionId, userMsg, createEventHandler(state))
  } finally {
    setStreaming(false)
    const lastId = state.currentAssistantId
    setMessages((prev) => prev.filter((message) =>
      !(message.id === lastId && message.role === 'assistant' && !message.content.trim())
    ))
  }
```

- [ ] **Step 3: 修改 `handleStop`**

移除 `sendingRef.current = false`：

```tsx
const handleStop = async () => {
  try {
    await cancel(sessionId)
  } finally {
    setStreaming(false)
  }
}
```

- [ ] **Step 4: 修改 `handleContinue` 守卫和逻辑**

守卫改为 `if (streaming) return`，移除 `sendingRef.current` 操作：

```tsx
const handleContinue = async () => {
  if (streaming) return
  setCanContinue(false)
  setStreaming(true)
  lastEventTimeRef.current = Date.now()
  setConnectionWarning(false)

  const state: EventHandlerState = {
    currentAssistantId: createMessageId(),
    assistantContent: '',
    toolMessageIds: new Map<string, string>(),
  }
  setMessages((prev) => [
    ...prev,
    { id: state.currentAssistantId, role: 'assistant' as const, content: '' },
  ])

  try {
    await continueGeneration(sessionId, createEventHandler(state))
  } finally {
    setStreaming(false)
    const lastId = state.currentAssistantId
    setMessages((prev) => prev.filter((message) =>
      !(message.id === lastId && message.role === 'assistant' && !message.content.trim())
    ))
  }
}
```

- [ ] **Step 5: 验证前端构建通过**

Run: `cd /Users/zhaoxiwei/独立开发者的自我修养/travel_agent_pro/frontend && npm run build`

- [ ] **Step 6: 提交**

```bash
git add frontend/src/components/ChatPanel.tsx
git commit -m "refactor: 移除 sendingRef，统一使用 streaming 状态"
```

---

### Task 4: ChatPanel JSX 渲染增强（无障碍 + 过渡动画 + placeholder + SVG 图标）

**Files:**
- Modify: `frontend/src/components/ChatPanel.tsx`

- [ ] **Step 1: 替换停止按钮从字符 "■" 为 SVG 图标**

找到渲染区中的：
```tsx
<button type="button" className="stop-btn" onClick={() => void handleStop()} title="停止生成">
  ■
</button>
```

替换为：
```tsx
<button type="button" className="stop-btn" onClick={() => void handleStop()} aria-label="停止生成" title="停止生成">
  <svg viewBox="0 0 24 24" fill="currentColor" width="18" height="18">
    <rect x="6" y="6" width="12" height="12" rx="2" />
  </svg>
</button>
```

- [ ] **Step 2: 增强发送按钮无障碍属性**

找到：
```tsx
<button type="button" className="send-btn" onClick={() => void handleSend()} disabled={!input.trim()}>
```

替换为：
```tsx
<button type="button" className="send-btn" onClick={() => void handleSend()} disabled={!input.trim()} aria-label="发送消息" title={!input.trim() ? '请输入内容' : '发送'}>
```

- [ ] **Step 3: 修改按钮切换逻辑为 CSS 显隐过渡**

将原来条件渲染的两个按钮改为同时存在于 DOM，通过 CSS 类控制显隐。

找到整个按钮渲染部分（从 `{streaming ? (` 到对应的 `)}`）：
```tsx
{streaming ? (
  <button type="button" className="stop-btn" onClick={() => void handleStop()} aria-label="停止生成" title="停止生成">
    <svg viewBox="0 0 24 24" fill="currentColor" width="18" height="18">
      <rect x="6" y="6" width="12" height="12" rx="2" />
    </svg>
  </button>
) : (
  <button type="button" className="send-btn" onClick={() => void handleSend()} disabled={!input.trim()} aria-label="发送消息" title={!input.trim() ? '请输入内容' : '发送'}>
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="22" y1="2" x2="11" y2="13" />
      <polygon points="22 2 15 22 11 13 2 9 22 2" />
    </svg>
  </button>
)}
```

替换为两个并排按钮，用 CSS 类控制显隐：
```tsx
<button
  type="button"
  className={`send-btn ${streaming ? 'send-btn--hidden' : ''}`}
  onClick={() => void handleSend()}
  disabled={!input.trim()}
  aria-label="发送消息"
  title={!input.trim() ? '请输入内容' : '发送'}
>
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <line x1="22" y1="2" x2="11" y2="13" />
    <polygon points="22 2 15 22 11 13 2 9 22 2" />
  </svg>
</button>
<button
  type="button"
  className={`stop-btn ${!streaming ? 'stop-btn--hidden' : ''}`}
  onClick={() => void handleStop()}
  aria-label="停止生成"
  title="停止生成"
>
  <svg viewBox="0 0 24 24" fill="currentColor" width="18" height="18">
    <rect x="6" y="6" width="12" height="12" rx="2" />
  </svg>
</button>
```

- [ ] **Step 4: 修改 placeholder 文本**

找到输入框的 `placeholder` 属性，确认已从 `"告诉我你想去哪里..."` 改为 `"告诉我你想去哪里…（Enter 发送）"`。

- [ ] **Step 5: 验证前端构建通过**

Run: `cd /Users/zhaoxiwei/独立开发者的自我修养/travel_agent_pro/frontend && npm run build`

- [ ] **Step 6: 提交**

```bash
git add frontend/src/components/ChatPanel.tsx
git commit -m "feat: 发送/停止按钮无障碍增强、SVG图标、过渡动画、Enter提示"
```

---

### Task 5: Playwright E2E 测试

**Files:**
- Create: `e2e-send-button.spec.ts`

- [ ] **Step 1: 编写 E2E 测试文件**

创建 `e2e-send-button.spec.ts`，使用 `page.route()` mock 后端 API：

```typescript
import { expect, test } from '@playwright/test'

const MOCK_SESSION_ID = 'test-session-001'
const MOCK_SESSION = {
  session_id: MOCK_SESSION_ID,
  title: '测试会话',
  phase: 1,
  status: 'active',
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
}

const MOCK_PLAN = {
  phase: 1,
  destination: '',
}

function createSseBody(events: object[]): string {
  return events.map((e) => `data: ${JSON.stringify(e)}`).join('\n\n') + '\n\n'
}

async function installMockRoutes(page: import('@playwright/test').Page) {
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    const pathname = url.pathname

    if (pathname === '/api/sessions' && route.request().method() === 'GET') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([MOCK_SESSION]) })
      return
    }

    if (pathname === '/api/sessions' && route.request().method() === 'POST') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_SESSION) })
      return
    }

    if (pathname === `/api/plan/${MOCK_SESSION_ID}`) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_PLAN) })
      return
    }

    if (pathname === `/api/messages/${MOCK_SESSION_ID}`) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) })
      return
    }

    if (pathname === `/api/chat/${MOCK_SESSION_ID}` && route.request().method() === 'POST') {
      const events = [
        { type: 'text_delta', content: '你好！' },
        { type: 'done' },
      ]
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
        body: createSseBody(events),
      })
      return
    }

    if (pathname === `/api/chat/${MOCK_SESSION_ID}/cancel` && route.request().method() === 'POST') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'cancelled' }) })
      return
    }

    await route.fulfill({
      status: 404,
      contentType: 'application/json',
      body: JSON.stringify({ detail: `Unhandled mock route: ${pathname}` }),
    })
  })
}

test.describe('发送按钮交互', () => {
  test.beforeEach(async ({ page }) => {
    await installMockRoutes(page)
    await page.goto('/')
    await expect(page.locator('.input-bar')).toBeVisible({ timeout: 15000 })
  })

  test('空输入时发送按钮 disabled', async ({ page }) => {
    const sendBtn = page.locator('.send-btn')
    await expect(sendBtn).toBeDisabled()
  })

  test('有内容时发送按钮 enabled', async ({ page }) => {
    const input = page.locator('.input-bar input')
    await input.fill('你好')
    const sendBtn = page.locator('.send-btn')
    await expect(sendBtn).toBeEnabled()
  })

  test('Enter 发送消息', async ({ page }) => {
    const input = page.locator('.input-bar input')
    await input.fill('你好')
    await input.press('Enter')
    const userMsg = page.locator('.message.user')
    await expect(userMsg.first()).toBeVisible({ timeout: 10000 })
    await expect(input).toHaveValue('')
  })

  test('发送后显示停止按钮', async ({ page }) => {
    const input = page.locator('.input-bar input')
    await input.fill('你好')

    await page.route(`**/api/chat/${MOCK_SESSION_ID}`, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 5000))
      const events = [
        { type: 'text_delta', content: '正在思考...' },
        { type: 'done' },
      ]
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
        body: createSseBody(events),
      })
    })

    await page.locator('.send-btn').click()
    const stopBtn = page.locator('.stop-btn:not(.stop-btn--hidden)')
    await expect(stopBtn).toBeVisible({ timeout: 5000 })
  })

  test('停止按钮点击后恢复为发送按钮', async ({ page }) => {
    const input = page.locator('.input-bar input')
    await input.fill('你好')

    let resolveSse: (() => void) | undefined
    await page.route(`**/api/chat/${MOCK_SESSION_ID}`, async (route) => {
      await new Promise<void>((resolve) => { resolveSse = resolve })
      const events = [
        { type: 'text_delta', content: '正在思考...' },
        { type: 'done' },
      ]
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
        body: createSseBody(events),
      })
    })

    await page.locator('.send-btn').click()
    const stopBtn = page.locator('.stop-btn:not(.stop-btn--hidden)')
    await expect(stopBtn).toBeVisible({ timeout: 5000 })

    await stopBtn.click()

    if (resolveSse) resolveSse()

    const sendBtn = page.locator('.send-btn:not(.send-btn--hidden)')
    await expect(sendBtn).toBeVisible({ timeout: 5000 })
  })

  test('发送按钮有 aria-label', async ({ page }) => {
    const sendBtn = page.locator('.send-btn')
    await expect(sendBtn).toHaveAttribute('aria-label', '发送消息')
  })

  test('停止按钮有 aria-label', async ({ page }) => {
    const input = page.locator('.input-bar input')
    await input.fill('你好')

    await page.route(`**/api/chat/${MOCK_SESSION_ID}`, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 5000))
      const events = [{ type: 'text_delta', content: '...' }, { type: 'done' }]
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
        body: createSseBody(events),
      })
    })

    await page.locator('.send-btn').click()
    const stopBtn = page.locator('.stop-btn')
    await expect(stopBtn).toHaveAttribute('aria-label', '停止生成', { timeout: 5000 })
  })

  test('空输入时发送按钮 disabled 且可见（opacity 可感知）', async ({ page }) => {
    const sendBtn = page.locator('.send-btn')
    await expect(sendBtn).toBeDisabled()
    const opacity = await sendBtn.evaluate((el) => getComputedStyle(el).opacity)
    expect(Number(opacity)).toBeGreaterThanOrEqual(0.3)
  })

  test('placeholder 包含 Enter 发送提示', async ({ page }) => {
    const input = page.locator('.input-bar input')
    const placeholder = await input.getAttribute('placeholder')
    expect(placeholder).toContain('Enter 发送')
  })
})
```

- [ ] **Step 2: 更新 playwright.config.ts 以包含新测试文件**

`playwright.config.ts` 当前的 `testMatch` 逻辑只在有显式参数时运行指定脚本，否则运行 `e2e-test.spec.ts`。需要把 `e2e-send-button.spec.ts` 也加入默认匹配：

找到：
```typescript
testMatch: explicitMatches.length > 0 ? explicitMatches : 'e2e-test.spec.ts',
```

替换为：
```typescript
testMatch: explicitMatches.length > 0 ? explicitMatches : ['e2e-test.spec.ts', 'e2e-send-button.spec.ts'],
```

- [ ] **Step 3: 运行测试验证**

Run: `cd /Users/zhaoxiwei/独立开发者的自我修养/travel_agent_pro && npx playwright test e2e-send-button.spec.ts`

注意：需要前端 dev server 运行在 `http://127.0.0.1:5173`。

- [ ] **Step 4: 提交**

```bash
git add e2e-send-button.spec.ts playwright.config.ts
git commit -m "test: 新增发送按钮 E2E 测试覆盖"
```

---

### Task 6: 最终验证 + 更新 PROJECT_OVERVIEW

**Files:**
- Modify: `PROJECT_OVERVIEW.md`

- [ ] **Step 1: 前端完整构建**

Run: `cd /Users/zhaoxiwei/独立开发者的自我修养/travel_agent_pro/frontend && npm run build`

Expected: 构建成功，无类型错误

- [ ] **Step 2: 更新 PROJECT_OVERVIEW.md 中 ChatPanel 描述**

在 `ChatPanel.tsx` 描述中更新关键改动点，确保 Doc 与代码一致：

找到：
```
│   │   │   ├── ChatPanel.tsx   # 聊天面板: SSE 流, 工具卡片, 状态变化展示, 停止按钮, 连接超时检测, 继续按钮, 未完成消息标注
```

替换为：
```
│   │   │   ├── ChatPanel.tsx   # 聊天面板: SSE 流, 工具卡片, 状态变化展示, 发送/停止按钮(过渡动画+无障碍), 连接超时检测, 继续按钮, 未完成消息标注
```

- [ ] **Step 3: 提交**

```bash
git add PROJECT_OVERVIEW.md
git commit -m "docs: 更新 PROJECT_OVERVIEW ChatPanel 描述"
```