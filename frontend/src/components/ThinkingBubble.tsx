import { useEffect, useState } from 'react'

interface Props {
  createdAt: number
  stage?: 'thinking' | 'summarizing' | 'compacting'
  iteration?: number
  hint?: string | null
  fading?: boolean
  staleness?: 'normal' | 'minor' | 'waiting'
}

const STAGE_LABELS: Record<'thinking' | 'summarizing' | 'compacting', string> = {
  thinking: '思考中…',
  summarizing: '汇总中…',
  compacting: '整理上下文中…',
}

const HINT_COLLAPSED_KEY = 'thinkingBubble.collapsed'

function readCollapsed(): boolean {
  try { return localStorage.getItem(HINT_COLLAPSED_KEY) === '1' } catch { return false }
}

export default function ThinkingBubble({ createdAt, stage = 'thinking', iteration, hint, fading = false, staleness = 'normal' }: Props) {
  const [now, setNow] = useState(Date.now())
  const [collapsed, setCollapsed] = useState<boolean>(readCollapsed)

  useEffect(() => {
    const timer = window.setInterval(() => {
      setNow(Date.now())
    }, 500)

    return () => window.clearInterval(timer)
  }, [])

  const handleCollapse = () => {
    setCollapsed(true)
    try { localStorage.setItem(HINT_COLLAPSED_KEY, '1') } catch { /* noop */ }
  }

  const elapsed = now - createdAt
  const fallbackLabel =
    stage === 'thinking' && elapsed >= 2000
      ? '正在连接…'
      : stage === 'thinking' && iteration && iteration >= 1
        ? `继续思考…（第 ${iteration + 1} 轮）`
        : STAGE_LABELS[stage]

  const label = (!collapsed && hint) ? hint : fallbackLabel

  return (
    <div className="message assistant thinking-message" data-testid="thinking-bubble" aria-live="polite">
      <div className={`bubble thinking-bubble${fading ? ' fading' : ''}`}>
        <span className="thinking-bubble-dot" aria-hidden="true" />
        <span>{label}</span>
        {hint && !collapsed && (
          <button className="thinking-collapse" onClick={handleCollapse} aria-label="简化提示" type="button">×</button>
        )}
        {staleness === 'minor' && <span className="breath-dot">⋯</span>}
      </div>
    </div>
  )
}
