import { useEffect, useState } from 'react'
import SessionItem from './SessionItem'
import MemoryCenter from './MemoryCenter'
import { useMemory } from '../hooks/useMemory'
import type { SessionMeta } from '../types/session'

interface Props {
  sessions: SessionMeta[]
  activeSessionId: string | null
  memoryRefreshTrigger?: number
  onSelectSession: (sessionId: string) => void
  onNewSession: () => void
  onDeleteSession: (sessionId: string) => void
}

export default function SessionSidebar({
  sessions,
  activeSessionId,
  memoryRefreshTrigger = 0,
  onSelectSession,
  onNewSession,
  onDeleteSession,
}: Props) {
  const [collapsed, setCollapsed] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [memoryOpen, setMemoryOpen] = useState(false)
  const memory = useMemory('default_user', activeSessionId, memoryRefreshTrigger)

  const activeSessions = sessions.filter((session) => session.status === 'active')
  const archivedSessions = sessions.filter((session) => session.status === 'archived')

  useEffect(() => {
    if (confirmDelete && !sessions.some((session) => session.session_id === confirmDelete)) {
      setConfirmDelete(null)
    }
  }, [confirmDelete, sessions])

  const handleDelete = (sessionId: string) => {
    if (confirmDelete === sessionId) {
      onDeleteSession(sessionId)
      setConfirmDelete(null)
      return
    }
    setConfirmDelete(sessionId)
  }

  if (collapsed) {
    return (
      <>
        <aside className="session-sidebar is-collapsed" aria-label="会话列表">
          <button
            type="button"
            className="sidebar-toggle"
            onClick={() => setCollapsed(false)}
            title="展开侧边栏"
            aria-label="展开侧边栏"
          >
            &#9654;
          </button>
        </aside>
        <MemoryCenter open={memoryOpen} onClose={() => setMemoryOpen(false)} memory={memory} />
      </>
    )
  }

  return (
    <>
      <aside className="session-sidebar" aria-label="会话列表">
      <div className="sidebar-header">
        <button
          type="button"
          className="sidebar-toggle"
          onClick={() => setCollapsed(true)}
          title="收起侧边栏"
          aria-label="收起侧边栏"
        >
          &#9664;
        </button>
        <button type="button" className="sidebar-new-btn" onClick={onNewSession}>
          + 新对话
        </button>
      </div>

      <div className="sidebar-list">
        {activeSessions.length === 0 && archivedSessions.length === 0 ? (
          <div className="sidebar-empty">暂无会话</div>
        ) : (
          <>
            {activeSessions.map((session) => (
              <SessionItem
                key={session.session_id}
                session={session}
                isActive={session.session_id === activeSessionId}
                onSelect={onSelectSession}
                onDelete={handleDelete}
              />
            ))}
            {archivedSessions.length > 0 && (
              <>
                <div className="sidebar-divider">归档</div>
                {archivedSessions.map((session) => (
                  <SessionItem
                    key={session.session_id}
                    session={session}
                    isActive={session.session_id === activeSessionId}
                    onSelect={onSelectSession}
                    onDelete={handleDelete}
                  />
                ))}
              </>
            )}
          </>
        )}
      </div>

      {confirmDelete && <div className="sidebar-confirm-toast">再次点击确认删除</div>}

        <button
          type="button"
          className="sidebar-memory-btn"
          onClick={() => setMemoryOpen(true)}
        >
          🧠 记忆管理
          {memory.pendingCount > 0 && (
            <span className="memory-pending-badge">{memory.pendingCount}</span>
          )}
        </button>
      </aside>
      <MemoryCenter open={memoryOpen} onClose={() => setMemoryOpen(false)} memory={memory} />
    </>
  )
}
