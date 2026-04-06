# 项目开发问题记录

## 2026-04-04

- **22:02** | `工具` | `codex_apps` 的 MCP startup failed 和 handshake failed 是什么意思？
  - 上下文: 通用
  - 简答: 这表示 `codex_apps` 这个 MCP 服务在初始化握手阶段就无法连通其远端接口，所以客户端没能成功启动，但其他未失败的 MCP 通常仍可继续使用。
