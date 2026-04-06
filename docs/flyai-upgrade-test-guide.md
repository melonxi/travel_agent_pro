# FlyAI 升级与测试说明

## 1. 本次升级范围

本次已将后端对 FlyAI CLI 的接入升级到新版 `flyai-skill` / `@fly-ai/flyai-cli` 命令体系，并完成以下改动：

### 1.1 FlyAIClient 升级

- `fliggy-fast-search` → `keyword-search`
- `search-hotels` → `search-hotel`
- 新增 `search-train`
- 新增 `ai-search`
- 保留 `search_hotels()` 兼容别名，内部转发到 `search_hotel()`

### 1.2 已适配的新输出字段

- **航班**
  - `adultPrice`
  - `marketingTransportName`
  - `marketingTransportNo`
  - `depDateTime`
  - `arrDateTime`
  - `seatClassName`
- **酒店**
  - `name`
  - `latitude`
  - `longitude`
  - `star`
- **POI**
  - 优先读取 `name`，兼容旧 `title`

### 1.3 新增工具

- `search_trains`
- `ai_travel_search`

### 1.4 受影响的现有工具

- `search_flights`
- `search_accommodations`
- `get_poi_info`
- `quick_travel_search`
- `search_travel_services`

其中：

- `quick_travel_search` / `search_travel_services` 现在底层走 `keyword-search`
- `search_accommodations` 的 FlyAI 分支现在对齐 `search-hotel`

---

## 2. 安装与准备

### 2.1 安装 flyai CLI

```bash
npm i -g @fly-ai/flyai-cli
```

### 2.2 验证安装

```bash
flyai --help
flyai keyword-search --query "杭州三日游"
```

如需增强结果，可配置 key：

```bash
flyai config set FLYAI_API_KEY "your-key"
```

---

## 3. 项目侧验证命令

进入项目后端：

```bash
cd backend
source .venv/bin/activate
```

运行与本次升级直接相关的测试：

```bash
python -m pytest \
  tests/test_flyai_client.py \
  tests/test_tool_fusion.py \
  tests/test_search_flights.py \
  tests/test_flyai_new_tools.py \
  tests/test_search_trains.py \
  tests/test_ai_travel_search.py -q
```

预期：**22 个测试全部通过**。

---

## 4. 手动测试清单

## 4.1 quick_travel_search

目标：确认底层已切到 `keyword-search`。

建议输入：

- “杭州三日游”
- “法国签证”
- “上海邮轮”

预期：

- 返回混合旅行产品
- 包含标题、价格、预订链接、图片链接
- `source` 为 `flyai`

---

## 4.2 search_travel_services

目标：确认服务类搜索通过 `keyword-search` 正常工作。

建议测试：

- `destination=日本, service_type=visa`
- `destination=泰国, service_type=insurance`
- `destination=香港, service_type=sim_card`

预期：

- 返回服务列表
- 查询词应分别拼成 “日本 签证办理”“泰国 旅行保险”“香港 境外电话卡”

---

## 4.3 search_accommodations

目标：确认底层已切到 `search-hotel`。

建议测试：

- 目的地：东京
- 日期：`2026-07-15` ~ `2026-07-20`
- 可带预算：`500`

预期：

- 返回住宿列表
- FlyAI 结果可解析 `name / price / detailUrl / latitude / longitude / star`
- Google + FlyAI 融合仍可工作

---

## 4.4 search_flights

目标：确认新版 `search-flight` 字段已被正确标准化。

建议测试：

- `origin=PEK`
- `destination=NRT`
- `date=2026-07-15`

预期：

- 可正确解析为统一字段：
  - `airline`
  - `flight_no`
  - `dep_time`
  - `arr_time`
  - `duration_min`
  - `price`
  - `cabin_class`
  - `booking_url`

---

## 4.5 get_poi_info

目标：确认新版 `search-poi` 结果兼容 `name` 字段。

建议测试：

- `query=伏见稻荷`
- `location=京都`

预期：

- 正常返回景点列表
- FlyAI POI 可正确读取 `name`
- Google + FlyAI 融合不报错

---

## 4.6 search_trains

目标：验证新增火车搜索工具。

建议测试：

- `origin=北京`
- `destination=上海`
- `date=2026-04-15`

可选过滤：

- `seat_class=second class`
- `journey_type=1`
- `sort_type=3`
- `max_price=600`

预期：

- 返回 `trains` 数组
- 每条数据包含：
  - `train_no`
  - `origin`
  - `origin_station`
  - `destination`
  - `destination_station`
  - `dep_time`
  - `arr_time`
  - `duration_min`
  - `price`
  - `seat_class`
  - `booking_url`

---

## 4.7 ai_travel_search

目标：验证新增 AI 语义旅行搜索工具。

建议测试：

- “五一去杭州玩三天，预算人均2000，想住西湖附近”
- “下周从上海去东京，优先直飞，帮我找性价比高的航班和酒店”

预期：

- 返回 `answer`
- `source = flyai_ai_search`
- 能处理复杂自然语言，而不是仅关键词匹配

---

## 5. 建议的 CLI 原生命令回归

如果要排查是项目接入问题还是 FlyAI CLI 本身问题，先直接跑原生命令：

```bash
flyai keyword-search --query "杭州三日游"
flyai ai-search --query "五一去杭州玩三天，预算人均2000，想住西湖附近"
flyai search-flight --origin "北京" --destination "东京" --dep-date 2026-07-15
flyai search-hotel --dest-name "东京" --check-in-date 2026-07-15 --check-out-date 2026-07-20
flyai search-poi --city-name "京都" --keyword "伏见稻荷"
flyai search-train --origin "北京" --destination "上海" --dep-date 2026-04-15
```

如果这些命令都正常，而项目工具异常，优先检查：

- `backend/tools/flyai_client.py`
- `backend/tools/normalizers.py`
- 新增工具注册是否已在 `backend/main.py` 生效

---

## 6. 已知说明

本次升级直接相关测试已通过，但仓库里仍存在**与本次改动无关**的历史失败项，跑全量测试时可能看到：

- `tests/test_e2e_golden_path.py::test_golden_path_tokyo_trip`
- `tests/test_phase_integration.py::test_phase1_destination_search`
- `tests/test_xiaohongshu_search.py::test_xiaohongshu_search_tool_registration`

因此，验证 FlyAI 升级时，优先使用第 3 节中的定向测试命令。
