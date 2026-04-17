import { test, expect } from '@playwright/test';

/**
 * 回归测试：验证 Phase 1 提示词收紧后，面对模糊旅行意图时，
 * 助手首轮回复不再主动追问 dates / travelers / budget / 节奏 等
 * 非目的地字段（除非候选地在季节/预算维度上强依赖，且本次测试语料
 * 不触发该例外）。
 *
 * 对应改动：
 * - backend/phase/prompts.py PHASE1_PROMPT "阶段边界" & Red Flag
 * - backend/phase/prompts.py GLOBAL_RED_FLAGS 新增硬禁条
 */

// 典型的"纯氛围型"模糊意图——没有任何季节/预算强依赖候选地的理由。
// 在这种输入下，收紧后的提示词应引导模型直接基于"海边 + 放松 + 风景"
// 做目的地推荐，而不是先问"预算""人数""时间"。
const VAGUE_INTENT = '想出去玩，好久没旅行了，帮我看看去哪';

// 违规短语：助手首轮回复若主动追问以下任一话题，即视为 Phase 1 边界越界。
// 采用"短语 + 问号上下文"双重匹配，降低误杀正常叙述的概率。
const OFFTOPIC_PATTERNS: { label: string; pattern: RegExp }[] = [
  { label: '预算', pattern: /预算.*?(多少|是|大概|大约|范围|\?|？)/ },
  { label: '人数', pattern: /(几个人|几位|多少人|同行|几人).{0,20}(去|出行|旅行|\?|？)/ },
  { label: '出行日期', pattern: /(什么时候|几月|哪个月|出发日期|出行日期|打算什么时间).{0,20}(去|出发|出行|\?|？)/ },
  { label: '天数', pattern: /(玩几天|打算几天|几天时间|出行天数).{0,10}(\?|？|)/ },
  { label: '节奏偏好', pattern: /(喜欢.*?节奏|节奏偏好|轻松还是紧凑|慢游还是暴走).{0,10}(\?|？|)/ },
];

test.describe('Phase 1 — 收紧后的模糊意图应答', () => {
  test('首轮回复不应主动追问 dates / travelers / budget / 节奏', async ({ page }) => {
    await page.goto('/');

    const input = page.locator('input[placeholder*="告诉我你想去哪里"]');
    await expect(input).toBeVisible({ timeout: 15000 });

    await input.fill(VAGUE_INTENT);
    await page.locator('.send-btn').click();

    // 等待首轮 assistant 气泡出现且文本不为空
    const lastAssistantBubble = page.locator('.message.assistant .bubble').last();
    await expect(lastAssistantBubble).toBeVisible({ timeout: 120_000 });
    await expect(lastAssistantBubble).not.toHaveText(/^$/, { timeout: 120_000 });

    // 等待流式输出稳定：文本连续 3 秒不再增长视为完成
    let prev = '';
    let stableSinceMs = 0;
    const stablePollIntervalMs = 500;
    const stableRequiredMs = 3000;
    const maxWaitMs = 120_000;
    const started = Date.now();

    while (Date.now() - started < maxWaitMs) {
      const current = (await lastAssistantBubble.innerText()).trim();
      if (current && current === prev) {
        stableSinceMs += stablePollIntervalMs;
        if (stableSinceMs >= stableRequiredMs) break;
      } else {
        stableSinceMs = 0;
        prev = current;
      }
      await page.waitForTimeout(stablePollIntervalMs);
    }

    const finalText = (await lastAssistantBubble.innerText()).trim();
    expect(finalText.length).toBeGreaterThan(0);

    // 可观测性：把模型的首轮回复打到测试日志，便于失败时人工复核
    console.log('\n[Phase1 首轮回复]\n' + finalText + '\n');

    const offenders = OFFTOPIC_PATTERNS.filter(({ pattern }) => pattern.test(finalText));

    if (offenders.length > 0) {
      // 构造可读的失败消息：列出命中的违规话题和 finalText 片段
      const joined = offenders.map((o) => o.label).join('、');
      throw new Error(
        `Phase 1 首轮回复主动追问了非目的地话题：${joined}。\n\n` +
          `--- 命中的首轮回复 ---\n${finalText}\n--- end ---`,
      );
    }
  });
});
