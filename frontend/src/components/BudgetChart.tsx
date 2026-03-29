import React from 'react'
import type { TravelPlanState } from '../types/plan'

interface Props {
  plan: TravelPlanState
}

export default function BudgetChart({ plan }: Props) {
  if (!plan.budget) return null

  const spent = plan.daily_plans.reduce(
    (sum, d) => sum + d.activities.reduce((s, a) => s + a.cost, 0),
    0,
  )
  const pct = Math.min((spent / plan.budget.total) * 100, 100)

  return (
    <div className="budget-chart">
      <div className="budget-header">
        <span>预算</span>
        <span>¥{spent} / ¥{plan.budget.total}</span>
      </div>
      <div className="budget-bar">
        <div className="budget-fill" style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}
