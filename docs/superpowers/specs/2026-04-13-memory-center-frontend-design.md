# Spec 3: Memory Center 前端

> **目标**：让项目最强差异化点（1,523 行记忆系统）从"只有后端"变为"端到端可展示"，面试可直接 demo。
>
> **隔离边界**：仅新增/修改 `frontend/` 下的文件。后端 API 已全部就绪，无需任何后端改动。唯一改动的现有文件是 `SessionSidebar.tsx`（加一个按钮）。

---

## 1. 背景与动机

记忆系统是项目与其他 portfolio 项目最大的差异化点。后端实现了完整的生命周期：提取 → policy 风险分类 → PII 脱敏 → pending 确认 → 三路检索 → trip_id 隔离 → episode 归档。

但面试官看不到这些。当前前端没有任何记忆相关的 UI。面试追问"用户怎么控制记忆"时无法现场演示。

现有后端 API 全部就绪：

| 端点 | 用途 |
|------|------|
| `GET /api/memory/{user_id}` | 获取所有记忆项 |
| `POST /api/memory/{user_id}/confirm` | 确认 pending 记忆 |
| `POST /api/memory/{user_id}/reject` | 拒绝 pending 记忆 |
| `DELETE /api/memory/{user_id}/{item_id}` | 标记记忆为 obsolete |
| `GET /api/memory/{user_id}/episodes` | 获取旅行 episode |

---

## 2. 交互设计

### 2.1 入口

`SessionSidebar.tsx` 底部新增一个"记忆"按钮（使用 🧠 图标或纯文字"记忆管理"）。点击后打开 Memory Center Drawer。

按钮旁显示 pending 记忆数量的徽章（红色圆点 + 数字），无 pending 时不显示。

### 2.2 Drawer 容器

- 从右侧滑入，宽度 480px（桌面端）或全屏（移动端 < 768px）
- 半透明遮罩层，点击关闭
- 顶部：标题"记忆管理" + 关闭按钮（×）
- ESC 键关闭
- 打开时获取最新数据

### 2.3 Tab 分栏

Drawer 内部按状态分 3 个 Tab：

| Tab | 筛选条件 | 说明 |
|-----|---------|------|
| 活跃 | status=active | 当前正在被检索使用的记忆 |
| 待确认 | status=pending | 需要用户确认的新提取记忆，黄色高亮 |
| 已归档 | status=rejected 或 obsolete | 灰色，默认折叠 |

Tab 右侧显示该状态下的记忆数量。

### 2.4 记忆卡片

每条记忆渲染为一个卡片，内容：

**主体区域**：
- **类别标签**：`category` 字段，作为彩色小标签（如"目的地偏好"、"住宿风格"、"饮食限制"）
- **内容文本**：`content` 字段，主要展示区
- **来源引用**：`source_quote` 字段，灰色斜体小字，前缀"来源："

**元数据行**：
- scope 徽章：`global`（蓝色）或 `trip`（绿色）
- domain 标签：如 destination / hotel / food
- confidence 指示器：高（绿点）/ 中（黄点）/ 低（红点）
- trip_id：仅 trip scope 显示，灰色小字
- 创建时间：相对时间（"2 小时前"、"3 天前"）

**操作区域**：
- Active 记忆：删除按钮（二次确认"确定删除此记忆？"）
- Pending 记忆：确认按钮（绿色）+ 拒绝按钮（红色），操作后乐观更新 UI
- Rejected/Obsolete 记忆：无操作按钮

### 2.5 空状态

无记忆时显示居中文案："暂无记忆数据。与 Agent 对话后，系统会自动提取和保存用户偏好。"

### 2.6 Loading 与 Error

- 加载中：骨架屏（3 个灰色卡片占位）
- 网络错误：红色提示条 + 重试按钮
- 操作失败：toast 提示 + 回滚乐观更新

---

## 3. 技术实现

### 3.1 `frontend/src/types/memory.ts` — 新建

```typescript
export interface MemoryItem {
  id: string;
  category: string;
  content: string;
  source_quote?: string;
  scope: 'global' | 'trip';
  domain?: string;
  confidence?: number;
  status: 'active' | 'pending' | 'rejected' | 'obsolete';
  trip_id?: string;
  created_at: string;
  updated_at?: string;
}

export interface MemoryEpisode {
  trip_id: string;
  destination: string;
  dates: string;
  summary: string;
  created_at: string;
}
```

### 3.2 `frontend/src/hooks/useMemory.ts` — 新建

