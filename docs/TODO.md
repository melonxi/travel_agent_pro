# TODO

## 1. tool-self-repair

### 背景

当前 agent 系统已经具备将工具报错作为 `tool_result` 返回给模型的能力，但对“模型传了不受支持的工具参数”这类错误，仍然缺少足够强的自纠错支持。

已出现的实际案例：

- 工具：`xiaohongshu_search`
- 场景：模型调用 `search_notes`
- 输入包含：`max_results`
- 结果：由于工具函数签名未接收该参数，触发 Python `TypeError`
- 当前落到 agent 的错误类型：`INTERNAL_ERROR`

这类错误虽然会被回传给模型，但错误语义不够明确，模型未必能稳定完成下一轮自我修正。

### 待办项

- 在 `ToolEngine` 中识别类似 `unexpected keyword argument` 的异常
- 不要统一归类成 `INTERNAL_ERROR`
- 改为更明确的可恢复错误，例如：`INVALID_INPUT` / `UNSUPPORTED_PARAMETER`
- 在错误结果里附带不被支持的参数名
- 在 `suggestion` 中返回该工具允许的参数列表
- 最好明确到 operation 级别，例如 `xiaohongshu_search.search_notes` 支持哪些字段
- 评估是否需要在真正调用 Python 工具函数前，先根据工具 schema 做一次参数白名单校验
- 扫描其他工具的 schema 与 Python 函数签名是否一致，重点关注搜索类工具

### 目标

让 agent 在工具调用失败时，不只是“把错误返回给模型”，而是能够以更高概率驱动模型完成自我纠错并继续执行。
