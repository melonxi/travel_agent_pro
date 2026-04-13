import { useEffect, useState, useCallback, useRef } from 'react'
import ChatPanel from './components/ChatPanel'
import PhaseIndicator from './components/PhaseIndicator'
import MapView from './components/MapView'
import Timeline from './components/Timeline'
import BudgetChart from './components/BudgetChart'
import Phase3Workbench from './components/Phase3Workbench'
import SessionSidebar from './components/SessionSidebar'
import TraceViewer from './components/TraceViewer'
import type { TravelPlanState } from './types/plan'
import type { SessionMeta } from './types/session'

function useTheme() {
  const [dark, setDark] = useState(() => {
    const saved = localStorage.getItem('theme')
    return saved ? saved === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches
  })

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light')
    localStorage.setItem('theme', dark ? 'dark' : 'light')
  }, [dark])

  return { dark, toggle: useCallback(() => setDark((d) => !d), []) }
}

function ThemeToggle({ dark, onToggle }: { dark: boolean; onToggle: () => void }) {
  return (
    <button className="theme-toggle" onClick={onToggle} title={dark ? '切换浅色' : '切换深色'}>
      {dark ? (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="5" />
          <path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42" />
        </svg>
      ) : (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
        </svg>
      )}
    </button>
  )
}

/* TODO(human): 设计一个品牌视觉符号组件 — 在加载屏幕和页头中使用的装饰性 SVG 图标 */
function BrandMark() {
  return (
    <svg width="48" height="48" viewBox="0 0 48 48" fill="none" style={{ marginBottom: 20, opacity: 0.6 }}>
      <circle cx="24" cy="24" r="22" stroke="currentColor" strokeWidth="0.5" opacity="0.3" />
      <path d="M24 6 L24 42 M6 24 L42 24" stroke="currentColor" strokeWidth="0.3" opacity="0.2" />
      <circle cx="24" cy="24" r="3" fill="var(--accent-amber)" opacity="0.8" />
    </svg>
  )
}

