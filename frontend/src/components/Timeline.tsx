import type { DayPlan } from '../types/plan'

interface Props {
  dailyPlans: DayPlan[]
}

export default function Timeline({ dailyPlans }: Props) {
  if (dailyPlans.length === 0) {
    return (
      <div className="sidebar-section">
        <div className="section-title">行程</div>
        <div className="timeline-empty">
          <div className="timeline-empty-icon">◫</div>
          行程规划中...
        </div>
      </div>
    )
  }

  return (
    <div className="sidebar-section">
      <div className="section-title">行程</div>
      <div className="timeline">
        {dailyPlans.map((day) => (
          <div key={day.day} className="day-card">
            <div className="day-header">
              <span className="day-label">Day {day.day}</span>
              <span className="day-date">{day.date}</span>
            </div>
            <div className="day-activities">
              {day.activities.map((act, i) => (
                <div key={i} className="activity-row">
                  <span className="activity-time">{act.start_time}–{act.end_time}</span>
                  <span className="activity-dot" />
                  <span className="activity-name">{act.name}</span>
                  {act.cost > 0 && <span className="activity-cost">¥{act.cost}</span>}
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
