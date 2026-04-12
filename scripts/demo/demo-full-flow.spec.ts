import { mkdirSync } from 'node:fs';

import { expect, test, type Page } from '@playwright/test';

const LONG_TIMEOUT = 180000;
const INPUT_SELECTOR = 'input[placeholder*="告诉我你想去哪里"]';
const SEND_BUTTON_SELECTOR = '.send-btn';
const ASSISTANT_BUBBLE_SELECTOR = '.message.assistant .bubble';
const TOOL_CARD_SELECTOR = '.tool-card';

test.beforeAll(() => {
  mkdirSync('screenshots/demos', { recursive: true });
});

async function sendMessage(page: Page, message: string) {
  const input = page.locator(INPUT_SELECTOR);

  await expect(input).toBeVisible({ timeout: LONG_TIMEOUT });
  await input.fill(message);
  await page.locator(SEND_BUTTON_SELECTOR).click();
}

async function waitForAssistantResponse(page: Page, previousAssistantCount = 0) {
  const bubbles = page.locator(ASSISTANT_BUBBLE_SELECTOR);

  if (previousAssistantCount > 0) {
    await expect
      .poll(async () => bubbles.count(), { timeout: LONG_TIMEOUT })
      .toBeGreaterThan(previousAssistantCount);
  } else {
    await expect(bubbles.first()).toBeVisible({ timeout: LONG_TIMEOUT });
  }

  const streamingButton = page.locator(`${SEND_BUTTON_SELECTOR}.is-streaming`);
  try {
    await streamingButton.waitFor({ state: 'visible', timeout: 10000 });
  } catch {
    // Responses can finish before the spinner is observed.
  }

  try {
    await streamingButton.waitFor({ state: 'hidden', timeout: LONG_TIMEOUT });
  } catch {
    // Fall through to the final bubble assertion below.
  }

  const lastBubble = bubbles.last();
  await expect(lastBubble).toBeVisible({ timeout: LONG_TIMEOUT });
  await expect
    .poll(async () => (await lastBubble.innerText()).trim().length, {
      timeout: LONG_TIMEOUT,
    })
    .toBeGreaterThan(0);

  return lastBubble;
}

test('demo full flow covers recommendation, planning, and backtrack', async ({
  page,
}) => {
  // This intentionally overrides the 5-minute demo config timeout because the
  // spec chains three long LLM interactions in one shared browser session.
  test.setTimeout(LONG_TIMEOUT * 3);

  await page.goto('/');
  await expect(page.locator('text=旅行者')).toBeVisible({ timeout: LONG_TIMEOUT });
  await expect(page.locator(INPUT_SELECTOR)).toBeVisible({ timeout: LONG_TIMEOUT });

  await test.step('Phase 1 vague intent -> destination convergence', async () => {
    const previousAssistantCount = await page
      .locator(ASSISTANT_BUBBLE_SELECTOR)
      .count();

    await sendMessage(
      page,
      '我想找个适合四月去、可以慢慢逛、吃得好一点的亚洲城市，先帮我收敛几个方向。',
    );

    await waitForAssistantResponse(page, previousAssistantCount);

    const toolCard = page.locator(TOOL_CARD_SELECTOR).last();
    await expect(toolCard).toBeVisible({ timeout: LONG_TIMEOUT });
    await expect(toolCard).toContainText(
      /xiaohongshu_search|web_search|quick_travel_search|update_plan_state/,
      { timeout: LONG_TIMEOUT },
    );
    await expect(page.locator('.phase-node.active')).toContainText('灵感与目的地', {
      timeout: LONG_TIMEOUT,
    });

    await page.screenshot({
      path: 'screenshots/demos/phase1-recommendations.png',
      fullPage: true,
    });
  });

  await test.step('Phase 3 confirm destination -> skeleton planning', async () => {
    const previousAssistantCount = await page
      .locator(ASSISTANT_BUBBLE_SELECTOR)
      .count();

    await sendMessage(
      page,
      '京都方向最打动我。那就先按京都做 4 天、两个人、预算 12000 元来规划，并给我 2 套骨架方案，住宿先放在四条河原町附近。',
    );

    await waitForAssistantResponse(page, previousAssistantCount);

    await expect(page.locator('.destination-banner .dest-name')).toContainText('京都', {
      timeout: LONG_TIMEOUT,
    });
    await expect(page.locator('.phase-node.active')).toContainText('日期与住宿', {
      timeout: LONG_TIMEOUT,
    });
    await expect(page.locator('.phase3-workbench')).toBeVisible({
      timeout: LONG_TIMEOUT,
    });
    await expect
      .poll(async () => page.locator('.p3-skeleton').count(), {
        timeout: LONG_TIMEOUT,
      })
      .toBeGreaterThan(0);

    await page.screenshot({
      path: 'screenshots/demos/phase3-planning.png',
      fullPage: true,
    });
  });

  await test.step('Phase 5 backtrack/change preference', async () => {
    const firstSkeletonTitle = (
      await page.locator('.p3-skeleton h4').first().innerText()
    ).trim();

    let previousAssistantCount = await page
      .locator(ASSISTANT_BUBBLE_SELECTOR)
      .count();

    await sendMessage(
      page,
      `我选第一套方案（${firstSkeletonTitle}），就按这个方向继续，住在河原町附近即可。先细化前两天的行程，我想看看节奏。`,
    );

    await waitForAssistantResponse(page, previousAssistantCount);

    await expect(page.locator('.phase-node.active')).toContainText('行程组装', {
      timeout: LONG_TIMEOUT,
    });
    await expect(page.locator('.timeline .day-card').first()).toBeVisible({
      timeout: LONG_TIMEOUT,
    });

    previousAssistantCount = await page.locator(ASSISTANT_BUBBLE_SELECTOR).count();

    await sendMessage(
      page,
      '我改主意了，不想去京都了。请回到目的地选择阶段，换成更安静、适合海边散步和吃海鲜的方向，重新帮我收敛候选地。',
    );

    await waitForAssistantResponse(page, previousAssistantCount);

    await expect(page.locator('.phase-node.active')).toContainText('灵感与目的地', {
      timeout: LONG_TIMEOUT,
    });
    await expect(page.locator('.destination-banner')).toHaveCount(0, {
      timeout: LONG_TIMEOUT,
    });

    await page.screenshot({
      path: 'screenshots/demos/phase5-backtrack.png',
      fullPage: true,
    });
  });
});
