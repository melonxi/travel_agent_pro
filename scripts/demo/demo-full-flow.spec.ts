import { mkdirSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { expect, test, type Page } from '@playwright/test'

const ROOT_DIR = path.resolve(__dirname, '..', '..')
const SCREENSHOTS_DIR = path.join(ROOT_DIR, 'screenshots', 'demos')
const FIXTURE_PATH = path.join(__dirname, 'demo-scripted-session.json')
const LONG_TIMEOUT = 180_000
const PHASE_LABELS: Record<number, string> = {
  1: '灵感与目的地',
  3: '日期与住宿',
  5: '行程组装',
  7: '出发前查漏',
}

type JsonValue = string | number | boolean | null | JsonObject | JsonValue[]
type JsonObject = { [key: string]: JsonValue }

type DemoSessionMeta = {
  session_id: string
  title: string
  phase: number
  status: string
  updated_at: string
}

type ToolCall = {
  id: string
  name: string
  arguments: JsonObject
}

type ToolResult = {
  tool_call_id: string
  status: string
  data: JsonObject
}

type DemoStep = {
  user_message: string
  assistant_text: string
  tool_calls: ToolCall[]
  tool_results: ToolResult[]
  plan: JsonObject
}

type SessionMessage = {
  seq: number
  role: 'user' | 'assistant' | 'tool'
  content: string
  tool_call_id?: string
  name?: string
  tool_calls?: ToolCall[]
}

type DemoFixture = {
  session: DemoSessionMeta
  initial_plan: JsonObject
  initial_messages: SessionMessage[]
  steps: DemoStep[]
}

function loadFixture(): DemoFixture {
  return JSON.parse(readFileSync(FIXTURE_PATH, 'utf-8')) as DemoFixture
}

function nextSeq(messages: SessionMessage[]): number {
  const last = messages[messages.length - 1]
  return last ? last.seq + 1 : 1
}

function appendStepMessages(messages: SessionMessage[], step: DemoStep): SessionMessage[] {
  let seq = nextSeq(messages)
  const nextMessages = [...messages]

  nextMessages.push({
    seq,
    role: 'user',
    content: step.user_message,
  })
  seq += 1

  nextMessages.push({
    seq,
    role: 'assistant',
    content: step.assistant_text,
    tool_calls: step.tool_calls,
  })
  seq += 1

  for (const result of step.tool_results) {
    const matchingCall = step.tool_calls.find((toolCall) => toolCall.id === result.tool_call_id)
    nextMessages.push({
      seq,
      role: 'tool',
      name: matchingCall?.name ?? 'tool',
      tool_call_id: result.tool_call_id,
      content: JSON.stringify(result.data, null, 2),
    })
    seq += 1
  }

  return nextMessages
}

function toSseBody(step: DemoStep): string {
  const events: JsonObject[] = [
    ...step.tool_calls.map((toolCall) => ({
      type: 'tool_call',
      tool_call: toolCall,
    })),
    ...step.tool_results.map((toolResult) => ({
      type: 'tool_result',
      tool_result: toolResult,
    })),
    {
      type: 'text_delta',
      content: step.assistant_text,
    },
    {
      type: 'state_update',
      plan: step.plan,
    },
    {
      type: 'done',
    },
  ]

  return `${events.map((event) => `data: ${JSON.stringify(event)}`).join('\n\n')}\n\n`
}

async function installDemoRoutes(page: Page, fixture: DemoFixture): Promise<void> {
  let currentPlan = fixture.initial_plan
  let currentMessages = fixture.initial_messages
  let currentSession = fixture.session
  let stepIndex = 0

  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const pathname = url.pathname

    if (pathname === '/api/sessions' && request.method() === 'GET') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([currentSession]),
      })
      return
    }

    if (pathname === '/api/sessions' && request.method() === 'POST') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(currentSession),
      })
      return
    }

    if (pathname === `/api/plan/${fixture.session.session_id}`) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(currentPlan),
      })
      return
    }

    if (pathname === `/api/messages/${fixture.session.session_id}`) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(currentMessages),
      })
      return
    }

    if (pathname === `/api/chat/${fixture.session.session_id}` && request.method() === 'POST') {
      const payload = request.postDataJSON() as { message?: string } | null
      const step = fixture.steps[stepIndex]

      if (!step) {
        await route.fulfill({
          status: 410,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'No scripted steps remaining' }),
        })
        return
      }

      if (payload?.message !== step.user_message) {
        await route.fulfill({
          status: 400,
          contentType: 'application/json',
          body: JSON.stringify({
            detail: 'Unexpected scripted demo message',
            expected: step.user_message,
            received: payload?.message ?? null,
          }),
        })
        return
      }

      currentPlan = step.plan
      currentMessages = appendStepMessages(currentMessages, step)
      currentSession = {
        ...currentSession,
        phase: Number(step.plan.phase ?? currentSession.phase),
        updated_at: new Date(Date.parse(currentSession.updated_at) + 60_000).toISOString(),
      }
      stepIndex += 1

      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: {
          'Cache-Control': 'no-cache',
          Connection: 'keep-alive',
        },
        body: toSseBody(step),
      })
      return
    }

    await route.fulfill({
      status: 404,
      contentType: 'application/json',
      body: JSON.stringify({ detail: `Unhandled demo route: ${pathname}` }),
    })
  })
}

