import { expect, test } from '@playwright/test'

const MOCK_SESSION_ID = 'retry-experience-session'
const MOCK_SESSION = {
  session_id: MOCK_SESSION_ID,
  title: '重试体验测试会话',
  phase: 1,
  status: 'active',
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
}

const MOCK_PLAN = {
  session_id: MOCK_SESSION_ID,
  phase: 1,
  destination: null,
  dates: null,
  phase3_step: 'brief',
  trip_brief: undefined,
  candidate_pool: [],
  shortlist: [],
  skeleton_plans: [],
  selected_skeleton_id: null,
  transport_options: [],
  selected_transport: null,
  accommodation_options: [],
  budget: null,
  travelers: null,
  accommodation: null,
  constraints: [],
  preferences: [],
  risks: [],
  alternatives: [],
  daily_plans: [],
  backtrack_history: [],
}

function createSseBody(events: object[]) {
  return events.map((e) => `data: ${JSON.stringify(e)}`).join('\n\n') + '\n\n'
}

async function installBaseRoutes(page: import('@playwright/test').Page) {
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

    if (pathname === `/api/chat/${MOCK_SESSION_ID}/cancel` && route.request().method() === 'POST') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'cancelled' }) })
      return
    }

    await route.fallback()
  })
}

test.describe('重试与中断恢复体验', () => {
  test.beforeEach(async ({ page }) => {
    await installBaseRoutes(page)
    await page.goto('/')
    await expect(page.locator('.input-bar')).toBeVisible({ timeout: 15000 })
  })

  test('可继续生成时显示状态条和继续按钮', async ({ page }) => {
    let continueCalled = false

    await page.route(`**/api/chat/${MOCK_SESSION_ID}`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
        body: createSseBody([
          { type: 'text_delta', content: '先给你一部分内容。' },
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
      continueCalled = true
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
        body: createSseBody([
          { type: 'text_delta', content: '继续补完。' },
          { type: 'done' },
        ]),
      })
    })

    await page.locator('.input-bar input').fill('测试继续生成')
    await page.locator('.send-btn:not(.send-btn--hidden)').click()

    const status = page.locator('.chat-status')
    await expect(status).toContainText('回复已中断，可从当前位置继续生成。')
    await expect(status).toContainText('回复阶段：模型回复过程中连接中断。')

    await page.getByRole('button', { name: '继续生成' }).click()
    await expect.poll(() => continueCalled).toBe(true)
    await expect(page.locator('.chat-status')).toHaveCount(0)
    await expect(page.locator('.message.assistant .bubble').last()).toContainText('继续补完。')
  })

  test('可重试错误时显示重新发送按钮并再次发起上一条消息', async ({ page }) => {
    const requests: string[] = []

    await page.route(`**/api/chat/${MOCK_SESSION_ID}`, async (route) => {
      const body = route.request().postDataJSON() as { message?: string }
      requests.push(body.message ?? '')

      const firstAttempt = requests.length === 1
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
        body: firstAttempt
          ? createSseBody([
              {
                type: 'error',
                message: '模型服务暂时繁忙，本轮回复已中断。',
                error_code: 'LLM_TRANSIENT_ERROR',
                retryable: true,
                can_continue: false,
                failure_phase: 'connection',
              },
            ])
          : createSseBody([
              { type: 'text_delta', content: '第二次发送成功。' },
              { type: 'done' },
            ]),
      })
    })

    await page.locator('.input-bar input').fill('帮我重试这条消息')
    await page.locator('.send-btn:not(.send-btn--hidden)').click()

    const status = page.locator('.chat-status')
    await expect(status).toContainText('本轮生成失败，可重新发送上一条消息。')
    await expect(status).toContainText('连接阶段：模型服务暂时繁忙，本轮回复已中断。')

    await page.getByRole('button', { name: '重新发送' }).click()
    await expect.poll(() => requests.length).toBe(2)
    await expect.poll(() => requests[1]).toBe('帮我重试这条消息')
    await expect(page.locator('.chat-status')).toHaveCount(0)
    await expect(page.locator('.message.assistant .bubble').last()).toContainText('第二次发送成功。')
  })

  test('用户停止后显示已停止状态并允许重新发送', async ({ page }) => {
    let sendCount = 0
    let releaseFirstStream: (() => void) | null = null

    await page.route(`**/api/chat/${MOCK_SESSION_ID}`, async (route) => {
      sendCount += 1
      if (sendCount === 1) {
        await new Promise<void>((resolve) => {
          releaseFirstStream = resolve
        })
        await route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
          body: '',
        })
        return
      }

      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
        body: createSseBody([
          { type: 'text_delta', content: '重新发送后恢复正常。' },
          { type: 'done' },
        ]),
      })
    })

    await page.locator('.input-bar input').fill('我要先停止再重发')
    await page.locator('.send-btn:not(.send-btn--hidden)').click()

    const stopBtn = page.locator('.stop-btn:not(.stop-btn--hidden)')
    await expect(stopBtn).toBeVisible({ timeout: 5000 })
    await stopBtn.click()
    releaseFirstStream?.()

    const status = page.locator('.chat-status')
    await expect(status).toContainText('已停止生成。')
    await expect(status).toContainText('可以重新发送上一条消息，或修改内容后再发。')

    await page.getByRole('button', { name: '重新发送' }).click()
    await expect.poll(() => sendCount).toBe(2)
    await expect(page.locator('.chat-status')).toHaveCount(0)
    await expect(page.locator('.message.assistant .bubble').last()).toContainText('重新发送后恢复正常。')
  })

  test('不可重试错误只显示说明，不展示动作按钮', async ({ page }) => {
    await page.route(`**/api/chat/${MOCK_SESSION_ID}`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
        body: createSseBody([
          {
            type: 'error',
            message: '请求参数异常，请缩短对话长度后重试。',
            error_code: 'LLM_BAD_REQUEST',
            retryable: false,
            can_continue: false,
            failure_phase: 'connection',
          },
        ]),
      })
    })

    await page.locator('.input-bar input').fill('触发不可重试错误')
    await page.locator('.send-btn:not(.send-btn--hidden)').click()

    const status = page.locator('.chat-status')
    await expect(status).toContainText('本轮生成未完成，请调整后重新发送。')
    await expect(status).toContainText('连接阶段：请求参数异常，请缩短对话长度后重试。')
    await expect(page.getByRole('button', { name: '重新发送' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: '继续生成' })).toHaveCount(0)
  })
})
