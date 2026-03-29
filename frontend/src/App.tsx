import React, { useEffect, useState } from 'react'
import ChatPanel from './components/ChatPanel'
import PhaseIndicator from './components/PhaseIndicator'
import MapView from './components/MapView'
import Timeline from './components/Timeline'
import BudgetChart from './components/BudgetChart'
import type { TravelPlanState } from './types/plan'

export default function App() {
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [plan, setPlan] = useState<TravelPlanState | null>(null)

  useEffect(() => {
    fetch('/api/sessions', { method: 'POST' })
      .then((r) => r.json())
      .then((data) => {
        setSessionId(data.session_id)
        setPlan({ session_id: data.session_id, phase: 1, destination: null, dates: null, budget: null, accommodation: null, daily_plans: [], backtrack_history: [] })
      })
  }, [])

  if (!sessionId) return <div className="loading">初始化中...</div>

  return (
    <div className="app">
      <header className="app-header">
        <h1>Travel Agent Pro</h1>
        {plan && <PhaseIndicator currentPhase={plan.phase} />}
      </header>
      <div className="app-body">
        <div className="left-panel">
          <ChatPanel sessionId={sessionId} onPlanUpdate={setPlan} />
        </div>
        <div className="right-panel">
          {plan && (
            <>
              <BudgetChart plan={plan} />
              <MapView dailyPlans={plan.daily_plans} />
              <Timeline dailyPlans={plan.daily_plans} />
            </>
          )}
        </div>
      </div>
    </div>
  )
}
