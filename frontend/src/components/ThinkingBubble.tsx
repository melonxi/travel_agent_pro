import { useEffect, useState } from 'react'

interface Props {
  createdAt: number
  stage?: 'thinking' | 'summarizing' | 'compacting'
  iteration?: number
  hint?: string | null
  fading?: boolean
}

const STAGE_LABELS: Record<'thinking' | 'summarizing' | 'compacting', string> = {
  thinking: '思考中…',
  summarizing: '汇总中…',
  compacting: '整理上下文中…',
}

export default function ThinkingBubble({ createdAt, stage = 'thinking', iteration, hint, fading = false }: Props) {
  const [now, setNow] = useState(Date.now())

  useEffect(() => {
    const timer = window.setInterval(() => {
      setNow(Date.now())
    }, 500)

    return () => window.clearInterval(timer)
  }, [])

  const elapsed = now - createdAt
  const label = hint
    ?? (stage === 'thinking' && elapsed >= 2000
      ? '正在连接…'
      : stage === 'thinking' && iteration && iteration >= 1
        ? `继续思考…（第 ${iteration + 1} 轮）`
        : STAGE_LABELS[stage])

  return (
    <div className="message assistant thinking-message" data-testid="thinking-bubble" aria-live="polite">
      <div className={`bubble thinking-bubble${fading ? ' fading' : ''}`}>
        <span className="thinking-bubble-dot" aria-hidden="true" />
        <span>{label}</span>
      </div>
    </div>
  )
}
