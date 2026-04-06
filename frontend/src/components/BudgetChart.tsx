import type { TravelPlanState } from '../types/plan'

interface Props {
  plan: TravelPlanState
}

export default function BudgetChart({ plan }: Props) {
  if (!plan.budget) return null
  const budgetTotal = plan.budget.total

  const spent = plan.daily_plans.reduce(
    (sum, d) => sum + d.activities.reduce((s, a) => s + a.cost, 0),
    0,
  )
  const pct = Math.min((spent / budgetTotal) * 100, 100)

  const budgetStatus =
    pct < 50 ? 'comfortable' :
    pct < 80 ? 'moderate' :
    pct < 100 ? 'tight' : 'over'

  const statusLabels: Record<string, string> = {
    comfortable: '充裕',
    moderate: '适中',
    tight: '紧张',
    over: '超支',
  }

  // Per-day breakdown
  const dailyCosts = plan.daily_plans.map((d) => ({
    day: d.day,
    cost: d.activities.reduce((s, a) => s + a.cost, 0),
  }))
  const maxDayCost = Math.max(...dailyCosts.map((d) => d.cost), 1)

  return (
    <div className="budget-card">
      <div className="budget-row">
        <div>
          <div className="budget-label">已使用</div>
          <div className="budget-amount">
            <span className="currency">¥</span>{spent.toLocaleString()}
          </div>
        </div>
        <div style={{ textAlign: 'right' }}>
          <div className="budget-label">总预算</div>
          <div className="budget-amount">
            <span className="currency">¥</span>{budgetTotal.toLocaleString()}
          </div>
        </div>
      </div>
      <div className="budget-track">
        <div className="budget-fill" style={{ width: `${pct}%` }} />
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 8 }}>
        <span style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>
          {pct.toFixed(0)}% 已使用
        </span>
        <span style={{
          fontSize: '0.7rem',
          color: budgetStatus === 'comfortable' ? 'var(--accent-sage)' :
                 budgetStatus === 'over' ? 'var(--accent-terracotta)' :
                 'var(--text-muted)',
        }}>
          {statusLabels[budgetStatus]}
        </span>
      </div>
      {dailyCosts.length > 0 && (
        <div className="daily-breakdown">
          {dailyCosts.map((d) => (
            <div key={d.day} className="daily-bar-row">
              <span className="daily-bar-label">D{d.day}</span>
              <div className="daily-bar-track">
                <div
                  className="daily-bar-fill"
                  style={{
                    width: `${(d.cost / maxDayCost) * 100}%`,
                    background: d.cost / budgetTotal > 0.4
                      ? 'var(--accent-terracotta)'
                      : 'var(--accent-gold)',
                  }}
                />
              </div>
              <span className="daily-bar-cost">¥{d.cost}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
