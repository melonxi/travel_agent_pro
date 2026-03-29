import { test, expect } from '@playwright/test';

test.describe('Travel Agent Pro E2E', () => {
  test('complete travel planning flow', async ({ page }) => {
    // 1. 打开前端
    await page.goto('http://localhost:5174');

    // 2. 等待页面加载
    await expect(page.locator('text=Travel Agent Pro')).toBeVisible({ timeout: 10000 });

    // 3. 输入第一条消息
    const input = page.locator('input[placeholder*="输入"]');
    await expect(input).toBeVisible({ timeout: 5000 });
    await input.fill('我想五一去东京玩5天，预算2万元');

    // 4. 点击发送
    await page.locator('button:has-text("发送")').click();

    // 5. 等待 AI 回复（检查是否出现住宿推荐关键词）
    await expect(page.locator('text=/涩谷|新宿|浅草/i').first()).toBeVisible({ timeout: 60000 });
    console.log('✅ Step 1: Initial response received');
    await page.screenshot({ path: 'screenshots/step1-initial-response.png', fullPage: true });

    // 6. 等待输入框可用（AI 回复完成）
    await page.waitForTimeout(2000);

    // 7. 选择住宿区域
    await input.fill('我选新宿吧');
    await page.locator('button:has-text("发送")').click();

    // 8. 等待住宿确认（检查消息气泡中包含"新宿"）
    await page.waitForTimeout(5000);
    await expect(page.locator('.bubble').last()).toBeVisible({ timeout: 60000 });
    console.log('✅ Step 2: Accommodation selected');
    await page.screenshot({ path: 'screenshots/step2-accommodation.png', fullPage: true });

    // 9. 请求行程安排
    await page.waitForTimeout(2000);
    await input.fill('帮我安排第一天的行程，我想去浅草寺和东京塔');
    await page.locator('button:has-text("发送")').click();

    // 10. 等待行程生成（检查是否出现景点名称）
    await expect(page.locator('text=/浅草寺|东京塔/i').first()).toBeVisible({ timeout: 90000 });
    console.log('✅ Step 3: Itinerary generated');
    await page.screenshot({ path: 'screenshots/step3-itinerary.png', fullPage: true });

    console.log('✅ E2E test completed successfully');
  });
});
