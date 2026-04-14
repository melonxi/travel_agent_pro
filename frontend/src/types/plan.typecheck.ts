import type {
  AgentStatusEvent,
  PhaseTransitionEvent,
  SSEEvent,
} from './plan'

const phaseTransitionEvent: PhaseTransitionEvent = {
  type: 'phase_transition',
  from_phase: 3,
  to_phase: 3,
  from_step: 'brief',
  to_step: 'skeleton',
  reason: 'phase3_step_change',
}

const agentStatusEvent: AgentStatusEvent = {
  type: 'agent_status',
  stage: 'thinking',
  iteration: 2,
  hint: 'analyzing options',
}

const readNewSSEEventFields = (event: SSEEvent) => {
  if (event.type === 'phase_transition') {
    return [
      event.from_phase,
      event.to_phase,
      event.from_step,
      event.to_step,
      event.reason,
    ] as const
  }

  if (event.type === 'agent_status') {
    return [event.stage, event.iteration, event.hint] as const
  }

  return null
}

export const sseEventTypecheck = [
  readNewSSEEventFields(phaseTransitionEvent),
  readNewSSEEventFields(agentStatusEvent),
]
