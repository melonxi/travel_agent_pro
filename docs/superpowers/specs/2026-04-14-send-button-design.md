# 发送按钮修复与适度增强 — 设计文档

> 日期：2026-04-14
> 范围：ChatPanel.tsx 输入栏组件 + index.css 相关样式

---

## 1. 问题清单

| # | 问题 | 严重程度 |
|---|------|----------|
| 1 | `.stop-btn` 无任何 CSS 样式，渲染为无格式按钮 | 高 |
| 2 | `.send-btn.is-streaming` 和 `.send-spinner` 为死代码 CSS | 低 |
| 3 | `sendingRef.current`（ref）与 `streaming`（state）双重状态源 | 中 |
| 4 | SVG 图标无 `aria-label`，停止按钮仅有 `title` 无 `aria-label` | 中 |
| 5 | `.send-btn:disabled` 的 `opacity: 0.2` 几乎不可见，无 tooltip | 中 |
| 6 | 停止按钮无 hover/active 交互反馈 | 中 |
| 7 | 输入框 Enter 发送无文本提示 | 低 |

---

## 2. 方案概述

**选定方案 A：最小修复 + 一致性清理 + 适度增强**

不动输入框类型（保持 `<input>`），不引入多行输入，不新增工具栏位。修复 7 个问题，添加少量微动画增强交互感。

---

## 3. CSS 修复与新增

### 3.1 删除死代码

从 `index.css` 中移除：
- `.send-btn.is-streaming` 块（行 1054-1059）
- `.send-spinner` 块（行 1066-1073）

### 3.2 新增 `.stop-btn` 样式

与 `.send-btn` 对称布局，使用红色调标识"停止"语义：

```css
.stop-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 46px;
  height: 46px;
  border-radius: var(--radius-md);
  border: 1px solid rgba(239, 68, 68, 0.4);
  background: linear-gradient(135deg, rgba(239, 68, 68, 0.12), rgba(239, 68, 68, 0.04));
  color: #f87171;
  cursor: pointer;
  transition: all var(--transition-smooth);
  flex-shrink: 0;
}

.stop-btn:hover {
  background: linear-gradient(135deg, rgba(239, 68, 68, 0.2), rgba(239, 68, 68, 0.08));
  box-shadow: 0 0 12px rgba(239, 68, 68, 0.15);
  transform: translateY(-2px);
}

.stop-btn:active {
  transform: translateY(0) scale(0.96);
}
```

### 3.3 修改 `.send-btn:disabled`

- `opacity: 0.2` → `opacity: 0.35`
- tooltip 提示在 TSX 中通过 `title` 属性处理

---

## 4. 组件逻辑修复

### 4.1 合并双重状态源

移除 `sendingRef`，统一使用 `streaming` state：

| 位置 | 变更 |
|------|------|
| `handleSend` 守卫 | `if (!input.trim() \|\| streaming) return` |
| `handleStop` | 移除 `sendingRef.current = false`，仅 `setStreaming(false)` |
| `handleSend` finally | 移除 `sendingRef.current = false` |
| `handleContinue` 守卫 | `if (streaming) return` |
| `handleContinue` finally | 移除 `sendingRef.current = false` |
| 顶层数据 | 删除 `const sendingRef = useRef(false)` |

### 4.2 停止按钮改为 SVG 图标

替换字符 `"■"` 为 SVG 方块图标：

```tsx
<button type="button" className="stop-btn" onClick={() => void handleStop()} aria-label="停止生成" title="停止生成">
  <svg viewBox="0 0 24 24" fill="currentColor" width="18" height="18">
    <rect x="6" y="6" width="12" height="12" rx="2" />
  </svg>
</button>
```

### 4.3 发送按钮无障碍增强

```tsx
<button
  type="button"
  className="send-btn"
  onClick={() => void handleSend()}
  disabled={!input.trim()}
  aria-label="发送消息"
  title={!input.trim() ? '请输入内容' : '发送'}
>
```

### 4.4 输入框 placeholder 加提示

```
"告诉我你想去哪里…" → "告诉我你想去哪里…（Enter 发送）"
```

### 4.5 停止/发送过渡动画

将条件渲染改为两个按钮同时存在于 DOM，通过 CSS 类控制显隐：

```tsx
<button
  type="button"
  className={`send-btn ${streaming ? 'send-btn--hidden' : ''}`}
  ...
>
<button
  type="button"
  className={`stop-btn ${!streaming ? 'stop-btn--hidden' : ''}`}
  ...
>
```

CSS：
```css
.send-btn--hidden,
.stop-btn--hidden {
  opacity: 0;
  pointer-events: none;
  position: absolute;
  transform: scale(0.8);
}

.send-btn,
.stop-btn {
  transition: opacity 0.15s ease, transform 0.15s ease;
}
```

输入栏容器需加 `position: relative` 以支持绝对定位的隐藏按钮。

---

## 5. Playwright E2E 测试

新建 `e2e-send-button.spec.ts`，mock 后端 SSE 接口（与 demo 方式一致），只依赖前端 dev server。

### 测试用例

| # | 名称 | 验证内容 |
|---|------|----------|
| 1 | 发送按钮初始状态 | 空输入时 disabled，有内容时 enabled |
| 2 | Enter 发送消息 | 输入内容后按 Enter，消息发送、输入框清空、按钮切换为停止按钮 |
| 3 | 停止按钮交互 | 流式中点击停止按钮，流式终止、恢复为发送按钮 |
| 4 | 继续生成按钮 | `canContinue=true` 时显示继续生成按钮 |
| 5 | 禁用态可感知 | disabled 时 opacity ≥ 0.3 |
| 6 | 无障碍属性 | 发送按钮 `aria-label="发送消息"`，停止按钮 `aria-label="停止生成"` |

---

## 6. 不做的事情

- 不改 `<input>` 为 `<textarea>`（多行输入超出范围）
- 不改发送按钮为圆形 Filled 图标（与 Solstice 线框风格不一致）
- 不新增附件/语音工具栏位（YAGNI）
- 不改变现有交互流程（继续生成、连接警告等保持不变）

---

## 7. 影响范围

| 文件 | 变更类型 |
|------|----------|
| `frontend/src/components/ChatPanel.tsx` | 移除 sendingRef，修改按钮渲染逻辑，增强无障碍 |
| `frontend/src/styles/index.css` | 删除死代码，新增 stop-btn 样式，修改 disabled opacity，新增过渡动画类 |
| `e2e-send-button.spec.ts` | 新建 E2E 测试文件 |