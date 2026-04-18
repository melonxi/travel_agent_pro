import type { Deliverables } from '../types/plan'

type DeliverablesCardProps = {
  sessionId: string
  deliverables: Deliverables
}

const LINKS = [
  {
    key: 'travel_plan_md',
    label: '旅行计划',
    description: '完整行程 Markdown',
  },
  {
    key: 'checklist_md',
    label: '出发清单',
    description: '出发前准备 Markdown',
  },
] as const

export default function DeliverablesCard({ sessionId, deliverables }: DeliverablesCardProps) {
  return (
    <div className="deliverables-card">
      <div className="deliverables-title">交付文档</div>
      <div className="deliverables-meta">
        生成于 {new Date(deliverables.generated_at).toLocaleString('zh-CN')}
      </div>
      <div className="deliverables-list">
        {LINKS.map((item) => {
          const filename = deliverables[item.key]
          const href = `/api/sessions/${sessionId}/deliverables/${encodeURIComponent(filename)}`
          return (
            <a
              key={item.key}
              className="deliverable-link"
              href={href}
              download={filename}
              target="_blank"
              rel="noreferrer"
            >
              <span>{item.label}</span>
              <small>{item.description}</small>
            </a>
          )
        })}
      </div>
    </div>
  )
}
