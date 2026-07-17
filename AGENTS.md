# 项目规范

## 项目全局视角

- 不要默认读取完整项目总览。
- 需要项目背景时，先读取 `docs/agent/START_HERE.md`，用两层循环建立鸟瞰视角。
- 再根据 `docs/agent/TASK_ROUTING.md` 的任务路由，只读取当前任务需要的 slice / deep 文档。
- 需要完整文档地图时读取 `docs/agent/INDEX.md`。
- 只有当用户明确要求“完整项目全景 / 全量架构说明 / 通读项目总览”时，才读取 `PROJECT_OVERVIEW.md`。
- 修改架构、数据流、工具、前端、持久化、API、测试或可观测性时，同步更新对应的 `docs/agent/slices/` 或 `docs/agent/deep/` 文档；`PROJECT_OVERVIEW.md` 作为全量参考，不再是 agent 默认入口。

## 截图存放规范

- 所有 Playwright / 调试 / 文档用截图统一存放在项目根目录的 `screenshots/` 下，禁止散落在项目根目录或其他位置。
- 调用 `mcp__playwright__browser_take_screenshot` 等工具时必须显式指定 `filename` 为 `screenshots/<描述性文件名>.png`。
- 临时验证用截图用完即删；需要长期保留的截图（用于文档、PR、issue）才提交到 git。

## 公开源码边界

- 公开仓库以本地 `main` 为权威；不要为「少推一些文件」另造远端无关历史。
- 密钥与本机配置（`.env`、`config.yaml` 等）永不入库；私人材料（学习笔记、面试资料、简历 bullet 等）只留本地，靠 `.gitignore` 排除。
- 完整约定见 `docs/public-source-boundary.md`；增删「本地-only」路径时同步改该文档与 `.gitignore`。