export default function App() {
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [plan, setPlan] = useState<TravelPlanState | null>(null)
  const [sessionList, setSessionList] = useState<SessionMeta[]>([])
  const [chatKey, setChatKey] = useState(0)
  const [bootstrapping, setBootstrapping] = useState(true)
  const [rightTab, setRightTab] = useState<'plan' | 'trace'>('plan')
  const [traceTrigger, setTraceTrigger] = useState(0)
  const [recalledIds, setRecalledIds] = useState<string[]>([])
  const { dark, toggle: toggleTheme } = useTheme()
  const initializedRef = useRef(false)
  const showPhase3Workbench = Boolean(
    plan && (
      plan.phase === 3 ||
      plan.trip_brief ||
      plan.candidate_pool?.length ||
      plan.shortlist?.length ||
      plan.skeleton_plans?.length ||
      plan.risks?.length ||
      plan.alternatives?.length
    )
  )

  const refreshSessionList = useCallback(async () => {
    try {
      const response = await fetch('/api/sessions')
      if (!response.ok) return []
      const data = (await response.json()) as SessionMeta[]
      setSessionList(data)
      return data
    } catch {
      return []
    }
  }, [])

  const loadPlan = useCallback(async (id: string) => {
    const response = await fetch(`/api/plan/${id}`)
    if (!response.ok) {
      throw new Error(`Failed to load plan for ${id}`)
    }
    return response.json() as Promise<TravelPlanState>
  }, [])

  const openSession = useCallback(async (id: string) => {
    const planData = await loadPlan(id)
    setSessionId(id)
    setPlan(planData)
    setChatKey((value) => value + 1)
  }, [loadPlan])

  const createSession = useCallback(async () => {
    const response = await fetch('/api/sessions', { method: 'POST' })
    if (!response.ok) {
      throw new Error('Failed to create session')
    }

    const data = (await response.json()) as { session_id: string }
    await openSession(data.session_id)
    await refreshSessionList()
  }, [openSession, refreshSessionList])

  const handlePlanUpdate = useCallback((newPlan: TravelPlanState) => {
    setPlan(newPlan)
    setTraceTrigger((n) => n + 1)
    void refreshSessionList()
  }, [refreshSessionList])

  const handleMemoryRecall = useCallback((itemIds: string[]) => {
    setRecalledIds(itemIds)
  }, [])

  const handleNewSession = useCallback(async () => {
    await createSession()
  }, [createSession])

  const handleSelectSession = useCallback(async (id: string) => {
    if (id === sessionId) return
    await openSession(id)
  }, [openSession, sessionId])

  const handleDeleteSession = useCallback(async (id: string) => {
    const response = await fetch(`/api/sessions/${id}`, { method: 'DELETE' })
    if (!response.ok) {
      return
    }

    const remaining = await refreshSessionList()
    if (id !== sessionId) {
      return
    }

    const nextSession = remaining.find((session) => session.status === 'active') ?? remaining[0]
    if (nextSession) {
      await openSession(nextSession.session_id)
      return
    }

    await createSession()
  }, [createSession, openSession, refreshSessionList, sessionId])

  useEffect(() => {
    if (initializedRef.current) return
    initializedRef.current = true

    const bootstrap = async () => {
      try {
        const sessions = await refreshSessionList()

        if (sessions.length === 0) {
          await createSession()
          return
        }

        const activeSession = sessions.find((session) => session.status === 'active') ?? sessions[0]
        await openSession(activeSession.session_id)
      } finally {
        setBootstrapping(false)
      }
    }

    void bootstrap()
  }, [createSession, openSession, refreshSessionList])

  if (bootstrapping || !sessionId) {
    return (
      <div className="loading-screen">
        <BrandMark />
        <div className="loading-title">旅行者</div>
        <div className="loading-subtitle">travel agent pro</div>
        <div className="loading-dots">
          <span /><span /><span />
        </div>
      </div>
    )
  }

  return (
    <div className="app">
      <header className="app-header">
        <div className="brand">
          <span className="brand-name">旅行者</span>
          <span className="brand-tag">travel agent</span>
        </div>
        <div className="header-right">
          {plan && <PhaseIndicator currentPhase={plan.phase} />}
          <ThemeToggle dark={dark} onToggle={toggleTheme} />
          <span className="session-badge">#{sessionId.slice(0, 8)}</span>
        </div>
      </header>
      <div className="app-body">
        <SessionSidebar
          sessions={sessionList}
          activeSessionId={sessionId}
          recalledIds={recalledIds}
          onSelectSession={(id) => {
            void handleSelectSession(id)
          }}
          onNewSession={() => {
            void handleNewSession()
          }}
          onDeleteSession={(id) => {
            void handleDeleteSession(id)
          }}
        />
        <ChatPanel key={chatKey} sessionId={sessionId} onPlanUpdate={handlePlanUpdate} onMemoryRecall={handleMemoryRecall} />
        <div className="right-panel">
          <div className="right-panel-tabs">
            <button
              className={`right-tab ${rightTab === 'plan' ? 'active' : ''}`}
              onClick={() => setRightTab('plan')}
            >
              Plan
            </button>
            <button
              className={`right-tab ${rightTab === 'trace' ? 'active' : ''}`}
              onClick={() => setRightTab('trace')}
            >
              Trace
            </button>
          </div>
          {rightTab === 'plan' ? (
            <>
              {plan && plan.destination && (
                <div className="destination-banner">
                  <div className="dest-label">目的地</div>
                  <div className="dest-name">{plan.destination}</div>
                  {plan.dates && (
                    <div className="dest-dates">{plan.dates.start} → {plan.dates.end}</div>
                  )}
                  <div className="dest-meta">
                    {plan.budget && (
                      <div className="dest-chip">
                        预算 ¥{plan.budget.total.toLocaleString()}
                      </div>
                    )}
                    {plan.accommodation && (
                      <div className="dest-chip">
                        住宿 {plan.accommodation.hotel ?? plan.accommodation.area}
                      </div>
                    )}
                  </div>
                </div>
              )}
              {plan && (
                <>
                  {showPhase3Workbench && (
                    <div className="sidebar-section">
                      <Phase3Workbench plan={plan} />
                    </div>
                  )}
                  <div className="sidebar-section">
                    <BudgetChart plan={plan} />
                  </div>
                  <div className="sidebar-section">
                    <MapView dailyPlans={plan.daily_plans} dark={dark} />
                  </div>
                  <div className="sidebar-section">
                    <Timeline dailyPlans={plan.daily_plans} />
                  </div>
                </>
              )}
            </>
          ) : (
            <TraceViewer sessionId={sessionId} refreshTrigger={traceTrigger} />
          )}
        </div>
      </div>
    </div>
  )
}
