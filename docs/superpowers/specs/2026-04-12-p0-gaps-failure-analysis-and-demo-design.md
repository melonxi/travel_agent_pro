# P0 Gaps: Failure Analysis & Reproducible Demo — Design Spec (v2)

> **目的**：补齐竞品分析报告 v2 中 P0 的两个缺失项 —— 7.1 失败案例分析和 7.7 可复现 Demo。
>
> **背景**：P0 的 12 个实施任务（guardrail 强化、eval pipeline、cost tracking 等）已全部完成（73/73 测试通过）。但 v2 报告中标注为"面试 ROI 最高"的失败案例分析和可复现 Demo 两项未纳入实施计划。
>
> **v2 变更**：基于 rubber-duck 审查，修正了 8 个设计问题（memory 注入路径、demo 会话策略、eval 框架复用等）。

---

## 1. 失败案例分析 (7.1)

### 1.1 目标

产出 `docs/learning/2026-04-13-失败案例分析.md`，包含 8 个真实场景的系统性失败模式分析。面试时能回答："你的系统什么时候会失败？你怎么知道的？"

### 1.2 架构 — 复用现有 eval 框架

> **设计决策**：不新建 `scripts/failure-analysis/` 独立管道。改为扩展 `backend/evals/` 现有框架，将 8 个失败场景作为 golden cases 加入，复用已有的断言评估体系。

```
backend/evals/
├── golden_cases/
│   ├── (existing 15 cases...)
│   ├── failure-001-tight-budget.yaml      # 新增 8 个失败分析场景
│   ├── failure-002-elderly-altitude.yaml
│   ├── ...
│   └── failure-008-greedy-itinerary.yaml
├── models.py                               # 已有，无需修改
├── runner.py                               # 已有，无需修改
└── failure_report.py                       # 新增：从 eval 结果生成 failure-analysis.md

scripts/failure-analysis/
├── run_and_analyze.py           # 主脚本：调用 evals runner → 截图 → 生成文档
└── capture_screenshots.ts       # Playwright 截图（访问同 session_id 的前端页面）

产出 → docs/learning/2026-04-13-失败案例分析.md
     → screenshots/failure-analysis/
```

### 1.3 执行流程

1. **`run_and_analyze.py`**：
   - 加载 `failure-*.yaml` golden cases
   - 通过 HTTP 调用后端 API 执行（`POST /api/sessions` → `POST /api/chat/{session_id}`，传 `user_id` 参数）
   - 执行后调用 eval runner 的断言评估（复用 `evaluate_assertion()`）
   - 记录运行元数据：model、provider、timestamp、config hash
   - 输出结构化结果 JSON

2. **`capture_screenshots.ts`**：
   - 使用步骤 1 产出的 **同一 session_id**，在前端打开对应会话页面截图
   - 不是重放，而是查看已完成的会话（避免 LLM 响应不一致）
   - 截图存入 `screenshots/failure-analysis/`

3. **`failure_report.py`**：
   - 读取结构化结果 + 断言通过/失败信息
   - 自动生成 `docs/learning/2026-04-13-失败案例分析.md` 骨架
   - 人工补充：根因分析（指向代码行）、修复建议、面试话术

### 1.4 场景清单

| # | 场景 | 用户输入 | 维度 | 预期观察点 |
|---|------|---------|------|-----------|
| 1 | 预算极紧 | "5天3000元日本自由行" | 预算约束 | 预算约束是否在 Phase 3 lock 前生效；feasibility gate 是否触发 |
| 2 | 特殊人群 | "带80岁老人去九寨沟" | 特殊需求 | 是否考虑高海拔风险、无障碍设施、医疗条件 |
| 3 | 不可解任务 | "500元去马尔代夫住5星酒店7天" | 不可行性检测 | feasibility gate 是否拦截并给出合理理由 |
| 4 | 多轮变更 | "先东京再京都" → "我改主意了，京都改成大阪" | 回退 | backtrack 是否正确清理下游状态 |
| 5 | 约束组合 | "3人春节去三亚，一个素食者" | 多约束 | 饮食约束是否进入行程、人数是否影响预算 |
| 6 | 极端时间 | "我明天就要飞纽约" | 日期约束 | guardrail 过去日期检测 + 紧迫时间处理 |
| 7 | 模糊意图 | "想去那个最近很火的地方玩一下" | 意图理解 | 目的地收敛能力、是否能引导澄清 |
| 8 | 贪心行程 | "东京-大阪-京都-奈良-神户，5天全部玩遍" | 时间/地理冲突 | 是否识别行程过于紧凑、validator 时间冲突检测 |

