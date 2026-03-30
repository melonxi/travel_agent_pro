# 项目开发问题记录

## 2026-03-30

- **14:30** | `设计` | 为什么这个项目要用 FastAPI 而不是 Flask？
  - 上下文: 通用 - 框架选型
  - 简答: FastAPI 原生支持异步、自动生成 OpenAPI 文档、内置 Pydantic 数据校验，更适合构建高并发的 AI Agent 后端服务

- **13:10** | `工具` | git worktree 在 main 上有未提交改动时还能不能创建？
  - 上下文: 通用 - Git worktree
  - 简答: 能不能建取决于具体命令和这些未提交改动是否会和目标 worktree 检出产生冲突，常见情况下 `git worktree add <path> <new-branch>` 可以建，但 `git worktree add <path> main` 往往不行，因为同一分支不能同时被两个 worktree 检出
