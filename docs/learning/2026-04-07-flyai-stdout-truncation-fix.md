# FlyAI CLI stdout 截断问题排查与修复

**日期**：2026-04-07  
**影响范围**：Phase 3 中 `search_flights`、`search_trains`、`search_accommodations` 等所有依赖 FlyAI CLI 的工具  
**现象**：工具调用失败，前端显示「No flight results from any source」  

---

## 1. 问题现象

在 Phase 3 的端到端对话中，用户请求查航班/火车/酒店时，`search_flights` 工具卡片显示**失败**，错误信息为：

```
No flight results from any source (Amadeus API key not configured)
Check API keys, install FlyAI CLI, or try different dates/airports
```

Amadeus 未配置 API key 是预期的，但 FlyAI 分支也静默返回了空结果，导致两路数据源全部为空。

## 2. 排查过程

### 第一层：CLI 和工具函数单元测试 — 全部正常

| 测试项 | 结果 |
|--------|------|
| `flyai search-flight --origin 北京 --destination 大阪 ...` | 返回 13KB JSON，10 条航班 |
| `flyai search-train --origin 北京 --destination 上海 ...` | 返回 9.5KB JSON，10 条车次 |
| `flyai search-hotel --dest-name 京都 ...` | 返回 4KB JSON，10 家酒店 |
| Python `FlyAIClient.search_flight()` | 返回 10 条 |
| Python `make_search_flights_tool()()` | 返回 9 条（normalize + merge 后） |

单独调用全部正常，问题仅在 **uvicorn 服务端环境** 中出现。

### 第二层：Playwright E2E 联测 — 复现失败

通过 Playwright 启动前后端，发送「我要去京都，从北京出发，4月20日到4月25日」触发 Phase 3 工具调用。  
前端页面上 `search_flights` 显示红色失败状态。

### 第三层：后端日志定位 — 发现 JSON 截断

在 `flyai_client.py` 添加 debug 日志后，后端输出：

```
FlyAI CLI [search-flight] returncode=0 stdout=7478 bytes stderr=0 bytes
FlyAI CLI invalid JSON: Expecting property name enclosed in double quotes:
  line 1 column 7479 (char 7478)
  (stdout_len=7478, last_100_chars='..."depStationName":"羽田机场",')
```

关键发现：
- `returncode=0`：Node.js 进程正常退出
- `stdout=7478 bytes`：实际应为 ~12000 bytes，**丢失了 38% 的数据**
- `stderr=0 bytes`：无错误输出
- JSON 在第 7478 字符处被截断，解析失败

### 第四层：排除并发因素

编写 asyncio 并发测试脚本，同时调用 4 个 FlyAI 命令：

```python
await asyncio.gather(
    run_flyai('flight1', [...]),
    run_flyai('flight2', [...]),
    run_flyai('hotel', [...]),
    run_flyai('train', [...]),
)
```

结果：**全部成功**。说明问题不在 asyncio 并发本身，而是 uvicorn 环境特有的。

## 3. 根因分析

### Node.js stdout pipe 异步 flush 问题

这是一个 Node.js 的已知行为：

1. 当 Node.js 的 stdout 连接到 **pipe**（而非 TTY 或文件）时，`process.stdout.write()` 是**异步**的
2. Node.js 进程在 `write()` 的回调完成之前就可以退出
3. 此时内核关闭 pipe 的写端，**用户态缓冲区中尚未 flush 的数据丢失**
4. 读端（Python 的 `communicate()`）收到 EOF，返回不完整的数据

### 为什么 uvicorn 环境下更容易触发？

uvicorn 的事件循环同时处理大量任务（HTTP 请求、OpenTelemetry trace export 重试、SSE 流推送等），event loop 的调度延迟增大。当 Python 的 `communicate()` 调度不够及时时，Node.js 端写入 pipe 的数据无法及时被消费，缓冲区压力增大，加剧了 flush 不完整的概率。