### 1.5 文档模板

`docs/learning/2026-04-13-失败案例分析.md` 结构：

```markdown
# Travel Agent Pro 失败案例分析

## 方法论
- 测试环境：生产配置（GPT-4o + Claude Sonnet 4）
- 测试方式：真实 API 调用，非 mock
- 测试时间：2026-04-12
- 运行元数据：model, provider, config hash 均记录在案

## 失败模式分类法
| 失败类别 | 含义 | 示例 |
|---------|------|------|
| LLM 推理 | 模型理解/推理能力不足 | 无法识别特殊人群需求 |
| 工具数据 | 外部 API 返回数据不足或异常 | 无航班搜索结果 |
| 状态机 | 阶段转换/回退逻辑缺陷 | backtrack 未清理下游 |
| 约束传递 | 用户约束未被传递到下游决策 | 饮食约束未进入行程 |
| 设计边界 | 系统设计本身的合理限制 | 不支持多人差异化行程 |

## 场景总览
| # | 场景 | 结果 | 失败类别 | 关键发现 |
|---|------|------|---------|---------|

## 详细分析

### 场景 1: {标题}
**输入**: 用户消息原文
**预期行为**: ...
**实际行为**: ... (含截图/工具调用记录)
**结果**: ✅ 成功 / ⚠️ 部分成功 / ❌ 失败
**失败类别**: LLM推理 / 工具数据 / 状态机 / 约束传递 / 设计边界
**为什么在这里失败**: 设计限制 vs 实现缺陷 vs 外部依赖
**根因分析**: ... (指向代码位置，如 `agent/loop.py:560`)
**修复状态**: 已修复 / 待修复 / 设计权衡
**面试话术**: 一句话描述这个案例的工程价值

## 失败模式归类
(按类别统计分布，展示系统边界认知)

## 改进路线图
(基于分析结果的后续优化方向)
```

### 1.6 成功标准

- 8 个场景全部执行完毕并有完整记录
- 每个场景包含：实际 trace + 根因分析 + 修复建议
- 至少 3 个场景有前端截图
- 文档可直接用于面试展示

---

## 2. 可复现 Demo (7.7)

### 2.1 目标

让任何人（含面试官）可以一键启动 demo，看到系统的核心能力。提供 3 条录屏视频 + seed data + 执行脚本。

### 2.2 架构

```
scripts/demo/
├── seed-memory.json             # 预设用户偏好和历史旅行记忆
├── playwright.config.ts         # Demo 专用 Playwright 配置（启用录屏）
├── demo-full-flow.spec.ts       # 单脚本三步骤：Phase 1 → 3 → 5+回退
├── run-all-demos.sh             # 一键执行：启动服务 → 注入 seed → 运行 demo → 收集视频
└── README.md                    # Demo 使用指南

产出视频 → screenshots/demos/*.webm
```

### 2.3 Seed Memory

> **关键设计决策**：前端 `useSSE.ts` 调用 `/api/chat/{session_id}` 时只传 `{ message }`，无 `user_id` 字段，后端 `ChatRequest.user_id` 默认为 `"default_user"`。因此 seed memory 必须注入到 `default_user`，而非自定义 user_id。

```json
{
  "user_id": "default_user",
  "preferences": {
    "travel_style": "文化体验为主，适度冒险",
    "accommodation": "偏好精品民宿和设计酒店",
    "dietary": "无特殊要求",
    "budget_sensitivity": "中等，追求性价比",
    "pace": "不赶路，每天2-3个景点"
  },
  "past_trips": [
    {
      "destination": "京都",
      "date": "2025-03",
      "rating": 5,
      "highlight": "岚山竹林和抹茶体验",
      "lesson": "樱花季酒店需提前3个月预订"
    }
  ]
}
```

### 2.4 三条 Demo 路径

> **会话策略**：三个 Demo 阶段在**同一个 `test()` 内使用 `test.step()`** 串联，共享同一 page/context/session。这确保了 Phase 状态连续性和录屏连贯性。不再声称"可独立运行"。

#### Step 1: Phase 1 — 模糊意图 → 目的地收敛

