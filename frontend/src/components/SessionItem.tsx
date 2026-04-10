import type { KeyboardEvent } from 'react'
import type { SessionMeta } from '../types/session'

interface Props {
  session: SessionMeta
  isActive: boolean
  onSelect: (sessionId: string) => void
  onDelete: (sessionId: string) => void
}

const PHASE_LABELS: Record<number, string> = {
  1: '需求收集',
  2: '信息探索',
  3: '方案设计',
  4: '精细规划',
  5: '最终确认',
  7: '已完成',
}

function formatTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) {
    return ''
  }

  const diffMinutes = Math.floor((Date.now() - date.getTime()) / 60000)
  if (diffMinutes < 1) return '刚刚'
  if (diffMinutes < 60) return `${diffMinutes}分钟前`
  const diffHours = Math.floor(diffMinutes / 60)
  if (diffHours < 24) return `${diffHours}小时前`
  return `${date.getMonth() + 1}/${date.getDate()}`
}

export default function SessionItem({
  session,
  isActive,
  onSelect,
  onDelete,
}: Props) {
  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      onSelect(session.session_id)
    }
  }

  return (
    <div
      className={[
        'session-item',
        isActive ? ' is-active' : '',
        session.status === 'archived' ? ' is-archived' : '',
      ].join('')}
      role="button"
      tabIndex={0}
      onClick={() => onSelect(session.session_id)}
      onKeyDown={handleKeyDown}
    >
      <div className="session-item-content">
        <div className="session-item-title">
          {session.status === 'archived' && <span className="session-archive-mark">✓</span>}
          {session.title || '新会话'}
        </div>
        <div className="session-item-meta">
          <span className="session-item-phase">
            {PHASE_LABELS[session.phase] ?? `Phase ${session.phase}`}
          </span>
          <span className="session-item-time">{formatTime(session.updated_at)}</span>
        </div>
      </div>
      <button
        type="button"
        className="session-item-delete"
        onClick={(event) => {
          event.stopPropagation()
          onDelete(session.session_id)
        }}
        aria-label="删除会话"
        title="删除会话"
      >
        &times;
      </button>
    </div>
  )
}
