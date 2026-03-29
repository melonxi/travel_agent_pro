import type { DayPlan } from '../types/plan'

interface Props {
  dailyPlans: DayPlan[]
}

export default function Timeline({ dailyPlans }: Props) {
  if (dailyPlans.length === 0) {
    return <div className="timeline-empty">行程规划中...</div>
  }

  return (
    <div className="timeline">
      {dailyPlans.map((day) => (
        <div key={day.day} className="timeline-day">
          <h4>Day {day.day} — {day.date}</h4>
          {day.activities.map((act, i) => (
            <div key={i} className="timeline-item">
              <span className="time">{act.start_time}-{act.end_time}</span>
              <span className="name">{act.name}</span>
              {act.cost > 0 && <span className="cost">¥{act.cost}</span>}
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}