```typescript
export function useMemory(userId: string) {
  // 状态
  memories: MemoryItem[]
  episodes: MemoryEpisode[]
  loading: boolean
  error: string | null

  // 方法
  fetchMemories(): Promise<void>  // GET /api/memory/{userId}
  confirmMemory(itemId: string): Promise<void>  // POST /api/memory/{userId}/confirm
  rejectMemory(itemId: string): Promise<void>  // POST /api/memory/{userId}/reject
  deleteMemory(itemId: string): Promise<void>  // DELETE /api/memory/{userId}/{itemId}
  fetchEpisodes(): Promise<void>  // GET /api/memory/{userId}/episodes

  // 派生
  pendingCount: number  // pending 状态的记忆数
}
```

userId 默认使用 `"default_user"`（与后端一致）。

乐观更新策略：操作时立即更新本地状态，API 失败时回滚并显示 toast。

### 3.3 `frontend/src/components/MemoryCenter.tsx` — 新建

Props：
```typescript
interface MemoryCenterProps {
  open: boolean;
  onClose: () => void;
}
```

组件结构：
```
<Drawer>                          // 遮罩 + 滑入动画
  <DrawerHeader>                  // 标题 + 关闭按钮
  <TabBar>                        // 活跃 | 待确认 | 已归档
  <MemoryList>                    // 根据 active tab 筛选
    <MemoryCard />                // 单条记忆
    <MemoryCard />
    ...
  </MemoryList>
  <EmptyState />                  // 条件渲染
</Drawer>
```

Drawer 组件内联实现（不引入第三方 UI 库），使用 CSS transition 实现滑入动画。

### 3.4 `frontend/src/styles/memory-center.css` — 新建

遵循现有 Solstice 设计系统：
- 使用已有 CSS 变量：`--glass-bg`, `--glass-border`, `--amber-accent`, `--text-primary`, `--text-secondary`
- Drawer 背景使用 `backdrop-filter: blur()` 玻璃效果
- 卡片样式与现有 Phase3Workbench 的卡片风格一致
- scope/domain/confidence 徽章使用小圆角标签
- pending 卡片左侧边框使用琥珀色高亮

**不修改 `index.css`**。所有样式在独立文件中。

### 3.5 `frontend/src/components/SessionSidebar.tsx` — 修改

改动量：约 5-8 行。

```tsx
// 导入
import MemoryCenter from './MemoryCenter';
import { useMemory } from '../hooks/useMemory';

// 在组件内部
const { pendingCount } = useMemory('default_user');
const [memoryOpen, setMemoryOpen] = useState(false);

// 在 sidebar 底部（现有 JSX 末尾之前）
<button className="memory-btn" onClick={() => setMemoryOpen(true)}>
  🧠 记忆管理
  {pendingCount > 0 && <span className="badge">{pendingCount}</span>}
</button>
<MemoryCenter open={memoryOpen} onClose={() => setMemoryOpen(false)} />
```

---

## 4. 文件清单

| 文件 | 改动类型 | 内容 |
|------|---------|------|
| `frontend/src/types/memory.ts` | 新建 | MemoryItem + MemoryEpisode 类型 |
| `frontend/src/hooks/useMemory.ts` | 新建 | 记忆 API 封装 + 状态管理 |
| `frontend/src/components/MemoryCenter.tsx` | 新建 | Drawer + Tab + MemoryCard |
| `frontend/src/styles/memory-center.css` | 新建 | 独立样式，遵循 Solstice 系统 |
| `frontend/src/components/SessionSidebar.tsx` | 修改 | 底部加入口按钮（5-8 行） |

**不碰的文件**：`App.tsx`、`index.css`、`ChatPanel.tsx`、右侧面板组件、后端所有文件。

---

## 5. 测试策略

### 5.1 手动验证清单

| 场景 | 验证点 |
|------|--------|
| 打开 Drawer | 从右侧滑入，遮罩可见，ESC 可关闭 |
| 加载记忆 | 显示骨架屏 → 加载完成显示卡片 |
| Tab 切换 | 活跃/待确认/已归档正确过滤 |
| 确认 pending | 点击确认 → 卡片移到活跃 Tab，UI 即时响应 |
| 拒绝 pending | 点击拒绝 → 卡片移到已归档 Tab |
| 删除 active | 二次确认弹窗 → 确定后卡片消失 |
| 空状态 | 无记忆时显示引导文案 |
| pending 徽章 | 侧边栏按钮显示 pending 数量 |
| 后端断开 | 显示错误提示 + 重试按钮 |

### 5.2 类型检查

`cd frontend && npx tsc --noEmit` 通过，无新增类型错误。

---

## 6. 验收标准

1. `cd frontend && npm run build` 构建成功
2. Drawer 打开/关闭动画流畅
3. 三个 Tab 正确筛选记忆状态
4. 确认/拒绝/删除操作调用正确 API 端点
5. 乐观更新 UI 响应即时
6. 样式与 Solstice 设计系统一致（暗色玻璃 + 琥珀色）
7. 不引入新的第三方依赖
