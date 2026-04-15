import { readFileSync } from 'node:fs';
import path from 'node:path';
import { test, expect, type Page } from '@playwright/test';

type JsonValue = string | number | boolean | null | JsonObject | JsonValue[];
type JsonObject = { [key: string]: JsonValue };

type DemoSessionMeta = {
  session_id: string;
  title: string;
  phase: number;
  status: string;
  updated_at: string;
};

type DemoStep = {
  user_message: string;
  assistant_text: string;
  plan: JsonObject;
};

type SessionMessage = {
  seq: number;
  role: 'user' | 'assistant' | 'tool';
  content: string;
};

type DemoFixture = {
  session: DemoSessionMeta;
  initial_plan: JsonObject;
  initial_messages: SessionMessage[];
  steps: DemoStep[];
};

const FIXTURE_PATH = path.join(__dirname, 'scripts', 'demo', 'demo-scripted-session.json');
const DEMO_FIXTURE = JSON.parse(readFileSync(FIXTURE_PATH, 'utf-8')) as DemoFixture;
const PHASE_TRANSITION_STEP = DEMO_FIXTURE.steps[1];
const PHASE_TRANSITION_DELAY_MS = 200;

async function installDeterministicPhaseTransitionMock(page: Page) {
  await page.addInitScript(
    ({ fixture, transitionStep, delayMs }) => {
      const encoder = new TextEncoder();
      let currentSession = fixture.session;
      let currentPlan = fixture.initial_plan;
      let currentMessages = fixture.initial_messages;
      let chatConsumed = false;

      const jsonResponse = (data: unknown, status = 200) =>
        new Response(JSON.stringify(data), {
          status,
          headers: { 'Content-Type': 'application/json' },
        });

      const toSseChunk = (event: unknown) => encoder.encode(`data: ${JSON.stringify(event)}\n\n`);

      const originalFetch = window.fetch.bind(window);
      window.fetch = async (input, init) => {
        const request = input instanceof Request ? input : null;
        const url = new URL(typeof input === 'string' ? input : request?.url ?? String(input), window.location.origin);
        const method = (init?.method ?? request?.method ?? 'GET').toUpperCase();
        const sessionId = fixture.session.session_id;

        if (url.pathname === '/api/sessions' && method === 'GET') {
          return jsonResponse([currentSession]);
        }

        if (url.pathname === '/api/sessions' && method === 'POST') {
          return jsonResponse(currentSession);
        }

        if (url.pathname === `/api/plan/${sessionId}` && method === 'GET') {
          return jsonResponse(currentPlan);
        }

        if (url.pathname === `/api/messages/${sessionId}` && method === 'GET') {
          return jsonResponse(currentMessages);
        }

        if (url.pathname === `/api/chat/${sessionId}` && method === 'POST') {
          const bodyText = typeof init?.body === 'string' ? init.body : null;
          const message = bodyText ? JSON.parse(bodyText).message : null;

          if (message !== transitionStep.user_message) {
            return jsonResponse(
              {
                detail: 'Unexpected mocked message',
                expected: transitionStep.user_message,
                received: message,
              },
              400,
            );
          }

          if (chatConsumed) {
            return jsonResponse({ detail: 'Mocked chat already consumed' }, 410);
          }

          chatConsumed = true;

          return new Response(
            new ReadableStream({
              start(controller) {
                controller.enqueue(
                  toSseChunk({
                    type: 'phase_transition',
                    from_phase: 1,
                    to_phase: Number(transitionStep.plan.phase ?? 3),
                    to_step: transitionStep.plan.phase3_step ?? null,
                    reason: 'mocked deterministic transition',
                  }),
                );
                controller.enqueue(
                  toSseChunk({
                    type: 'text_delta',
                    content: transitionStep.assistant_text,
                  }),
                );

                window.setTimeout(() => {
                  currentPlan = transitionStep.plan;
                  currentSession = {
                    ...currentSession,
                    phase: Number(transitionStep.plan.phase ?? currentSession.phase),
                  };
                  currentMessages = [
                    ...currentMessages,
                    { seq: currentMessages.length + 1, role: 'user', content: transitionStep.user_message },
                    { seq: currentMessages.length + 2, role: 'assistant', content: transitionStep.assistant_text },
                  ];

                  controller.enqueue(
                    toSseChunk({
                      type: 'state_update',
                      plan: transitionStep.plan,
                    }),
                  );
                  controller.enqueue(toSseChunk({ type: 'done' }));
                  controller.close();
                }, delayMs);
              },
            }),
            {
              status: 200,
              headers: {
                'Content-Type': 'text/event-stream',
                'Cache-Control': 'no-cache',
              },
            },
          );
        }

        return originalFetch(input, init);
      };
    },
    {
      fixture: DEMO_FIXTURE,
      transitionStep: PHASE_TRANSITION_STEP,
      delayMs: PHASE_TRANSITION_DELAY_MS,
    },
  );
}

test.describe('Travel Agent Pro Phase 1 Flow', () => {
  test('abstract destination intent triggers destination recommendation flow', async ({ page }) => {
    await page.goto('/');

    await expect(page.locator('text=旅行者')).toBeVisible({ timeout: 15000 });
    await expect(page.locator('input[placeholder*="告诉我你想去哪里"]')).toBeVisible({
      timeout: 15000,
    });

    const input = page.locator('input[placeholder*="告诉我你想去哪里"]');
    await input.fill('我想找个海边放松、风景好一点的地方');
    await page.locator('.send-btn').click();

    const toolCard = page.locator('.tool-card').first();
    await expect(toolCard).toBeVisible({ timeout: 90000 });
    await expect(toolCard.locator('.tool-status')).toHaveText(/成功|执行中/, {
      timeout: 90000,
    });
    await expect(toolCard).toContainText(/xiaohongshu_search|web_search|quick_travel_search|update_plan_state/, {
      timeout: 15000,
    });

    const toggle = toolCard.getByRole('button', { name: /详情/ });
    await toggle.click();
    await expect(toolCard.locator('.tool-section')).toBeVisible({ timeout: 15000 });

    const lastAssistantBubble = page.locator('.message.assistant .bubble').last();
    await expect(lastAssistantBubble).toBeVisible({ timeout: 90000 });
    await expect(lastAssistantBubble).not.toHaveText(/^$/, { timeout: 90000 });
  });

  test('mocked phase_transition updates phase indicator before state_update arrives', async ({ page }) => {
    await installDeterministicPhaseTransitionMock(page);
    await page.goto('/');

    const input = page.locator('input[placeholder*="告诉我你想去哪里"]');
    await expect(input).toBeVisible({ timeout: 15000 });

    await input.fill(PHASE_TRANSITION_STEP.user_message);
    await page.locator('.send-btn').click();

    const activePhaseLabel = page.locator('.phase-node.active .phase-label');
    const transitionCard = page.locator('.phase-transition-card');
    const destinationBanner = page.locator('.destination-banner');

    await expect(activePhaseLabel).toHaveText('日期与住宿', { timeout: 5000 });
    await expect(transitionCard).toBeVisible({ timeout: 5000 });
    await expect(transitionCard).toContainText('已进入日期与住宿');

    await page.waitForTimeout(50);
    await expect(destinationBanner).toHaveCount(0);

    await expect(destinationBanner).toContainText('京都', { timeout: 5000 });
    await expect(page.locator('.phase3-workbench')).toBeVisible({ timeout: 5000 });
  });
});
