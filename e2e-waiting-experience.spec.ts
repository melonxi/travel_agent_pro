import { expect, test, type Page } from '@playwright/test'

type MockEvent = {
  delayMs: number
  payload: Record<string, unknown>
}

type MockScenario = {
  expectedMessage: string
  events: MockEvent[]
}

const MOCK_SESSION_ID = 'waiting-experience-session'
const MOCK_SESSION = {
  session_id: MOCK_SESSION_ID,
  title: '等待体验测试会话',
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
  trip_brief: {},
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

async function installDeterministicWaitingMock(page: Page, scenario: MockScenario) {
  await page.addInitScript(
    ({ session, plan, scenario }) => {
      const encoder = new TextEncoder()
      const mockWindow = window as Window & { __waitingMockState?: { chatRequests: number } }
      let currentSession = session
      let currentPlan = plan
      let currentMessages: Array<{ seq: number; role: 'user' | 'assistant' | 'tool'; content: string }> = []
      let chatConsumed = false

      mockWindow.__waitingMockState = { chatRequests: 0 }

      const jsonResponse = (data: unknown, status = 200) =>
        new Response(JSON.stringify(data), {
          status,
          headers: { 'Content-Type': 'application/json' },
        })

      const toSseChunk = (event: unknown) => encoder.encode(`data: ${JSON.stringify(event)}\n\n`)
      const originalFetch = window.fetch.bind(window)

      window.fetch = async (input, init) => {
        const request = input instanceof Request ? input : null
        const url = new URL(typeof input === 'string' ? input : request?.url ?? String(input), window.location.origin)
        const method = (init?.method ?? request?.method ?? 'GET').toUpperCase()
        const sessionId = session.session_id

        if (url.pathname === '/api/sessions' && method === 'GET') {
          return jsonResponse([currentSession])
        }

        if (url.pathname === '/api/sessions' && method === 'POST') {
          return jsonResponse(currentSession)
        }

        if (url.pathname === `/api/plan/${sessionId}` && method === 'GET') {
          return jsonResponse(currentPlan)
        }

        if (url.pathname === `/api/messages/${sessionId}` && method === 'GET') {
          return jsonResponse(currentMessages)
        }

        if (url.pathname === `/api/chat/${sessionId}/cancel` && method === 'POST') {
          return jsonResponse({ status: 'cancelled' })
        }

        if (url.pathname === `/api/chat/${sessionId}` && method === 'POST') {
          mockWindow.__waitingMockState.chatRequests += 1
          const bodyText = typeof init?.body === 'string'
            ? init.body
            : request
              ? await request.clone().text()
              : null
          const message = bodyText ? JSON.parse(bodyText).message : null

          if (message !== scenario.expectedMessage) {
            return jsonResponse(
              {
                detail: 'Unexpected mocked message',
                expected: scenario.expectedMessage,
                received: message,
              },
              400,
            )
          }

          if (chatConsumed) {
            return jsonResponse({ detail: 'Mocked chat already consumed' }, 410)
          }

          chatConsumed = true

          return new Response(
            new ReadableStream({
              start(controller) {
                let assistantContent = ''
                let closed = false

                const finalize = () => {
                  if (closed) return
                  const nextMessages = [
                    ...currentMessages,
                    { seq: currentMessages.length + 1, role: 'user' as const, content: scenario.expectedMessage },
                  ]
                  if (assistantContent) {
                    nextMessages.push({
                      seq: nextMessages.length + 1,
                      role: 'assistant',
                      content: assistantContent,
                    })
                  }
                  currentMessages = nextMessages
                  closed = true
                  controller.close()
                }

                for (const event of scenario.events) {
                  window.setTimeout(() => {
                    if (closed) return

                    if (event.payload.type === 'text_delta' && typeof event.payload.content === 'string') {
                      assistantContent += event.payload.content
                    }

                    if (
                      event.payload.type === 'state_update'
                      && event.payload.plan
                      && typeof event.payload.plan === 'object'
                    ) {
                      currentPlan = event.payload.plan as typeof plan
                      if (typeof currentPlan.phase === 'number') {
                        currentSession = {
                          ...currentSession,
                          phase: currentPlan.phase,
                        }
                      }
                    }

                    controller.enqueue(toSseChunk(event.payload))
                  }, event.delayMs)
                }

                const lastDelayMs = scenario.events.reduce(
                  (maxDelay, event) => Math.max(maxDelay, event.delayMs),
                  0,
                )
                window.setTimeout(finalize, lastDelayMs + 100)
              },
            }),
            {
              status: 200,
              headers: {
                'Content-Type': 'text/event-stream',
                'Cache-Control': 'no-cache',
              },
            },
          )
        }

        return originalFetch(input, init)
      }
    },
    {
      session: MOCK_SESSION,
      plan: MOCK_PLAN,
      scenario,
    },
  )
}

async function openChatAndSend(page: Page, message: string) {
  await page.goto('/')
  await expect(page.locator('.input-bar input')).toBeVisible({ timeout: 15000 })
  await page.locator('.input-bar input').fill(message)
  await page.locator('.send-btn:not(.send-btn--hidden)').click()
}

test.describe('Agent waiting experience', () => {
  test('ThinkingBubble appears immediately after send', async ({ page }) => {
    const scenario: MockScenario = {
      expectedMessage: '去成都',
      events: [
        { delayMs: 900, payload: { type: 'text_delta', content: '先给你一个方向。' } },
        { delayMs: 950, payload: { type: 'done' } },
      ],
    }

    await installDeterministicWaitingMock(page, scenario)
    await openChatAndSend(page, scenario.expectedMessage)

    await expect(page.getByTestId('thinking-bubble')).toBeVisible({ timeout: 500 })
    await expect(page.getByTestId('thinking-bubble')).toContainText('思考中…')
    await expect.poll(async () => page.evaluate(() => (
      (window as Window & { __waitingMockState?: { chatRequests: number } }).__waitingMockState?.chatRequests ?? 0
    ))).toBe(1)
    await expect(page.locator('.message.assistant .bubble').last()).toContainText('先给你一个方向。')
  })

  test('ThinkingBubble dismisses on first text_delta', async ({ page }) => {
    const scenario: MockScenario = {
      expectedMessage: '帮我先收起等待气泡',
      events: [
        { delayMs: 800, payload: { type: 'text_delta', content: '第一段回复已经到了。' } },
        { delayMs: 850, payload: { type: 'done' } },
      ],
    }

    await installDeterministicWaitingMock(page, scenario)
    await openChatAndSend(page, scenario.expectedMessage)

    await expect(page.getByTestId('thinking-bubble')).toBeVisible({ timeout: 500 })
    await expect(page.getByTestId('thinking-bubble')).toBeHidden({ timeout: 1100 })
    await expect(page.locator('.message.assistant .bubble').last()).toContainText('第一段回复已经到了。')
  })

  test('tool card shows human_label and elapsed timer', async ({ page }) => {
    const scenario: MockScenario = {
      expectedMessage: '给我找点京都四月慢逛灵感',
      events: [
        {
          delayMs: 100,
          payload: {
            type: 'tool_call',
            tool_call: {
              id: 'tc_waiting_tool',
              name: 'xiaohongshu_search',
              human_label: '翻小红书找灵感',
              arguments: {
                query: '京都 四月 慢逛 美食',
              },
            },
          },
        },
        {
          delayMs: 1650,
          payload: {
            type: 'tool_result',
            tool_result: {
              tool_call_id: 'tc_waiting_tool',
              status: 'success',
              data: {
                notes: 3,
              },
            },
          },
        },
        {
          delayMs: 1700,
          payload: {
            type: 'text_delta',
            content: '我先筛出 3 条适合四月慢慢逛的京都灵感。',
          },
        },
        { delayMs: 1750, payload: { type: 'done' } },
      ],
    }

    await installDeterministicWaitingMock(page, scenario)
    await openChatAndSend(page, scenario.expectedMessage)

    const subtitle = page.locator('.tool-subtitle').first()
    const elapsed = page.locator('.tool-elapsed').first()

    await expect(subtitle).toContainText('翻小红书找灵感')
    await expect.poll(async () => (await elapsed.textContent())?.trim() ?? '').toMatch(/^[1-9]\d*\.\d+s$/)
    await expect(page.locator('.message.assistant .bubble').last()).toContainText('我先筛出 3 条适合四月慢慢逛的京都灵感。')
  })

  test('tool card warns when a tool runs longer than 8 seconds', async ({ page }) => {
    const scenario: MockScenario = {
      expectedMessage: '帮我多翻一会儿灵感',
      events: [
        {
          delayMs: 100,
          payload: {
            type: 'tool_call',
            tool_call: {
              id: 'tc_waiting_tool_long',
              name: 'xiaohongshu_search',
              human_label: '翻小红书找灵感',
              arguments: {
                query: '京都 四月 慢逛 灵感',
              },
            },
          },
        },
        {
          delayMs: 10500,
          payload: {
            type: 'tool_result',
            tool_result: {
              tool_call_id: 'tc_waiting_tool_long',
              status: 'success',
              data: {
                notes: 5,
              },
            },
          },
        },
        {
          delayMs: 10550,
          payload: {
            type: 'text_delta',
            content: '这次多翻了一轮，先给你 5 条更像本地人路线的灵感。',
          },
        },
        { delayMs: 10600, payload: { type: 'done' } },
      ],
    }

    await installDeterministicWaitingMock(page, scenario)
    await openChatAndSend(page, scenario.expectedMessage)

    const subtitle = page.locator('.tool-subtitle.long-running').first()
    const elapsed = page.locator('.tool-elapsed').first()

    await expect(subtitle).toContainText('翻小红书找灵感（运行较久，请稍候）', { timeout: 10000 })
    await expect.poll(async () => (await elapsed.textContent())?.trim() ?? '', { timeout: 10000 }).toMatch(/^[89]\.\d+s$|^1\d\.\d+s$/)
  })
})
