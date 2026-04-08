# 项目规范

## 输出约束

- 当前模型存在输出长度限制，每次回复的输出 token 长度必须控制在 8K 以内。内容较多时应精简表达或分多次输出。

## 截图存放规范

- 所有 Playwright / 调试 / 文档用截图统一存放在项目根目录的 `screenshots/` 下，禁止散落在项目根目录或其他位置。
- 调用 `mcp__playwright__browser_take_screenshot` 等工具时必须显式指定 `filename` 为 `screenshots/<描述性文件名>.png`。
- 临时验证用截图用完即删；需要长期保留的截图（用于文档、PR、issue）才提交到 git。
