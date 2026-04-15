import { useEffect, useState } from 'react'

interface Props {
  toolCount: number
  durationMs: number
  memoryCount: number
}

export default function RoundSummaryBar({ toolCount, durationMs, memoryCount }: Props) {
  const [visible, setVisible] = useState(true)
  useEffect(() => {
    const t = setTimeout(() => setVisible(false), 2500)
    return () => clearTimeout(t)
  }, [])
  if (!visible) return null
  return (
    <div className="round-summary-bar" role="status">
      ✓ 本轮已完成 · {toolCount} 个工具 · 用时 {(durationMs / 1000).toFixed(1)}s
      {memoryCount > 0 && ` · 命中 ${memoryCount} 条记忆`}
    </div>
  )
}
