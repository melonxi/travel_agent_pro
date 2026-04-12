import { expect, test } from '@playwright/test'
import { existsSync, mkdirSync, readFileSync } from 'node:fs'
import path from 'node:path'

type JsonRecord = Record<string, unknown>

interface SessionMessage {
  role: 'system' | 'user' | 'assistant' | 'tool'
  content: string | null
  tool_calls?: Array<{
    id: string
    name: string
    arguments: Record<string, unknown>
  }> | null
  tool_call_id?: string | null
  seq: number
}

interface FailureResult {
  scenario_id: string
  name?: string
  session_id?: string
  user_input?: string
  responses?: string[]
  plan_state?: JsonRecord
  messages?: SessionMessage[]
}

const RESULTS_PATH = path.resolve(process.cwd(), 'scripts/failure-analysis/results/failure-results.json')
const SCREENSHOTS_DIR = path.resolve(process.cwd(), 'screenshots/failure-analysis')
function warn(message: string): void {
  console.warn(`[capture_screenshots] ${message}`)
}

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === 'object' && value !== null
}

function asString(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value : undefined
}

function normalizeToolCalls(value: unknown): SessionMessage['tool_calls'] {
  if (!Array.isArray(value)) return null

  const toolCalls = value.flatMap((entry, index) => {
    if (!isRecord(entry)) return []

    const nestedFunction = isRecord(entry.function) ? entry.function : null
    const argumentsValue =
      (isRecord(entry.arguments) ? entry.arguments : undefined) ??
      (typeof entry.arguments === 'string'
        ? (() => {
            try {
              const parsed = JSON.parse(entry.arguments)
              return isRecord(parsed) ? parsed : {}
            } catch {
              return {}
            }
          })()
        : undefined) ??
      (nestedFunction && typeof nestedFunction.arguments === 'string'
        ? (() => {
            try {
              const parsed = JSON.parse(nestedFunction.arguments)
              return isRecord(parsed) ? parsed : {}
            } catch {
              return {}
            }
          })()
        : undefined) ??
      {}

    return [{
      id: asString(entry.id) ?? `tool-call-${index + 1}`,
      name: asString(entry.name) ?? asString(nestedFunction?.name) ?? 'tool',
      arguments: argumentsValue,
    }]
  })

  return toolCalls.length > 0 ? toolCalls : null
}

function normalizeMessage(value: unknown, index: number): SessionMessage | null {
  if (!isRecord(value)) return null

  const role = value.role
  if (role !== 'system' && role !== 'user' && role !== 'assistant' && role !== 'tool') {
    return null
  }

  return {
    role,
    content: typeof value.content === 'string' ? value.content : null,
    tool_calls: normalizeToolCalls(value.tool_calls),
    tool_call_id: typeof value.tool_call_id === 'string' ? value.tool_call_id : null,
    seq: typeof value.seq === 'number' ? value.seq : index + 1,
  }
}

function loadFailureResults(): FailureResult[] {
  if (!existsSync(RESULTS_PATH)) {
    warn(`results file not found: ${path.relative(process.cwd(), RESULTS_PATH)}`)
    return []
  }

  try {
    const raw = readFileSync(RESULTS_PATH, 'utf8')
    const parsed = JSON.parse(raw) as unknown
    if (!Array.isArray(parsed)) {
      warn('results file is not a JSON array; no screenshots will be captured')
      return []
    }

    return parsed.flatMap((entry) => {
      if (!isRecord(entry)) return []
      const scenarioId = asString(entry.scenario_id)
      if (!scenarioId) return []

      const messages = Array.isArray(entry.messages)
        ? entry.messages
            .map((message, index) => normalizeMessage(message, index))
            .filter((message): message is SessionMessage => message !== null)
        : undefined

      return [{
        scenario_id: scenarioId,
        name: asString(entry.name),
        session_id: asString(entry.session_id),
        user_input: asString(entry.user_input),
        responses: Array.isArray(entry.responses)
          ? entry.responses.filter((item): item is string => typeof item === 'string')
          : undefined,
        plan_state: isRecord(entry.plan_state) ? entry.plan_state : undefined,
        messages,
      }]
    })
  } catch (error) {
    warn(`failed to read results file: ${error instanceof Error ? error.message : String(error)}`)
    return []
  }
}

function ensureScreenshotsDir(): void {
  mkdirSync(SCREENSHOTS_DIR, { recursive: true })
}

function buildPlanState(result: FailureResult): JsonRecord {
  return {
    phase: typeof result.plan_state?.phase === 'number' ? result.plan_state.phase : 7,
    destination: null,
    dates: null,
    budget: null,
    accommodation: null,
    daily_plans: [],
    backtrack_history: [],
    ...result.plan_state,
    session_id: result.session_id ?? '',
  }
}