- **用户输入**: "我想找个安静的海边城市放松一下，预算1万左右，大概5天"
- **展示重点**: 工具调用（web_search / xiaohongshu_search）→ 多目的地推荐 → 用户选择确认
- **预期展示**: Agent 如何从模糊意图收敛到具体目的地
- **截图时机**: 工具调用展开面板、推荐结果列表

#### Step 2: Phase 3 — 框架规划 + 骨架选择

- **前置**: 同一会话，Phase 1 完成后
- **用户输入**: "就去这个吧"（确认目的地）
- **展示重点**: search_flights + search_accommodations → 旅行骨架生成 → update_plan_state
- **预期展示**: 多工具并行调用、骨架锁定流程
- **截图时机**: 航班搜索结果、住宿搜索结果、骨架摘要

#### Step 3: Phase 5 — 日程详排 + 用户中途回退

- **前置**: 同一会话，Phase 3 骨架已锁定
- **用户输入**:
  1. 等待 Phase 5 日程生成
  2. "我改主意了，不住市中心了，想住海边的民宿"
- **展示重点**: backtrack 机制 → 下游状态清理 → 重新搜索住宿 → 日程调整
- **预期展示**: 系统的自我修复和回退能力
- **截图时机**: 回退前后的状态对比

### 2.5 Playwright 录屏配置

```typescript
// scripts/demo/playwright.config.ts
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: '.',
  testMatch: 'demo-*.spec.ts',
  timeout: 300000,  // 5 分钟 — LLM 响应需要时间
  use: {
    baseURL: 'http://127.0.0.1:5173',
    video: { mode: 'on', size: { width: 1280, height: 720 } },
    screenshot: 'on',
  },
  projects: [
    {
      name: 'demo',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
```

### 2.6 Seed Memory 注入方式

后端提供完整的 Memory CRUD API：
- `POST /api/memory/{user_id}/events` — 写入记忆事件
- `GET /api/memory/{user_id}` — 读取记忆
- `POST /api/memory/{user_id}/confirm` — 确认记忆项

注入流程：`run-all-demos.sh` 在启动服务后、运行 demo 前，通过 `curl` 调用 `/api/memory/demo-user/events` 写入 seed memory（用户偏好、历史旅行），然后调用 confirm 端点将其标记为 active。

### 2.7 一键执行脚本

`run-all-demos.sh`:
1. 检查后端/前端是否运行，未运行则启动（复用 `scripts/dev.sh`）
2. 通过 Memory API 注入 seed memory 数据
3. 运行 demo spec（串行，共享会话）
4. 收集视频到 `screenshots/demos/`
5. 输出执行摘要

### 2.8 Demo README

`scripts/demo/README.md` 包含：
- 前置要求（Node.js, Python, API keys）
- 一键运行命令
- 各 demo 的预期效果描述
- 视频文件位置
- 常见问题排查

### 2.9 成功标准

- 单脚本 demo 可正常执行（含 3 个 `test.step()`）
- 产出完整 .webm 录屏视频
- seed memory 正确注入到 `default_user`
- README 完整可用
- `run-all-demos.sh` 一键执行成功

---

## 3. 依赖关系

```
7.1 和 7.7 可并行开发，无相互依赖。
两者都依赖后端+前端服务正常运行。
7.1 的部分场景（如场景 3 不可解任务）可复用 7.7 的 seed data 机制。
```

## 4. 文件变更清单

| 操作 | 文件 |
|------|------|
| 新建 | `backend/evals/golden_cases/failure-001-tight-budget.yaml` — `failure-008-greedy-itinerary.yaml` (8 files) |
| 新建 | `backend/evals/failure_report.py` |
| 新建 | `scripts/failure-analysis/run_and_analyze.py` |
| 新建 | `scripts/failure-analysis/capture_screenshots.ts` |
| 新建 | `docs/learning/2026-04-13-失败案例分析.md` |
| 新建 | `scripts/demo/seed-memory.json` |
| 新建 | `scripts/demo/playwright.config.ts` |
| 新建 | `scripts/demo/demo-full-flow.spec.ts` |
| 新建 | `scripts/demo/run-all-demos.sh` |
| 新建 | `scripts/demo/README.md` |
| 修改 | `README.md` — 添加 Demo 和失败分析入口链接 |
| 修改 | `PROJECT_OVERVIEW.md` — 更新文档结构 |