async function sendMessage(page: Page, text: string) {
  const input = page.locator('.input-bar input')
  await expect(input).toBeVisible()
  await input.fill(text)
  await page.locator('.send-btn').click()
}

async function waitForAssistantResponse(page: Page, previousAssistantCount: number) {
  const assistantBubbles = page.locator('.message.assistant')
  await expect(assistantBubbles).toHaveCount(previousAssistantCount + 1, { timeout: LONG_TIMEOUT })
  await expect(page.locator('.send-btn')).not.toHaveClass(/is-streaming/, { timeout: LONG_TIMEOUT })
}

async function assertPhase(page: Page, phase: number) {
  await expect(page.locator('.phase-node.active .phase-label')).toHaveText(PHASE_LABELS[phase], { timeout: LONG_TIMEOUT })
}

async function takeDemoScreenshot(page: Page, name: string) {
  const target = path.join(SCREENSHOTS_DIR, name)
  await page.screenshot({ path: target, fullPage: true })
}

test.beforeAll(() => {
  mkdirSync(SCREENSHOTS_DIR, { recursive: true })
})

test.use({ video: 'on' })

async function saveDemoVideo(page: Page, filename: string) {
  const video = page.video()
  if (!video) return
  const targetPath = path.join(SCREENSHOTS_DIR, filename)
  await page.close()
  await video.saveAs(targetPath)
}

test('demo full flow covers recommendation, planning, and backtrack', async ({ page }) => {
  test.setTimeout(LONG_TIMEOUT * 2)

  const fixture = loadFixture()
  await installDemoRoutes(page, fixture)

  await page.goto('/')
  await expect(page.locator('.brand-name')).toBeVisible({ timeout: LONG_TIMEOUT })
  await expect(page.locator('.input-bar input')).toBeVisible({ timeout: LONG_TIMEOUT })

  const phase1Message = fixture.steps[0]
  const phase3Message = fixture.steps[1]
  const lockSkeletonMessage = fixture.steps[2]
  const lockHotelMessage = fixture.steps[3]
  const phase5Message = fixture.steps[4]
  const backtrackMessage = fixture.steps[5]

  let previousAssistantCount = await page.locator('.message.assistant').count()

  await sendMessage(page, phase1Message.user_message)
  await waitForAssistantResponse(page, previousAssistantCount)
  previousAssistantCount += 1
  await expect(page.locator('.message.assistant').last()).toContainText('京都')
  await expect(page.locator('.message.tool .tool-badge').first()).toContainText(/xiaohongshu_search|web_search|update_plan_state/)
  await assertPhase(page, 1)
  await takeDemoScreenshot(page, 'phase1-recommendations.png')

  await sendMessage(page, phase3Message.user_message)
  await waitForAssistantResponse(page, previousAssistantCount)
  previousAssistantCount += 1
  await expect(page.locator('.destination-banner')).toContainText('京都')
  await assertPhase(page, 3)
  await expect(page.locator('.phase3-workbench')).toBeVisible()
  await expect(page.locator('.p3-skeleton')).toHaveCount(2)
  await takeDemoScreenshot(page, 'phase3-planning.png')

  await sendMessage(page, lockSkeletonMessage.user_message)
  await waitForAssistantResponse(page, previousAssistantCount)
  previousAssistantCount += 1
  await expect(page.locator('.phase3-workbench')).toBeVisible()
  await expect(page.locator('.p3-lockitem')).toHaveCount(3)

  await sendMessage(page, lockHotelMessage.user_message)
  await waitForAssistantResponse(page, previousAssistantCount)
  previousAssistantCount += 1
  await assertPhase(page, 5)
  await expect(page.locator('.destination-banner .dest-chip').filter({ hasText: '住宿' })).toContainText('Nohga Hotel Kiyomizu Kyoto')

  await sendMessage(page, phase5Message.user_message)
  await waitForAssistantResponse(page, previousAssistantCount)
  previousAssistantCount += 1
  await assertPhase(page, 5)
  await expect(page.locator('.day-card')).toHaveCount(2)

  await sendMessage(page, backtrackMessage.user_message)
  await waitForAssistantResponse(page, previousAssistantCount)
  await assertPhase(page, 1)
  await expect(page.locator('.destination-banner')).toHaveCount(0)
  await expect(page.locator('.message.assistant').last()).toContainText('函馆')
  await takeDemoScreenshot(page, 'phase5-backtrack-change-preference.png')
  await saveDemoVideo(page, 'demo-full-flow.webm')
})
