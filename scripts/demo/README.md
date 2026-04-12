# Travel Agent Pro — Demo Recording

一键录制 Travel Agent Pro 的核心规划流程，并在录制前把 demo 记忆种进后端真实会读取的数据目录。

## 前置要求

- Node.js 18+
- 可用的 Python 环境（优先使用 `backend/.venv/bin/python`，否则回退到 `python`）
- backend 和 frontend 已启动（默认 `http://127.0.0.1:8000` / `http://127.0.0.1:5173`）
- 已安装 Playwright Chromium：`npx playwright install chromium`
- 已配置可用的 LLM / tool API keys

## 快速开始

```bash
# 1. 在另一个终端启动服务
scripts/dev.sh

# 2. 运行 demo 录制
scripts/demo/run-all-demos.sh
```

## 脚本会做什么

1. 检查 backend `/health` 和 frontend 首页是否可访问
2. 备份当前 demo 用户目录，并用 `python -m memory.demo_seed --reset-user` 注入一份干净的 demo 记忆
3. 清空本次 demo 的 `screenshots/demos/` 和 `scripts/demo/test-results/`
4. 执行 `scripts/demo/demo-full-flow.spec.ts`
5. 只收集这一次 run 产出的 `.webm` 到 `screenshots/demos/`
6. 脚本退出时恢复原来的 demo 用户数据，避免污染日常本地环境

## Demo 内容

`demo-full-flow.spec.ts` 在一个共享会话里串联三段核心路径：

1. Phase 1：从模糊意图收敛候选目的地
2. Phase 3：确认京都方向后生成旅行骨架
3. Phase 5：接受方案后再回退，验证 backtrack 清理与重新收敛

截图输出：

- `screenshots/demos/phase1-recommendations.png`
- `screenshots/demos/phase3-planning.png`
- `screenshots/demos/phase5-backtrack.png`

录屏输出：

- `screenshots/demos/*.webm`

## Seed Memory

`seed-memory.json` 当前包含：

- 3 条全局偏好：文化体验、精品民宿、不赶路
- 1 条历史旅行 episode：京都 2025-03

前端聊天请求默认不传 `user_id`，后端会落到 `default_user`，所以 demo seed 也固定注入这个用户。`run-all-demos.sh` 会在执行前备份该用户已有数据，录制结束后再恢复。

## 可选环境变量

| 变量 | 默认值 | 作用 |
|---|---|---|
| `BACKEND_URL` | `http://127.0.0.1:8000` | 健康检查地址 |
| `FRONTEND_URL` | `http://127.0.0.1:5173` | 前端首页检查地址 |
| `BACKEND_DATA_DIR` | `backend/data` | demo seed 写入的数据目录 |
| `BACKEND_PYTHON` | 自动探测（`.venv` → `python`） | 用于执行 seed helper 的 Python |

## 常见问题

**服务没启动？**  
先在另一个终端运行 `scripts/dev.sh`。

**seed 运行了但 demo 没体现偏好？**  
确认 backend 是从仓库里的 `backend/` 目录启动的，并且使用的 `data_dir` 与 `BACKEND_DATA_DIR` 一致。

**没找到录屏文件？**  
脚本只会从 `scripts/demo/test-results/` 收集本次 run 生成的 `.webm`；如果 Playwright 运行失败，脚本仍会尝试复制已有视频，然后以失败码退出。
