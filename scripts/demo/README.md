# Travel Agent Pro — Demo Recording

一键录制 Travel Agent Pro 的核心规划流程。当前录制采用 **deterministic scripted playback**：前端可见的 Phase 1 → Phase 3 → Phase 5 → backtrack 流程由固定 fixture 回放，不依赖后端或实时 LLM 输出稳定性。

## 前置要求

- Node.js 18+
- frontend 已启动（默认 `http://127.0.0.1:5173`）
- 已安装 Playwright Chromium：`npx playwright install chromium`

## 快速开始

```bash
# 1. 在另一个终端启动前端
cd frontend && npm run dev

# 2. 运行 demo 录制
scripts/demo/run-all-demos.sh
```

## 脚本会做什么

1. 检查 frontend 首页是否可访问
2. 清空本次 demo 的 `screenshots/demos/`
3. 执行 `scripts/demo/demo-full-flow.spec.ts`，mock `/api/sessions`、`/api/plan`、`/api/messages`、`/api/chat`，稳定回放脚本化对话
4. 将录屏 `.webm` 与 3 张截图写入 `screenshots/demos/`

## Demo 内容

`demo-full-flow.spec.ts` 读取 `demo-scripted-session.json`，在一个共享会话里串联四段核心路径：

1. Phase 1：从模糊意图收敛候选目的地
2. Phase 3：确认京都方向后生成旅行骨架
3. Phase 3 → 5：**显式选择住宿候选**，锁定住宿并进入日程组装
4. Phase 5 → 1：回退并重新收敛海边候选，验证 backtrack 清理

截图输出：

- `screenshots/demos/phase1-recommendations.png`
- `screenshots/demos/phase3-planning.png`
- `screenshots/demos/phase5-backtrack-change-preference.png`

录屏输出：

- `screenshots/demos/*.webm`

## Scripted Fixture

- `demo-scripted-session.json` 定义会话元数据、每一轮用户输入、工具卡片、SSE `state_update` 和最终 plan 快照
- `demo-full-flow.spec.ts` 在内存里推进 fixture 状态，并把成功 run 的视频直接保存为 `screenshots/demos/demo-full-flow.webm`
- 这意味着 demo 录制结果稳定可复现，不会再因为模型临场波动卡在 Phase 3

## Seed Memory

`seed-memory.json` 当前包含：

- 3 条全局偏好：文化体验、精品民宿、不赶路
- 1 条历史旅行 episode：京都 2025-03

这份 seed 数据保留给手动 live demo 使用。当前 scripted recording 会 mock API，不读取后端 memory，也不会修改 `backend/data/`。

## 可选环境变量

| 变量 | 默认值 | 作用 |
|---|---|---|
| `FRONTEND_URL` | `http://127.0.0.1:5173` | 前端首页检查地址 |

## 常见问题

**服务没启动？**  
先在另一个终端运行 `cd frontend && npm run dev`。

**seed 运行了但 demo 没体现偏好？**  
当前 scripted recording 不使用后端 seed。要验证 memory 行为，请运行 live 后端并手动使用 `backend/memory/demo_seed.py`。

**没找到录屏文件？**  
成功 run 会直接把 `demo-full-flow.webm` 写到 `screenshots/demos/`；如果 Playwright 失败，脚本仍会保留已生成的截图/视频并以失败码退出。
