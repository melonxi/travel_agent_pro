import { useEffect, useRef, useState } from 'react'

const PHASE_STEPS = [
  { phase: 1, label: '灵感与目的地', displayNum: '1' },
  { phase: 3, label: '日期与住宿', displayNum: '2' },
  { phase: 5, label: '行程组装', displayNum: '3' },
  { phase: 7, label: '出发前查漏', displayNum: '4' },
] as const

const ADVANCING_DURATION_MS = 650

interface Props {
  currentPhase: number
  overridePhase?: number | null
}

export function resolveEffectivePhase(currentPhase: number, overridePhase?: number | null) {
  return overridePhase ?? currentPhase
}

export function shouldAnimateAdvance(previousEffectivePhase: number | null, nextEffectivePhase: number) {
  return previousEffectivePhase !== null && previousEffectivePhase !== nextEffectivePhase
}

export default function PhaseIndicator({ currentPhase, overridePhase }: Props) {
  const effectivePhase = resolveEffectivePhase(currentPhase, overridePhase)
  const [advancingPhase, setAdvancingPhase] = useState<number | null>(null)
  const previousEffectivePhaseRef = useRef<number | null>(null)

  useEffect(() => {
    const previousEffectivePhase = previousEffectivePhaseRef.current
    previousEffectivePhaseRef.current = effectivePhase

    if (overridePhase == null || !shouldAnimateAdvance(previousEffectivePhase, effectivePhase)) {
      return
    }

    setAdvancingPhase(effectivePhase)

    const timeoutId = window.setTimeout(() => {
      setAdvancingPhase((phase) => (phase === effectivePhase ? null : phase))
    }, ADVANCING_DURATION_MS)

    return () => {
      window.clearTimeout(timeoutId)
    }
  }, [effectivePhase, overridePhase])

  return (
    <div className="phase-bar">
      {PHASE_STEPS.map((step) => {
        const isActive = step.phase === effectivePhase
        const isCompleted = step.phase < effectivePhase
        const isAdvancing = step.phase === advancingPhase
        return (
          <div
            key={step.phase}
            className={[
              'phase-node',
              isActive ? 'active' : '',
              isCompleted ? 'completed' : '',
              isAdvancing ? 'advancing' : '',
            ].filter(Boolean).join(' ')}
          >
            <span className="phase-num">{isCompleted ? '✓' : step.displayNum}</span>
            <span className="phase-label">{step.label}</span>
          </div>
        )
      })}
    </div>
  )
}
