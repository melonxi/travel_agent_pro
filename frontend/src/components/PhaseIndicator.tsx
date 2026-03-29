import React from 'react'

const PHASE_LABELS: Record<number, string> = {
  1: '灵感探索',
  2: '目的地选择',
  3: '天数与节奏',
  4: '住宿区域',
  5: '行程组装',
  7: '出发前查漏',
}

interface Props {
  currentPhase: number
}

export default function PhaseIndicator({ currentPhase }: Props) {
  const phases = [1, 2, 3, 4, 5, 7]
  return (
    <div className="phase-indicator">
      {phases.map((p) => (
        <div
          key={p}
          className={`phase-step ${p === currentPhase ? 'active' : ''} ${p < currentPhase ? 'done' : ''}`}
        >
          <span className="phase-num">{p}</span>
          <span className="phase-label">{PHASE_LABELS[p]}</span>
        </div>
      ))}
    </div>
  )
}
