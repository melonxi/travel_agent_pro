import { test, expect } from '@playwright/test';

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

    const toolCard = page.locator('.tool-card').filter({ hasText: 'search_destinations' });
    await expect(toolCard).toBeVisible({ timeout: 90000 });
    await expect(toolCard.locator('.tool-status')).toHaveText(/成功|执行中/, {
      timeout: 90000,
    });

    const toggle = toolCard.getByRole('button', { name: /详情/ });
    await toggle.click();
    await expect(toolCard.locator('.tool-section')).toBeVisible({ timeout: 15000 });

    const lastAssistantBubble = page.locator('.message.assistant .bubble').last();
    await expect(lastAssistantBubble).toBeVisible({ timeout: 90000 });
    await expect(lastAssistantBubble).not.toHaveText(/^$/, { timeout: 90000 });
  });
});