### 截断位置的规律

多次测试中，截断位置稳定在 **7478 bytes**（接近 8KB）。这与 Node.js 内部 stream 的 `highWaterMark`（默认 16KB）和操作系统 pipe 缓冲区调度有关。

## 4. 修复方案

### 修复 1：改用临时文件捕获 stdout（核心修复）

**文件**：`backend/tools/flyai_client.py`

**原逻辑**：
```python
proc = await asyncio.create_subprocess_exec(
    *cmd,
    stdout=asyncio.subprocess.PIPE,  # Node.js 异步写入 pipe，可能截断
    stderr=asyncio.subprocess.PIPE,
)
stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
data = json.loads(stdout.decode())
```

**新逻辑**：
```python
fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="flyai_")
os.close(fd)
shell_cmd = " ".join(shlex.quote(c) for c in cmd) + f" > {shlex.quote(tmp_path)}"

proc = await asyncio.create_subprocess_shell(
    shell_cmd,
    stdout=asyncio.subprocess.DEVNULL,
    stderr=asyncio.subprocess.PIPE,
)
_, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)

with open(tmp_path, "r", encoding="utf-8") as f:
    raw_output = f.read()
data = json.loads(raw_output)
```

**原理**：Shell 的 `>` 重定向在 fork 之后、exec 之前设置 fd 1 指向文件。Node.js 的 stdout 实际上直接写入文件系统，由内核保证 `write()` 系统调用的完整性。即使进程退出，已经通过系统调用写入的数据不会丢失。

### 修复 2：修复价格字段名（附带修复）

**文件**：`backend/tools/normalizers.py`

FlyAI CLI 新版（v1.0.14）的航班和火车数据中，价格字段名从 `adultPrice` 改为了 `ticketPrice`（航班）和 `price`（火车），但 normalizer 代码未跟进。

```python
# 航班 — 修复前
price = _safe_float(raw.get("adultPrice") or raw.get("price"))
# 航班 — 修复后
price = _safe_float(raw.get("adultPrice") or raw.get("ticketPrice") or raw.get("price"))

# 火车 — 修复前
price = _safe_float(raw.get("adultPrice"))
# 火车 — 修复后
price = _safe_float(raw.get("adultPrice") or raw.get("price") or raw.get("ticketPrice"))
```

## 5. 验证结果

修复后通过 Playwright E2E 测试：

| 工具 | 修复前 | 修复后 |
|------|--------|--------|
| `search_flights`（去程） | 失败：JSON 截断 | 成功：HU473 ¥3,286、CA161 ¥3,180 等 |
| `search_flights`（返程） | 失败：JSON 截断 | 成功：HU474 ¥7,257、CA162 ¥8,771 等 |
| `search_accommodations` | 静默空结果 | 成功：Richmond Hotel ¥700、FRESA INN ¥652 等 |
| `xiaohongshu_search` | 正常 | 正常 |
| `web_search` | 正常 | 正常 |

## 6. 经验总结

1. **Node.js CLI 作为子进程时，不要依赖 pipe 捕获大量输出**。Node.js 对 pipe 的写入是异步的，进程退出时可能丢数据。用临时文件或 shell 重定向是更可靠的方案。

2. **单元测试通过 ≠ 集成环境正常**。本次问题仅在 uvicorn（高负载事件循环）下出现，独立 asyncio 脚本无法复现。排查此类问题需要在真实环境做 E2E 测试。

3. **CLI 工具版本升级后，注意检查输出字段名的变化**。FlyAI CLI 的 `ticketPrice` vs `adultPrice` 变更未被及时发现，导致价格数据静默丢失（显示为 None）。

4. **静默降级（graceful degradation）有双面性**。`FlyAIClient._run()` 捕获所有异常并返回空列表，虽然不会崩溃，但也掩盖了问题。建议在 warning 日志中输出更多上下文信息以便排查。
