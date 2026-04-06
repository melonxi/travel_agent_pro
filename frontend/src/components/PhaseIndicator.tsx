const PHASE_STEPS = [
  { phase: 1, label: '灵感与目的地', displayNum: '1' },
  { phase: 3, label: '日期与住宿', displayNum: '2' },
  { phase: 5, label: '行程组装', displayNum: '3' },
  { phase: 7, label: '出发前查漏', displayNum: '4' },
] as const

interface Props {
  currentPhase: number
}

export default function PhaseIndicator({ currentPhase }: Props) {
  return (
    <div className="phase-bar">
      {PHASE_STEPS.map((step) => {
        const isActive = step.phase === currentPhase
        const isCompleted = step.phase < currentPhase
        return (
          <div
            key={step.phase}
            className={`phase-node ${isActive ? 'active' : ''} ${isCompleted ? 'completed' : ''}`}
          >
            <span className="phase-num">{isCompleted ? '✓' : step.displayNum}</span>
            <span className="phase-label">{step.label}</span>
          </div>
        )
      })}
    </div>
  )
}