function buildMessages(result: FailureResult): SessionMessage[] {
  if (result.messages && result.messages.length > 0) {
    return [...result.messages].sort((left, right) => left.seq - right.seq)
  }

  const synthesized: SessionMessage[] = []
  let seq = 1

  if (result.user_input) {
    synthesized.push({
      role: 'user',
      content: result.user_input,
      seq,
      tool_calls: null,
      tool_call_id: null,
    })
    seq += 1
  }

  const assistantResponse = result.responses?.join('').trim()
  if (assistantResponse) {
    synthesized.push({
      role: 'assistant',
      content: assistantResponse,
      seq,
      tool_calls: null,
      tool_call_id: null,
    })
  }

  return synthesized
}

function buildRenderedMessages(result: FailureResult): SessionMessage[] {
  return buildMessages(result).filter((message) => message.role !== 'system')
}

function buildSessionList(result: FailureResult): Array<{
  session_id: string
  title: string
  phase: number
  status: 'active'
  updated_at: string
}> {
  const planState = buildPlanState(result)
  const phase = typeof planState.phase === 'number' ? planState.phase : 7

  return [{
    session_id: result.session_id ?? '',
    title: result.name ?? result.scenario_id,
    phase,
    status: 'active',
    updated_at: new Date().toISOString(),
  }]
}

function sanitizeFileName(value: string): string {
  return value.replace(/[^a-zA-Z0-9._-]+/g, '-')
}

async function installSessionRoutes(page: Parameters<typeof test>[0]['page'], result: FailureResult): Promise<void> {
  const sessionId = result.session_id ?? ''
  const planState = buildPlanState(result)
  const messages = buildMessages(result)
  const sessions = buildSessionList(result)

  await page.route('**/api/**', async (route) => {
    const { pathname } = new URL(route.request().url())

    if (pathname === '/api/sessions') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(sessions),
      })
      return
    }

    if (pathname === `/api/plan/${sessionId}`) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(planState),
      })
      return
    }

    if (pathname === `/api/messages/${sessionId}`) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(messages),
      })
      return
    }

    await route.fulfill({
      status: 404,
      contentType: 'application/json',
      body: JSON.stringify({ error: `Unhandled mocked API route: ${pathname}` }),
    })
  })
}

async function waitForChatToRender(
  page: Parameters<typeof test>[0]['page'],
  result: FailureResult,
): Promise<void> {
  const sessionId = result.session_id ?? ''
  const expectedMessages = buildRenderedMessages(result)

  const planResponse = page.waitForResponse((response) =>
    response.url().endsWith(`/api/plan/${sessionId}`) && response.ok(),
  )
  const messagesResponse = page.waitForResponse((response) =>
    response.url().endsWith(`/api/messages/${sessionId}`) && response.ok(),
  )

  await page.goto('/')
  await Promise.all([planResponse, messagesResponse])

  await expect(page.locator('.loading-screen')).toHaveCount(0)
  await expect(page.locator('.chat-panel')).toBeVisible()
  await expect(page.locator('.messages')).toBeVisible()
  await expect(page.locator('.session-badge')).toContainText(sessionId.slice(0, 8))

  if (expectedMessages.length > 0) {
    await expect(page.locator('.messages .message')).toHaveCount(expectedMessages.length)
    await expect(page.locator('.messages .message').first()).toBeVisible()
  } else {
    await expect(page.locator('input[placeholder*="告诉我想去哪里"], input[placeholder*="告诉我你想去哪里"]')).toBeVisible()
  }
}

const allResults = loadFailureResults()
const results = allResults.filter((result) => result.session_id)

if (allResults.length > results.length) {
  warn(`skipping ${allResults.length - results.length} result(s) without session_id`)
}

test.describe('failure analysis screenshot capture', () => {
  test.beforeAll(() => {
    ensureScreenshotsDir()
  })

  if (results.length === 0) {
    test('warns when no screenshot scenarios are available', async () => {
      test.skip(true, `No screenshot scenarios found in ${path.relative(process.cwd(), RESULTS_PATH)}`)
    })
    return
  }

  for (const result of results) {
    test('captures ' + result.scenario_id, async ({ page }) => {
      await installSessionRoutes(page, result)
      await waitForChatToRender(page, result)
      await page.screenshot({
        path: path.join(SCREENSHOTS_DIR, `${sanitizeFileName(result.scenario_id)}.png`),
        fullPage: true,
      })
    })
  }
})
