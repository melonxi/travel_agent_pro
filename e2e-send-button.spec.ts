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

    await page.locator('.send-btn:not(.send-btn--hidden)').click()
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

    await page.locator('.send-btn:not(.send-btn--hidden)').click()
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

    await page.locator('.send-btn:not(.send-btn--hidden)').click()
    const stopBtn = page.locator('.stop-btn')
    await expect(stopBtn).toHaveAttribute('aria-label', '停止生成', { timeout: 5000 })
  })

  test('空输入时发送按钮 disabled 且 opacity 可感知', async ({ page }) => {
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