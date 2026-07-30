import { useEffect, useState, useCallback, useRef } from 'react'
import ChatPanel from './components/ChatPanel'
import PhaseIndicator from './components/PhaseIndicator'
import MapView from './components/MapView'
import Timeline from './components/Timeline'
import BudgetChart from './components/BudgetChart'
import DeliverablesCard from './components/DeliverablesCard'
import Phase2Workbench from './components/Phase2Workbench'
import SessionSidebar from './components/SessionSidebar'
import TraceViewer from './components/TraceViewer'
import MemoryTracePanel from './components/MemoryTracePanel'
import type { PhaseTransitionEvent, TravelPlanState } from './types/plan'
import type { SessionMeta } from './types/session'

type PhaseOverride = {
  phase: number
  step?: string | null
  expiresAt: number
} | null

type UiTheme = 'light' | 'dark'

function useUiAppearance() {
  const [theme, setTheme] = useState<UiTheme>(() => {
    const saved = localStorage.getItem('ui-theme')
    if (saved === 'light' || saved === 'dark') return saved
    const legacy = localStorage.getItem('theme')
    if (legacy === 'light' || legacy === 'dark') return legacy
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  })

  useEffect(() => {
    // Product shell is always craft-paper; no public shell switcher.
    document.documentElement.setAttribute('data-shell', 'craft-paper')
    document.documentElement.setAttribute('data-theme', theme)
    localStorage.setItem('ui-theme', theme)
    localStorage.removeItem('ui-shell')
  }, [theme])

  const toggleTheme = useCallback(() => setTheme((t) => (t === 'dark' ? 'light' : 'dark')), [])

  return {
    theme,
    dark: theme === 'dark',
    toggleTheme,
  }
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

/* 阿导卡通头像 Brand Logo（导游帽） */
function BrandMark({ size = 48, className = "" }: { size?: number; className?: string }) {
  return (
    <img
      src="/logo.png"
      width={size}
      height={size}
      alt="阿导"
      className={`brand-mark ${className}`.trim()}
      style={{ width: size, height: size }}
      draggable={false}
    />
  )
}

export default function App() {
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [plan, setPlan] = useState<TravelPlanState | null>(null)
  const [phaseOverride, setPhaseOverride] = useState<PhaseOverride>(null)
  const [sessionList, setSessionList] = useState<SessionMeta[]>([])
  const [chatKey, setChatKey] = useState(0)
  const [bootstrapping, setBootstrapping] = useState(true)
  const [rightTab, setRightTab] = useState<'plan' | 'trace' | 'memory'>('plan')
  const [traceTrigger, setTraceTrigger] = useState(0)
  const [memoryRefreshTrigger, setMemoryRefreshTrigger] = useState(0)
  const { dark, toggleTheme } = useUiAppearance()
  const initializedRef = useRef(false)
  const showPhase2Workbench = Boolean(
    plan && (
      plan.phase === 2 ||
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
    setPhaseOverride(null)
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

  const handlePhaseTransition = useCallback((event: PhaseTransitionEvent) => {
    if (event.from_phase > event.to_phase) {
      setPhaseOverride(null)
      return
    }

    setPhaseOverride({
      phase: event.to_phase,
      step: event.to_step,
      expiresAt: Date.now() + 800,
    })
  }, [])

  const handleMemoryRecall = useCallback((_itemIds: string[]) => {
    // Recall highlighting moved to the Memory tab (MemoryTracePanel reads from trace).
    // Kept as a no-op to preserve the ChatPanel event contract.
  }, [])

  useEffect(() => {
    // Reset any session-scoped UI when switching sessions.
  }, [sessionId])

  const handleStreamEnd = useCallback(() => {
    setTraceTrigger((n) => n + 1)
    setMemoryRefreshTrigger((n) => n + 1)
    if (!sessionId) return
    void loadPlan(sessionId)
      .then((latestPlan) => {
        setPlan(latestPlan)
        void refreshSessionList()
      })
      .catch(() => {})
  }, [loadPlan, refreshSessionList, sessionId])

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

  useEffect(() => {
    if (!plan || !phaseOverride) return
    if (plan.phase !== phaseOverride.phase) return

    const currentStep = plan.phase === 2 ? plan.phase2_step ?? null : null
    const overrideStep = phaseOverride.step ?? null
    if (overrideStep === null || currentStep === overrideStep) {
      setPhaseOverride(null)
    }
  }, [phaseOverride, plan])

  useEffect(() => {
    if (!phaseOverride) return

    const remainingMs = phaseOverride.expiresAt - Date.now()
    if (remainingMs <= 0) {
      setPhaseOverride(null)
      return
    }

    const timeoutId = window.setTimeout(() => {
      setPhaseOverride((currentOverride) => (
        currentOverride?.expiresAt === phaseOverride.expiresAt ? null : currentOverride
      ))
    }, remainingMs)

    return () => {
      window.clearTimeout(timeoutId)
    }
  }, [phaseOverride])

  if (bootstrapping || !sessionId) {
    return (
      <div className="loading-screen">
        <BrandMark />
        <div className="loading-title">阿导</div>
        <div className="loading-subtitle">guida</div>
        <div className="loading-dots">
          <span /><span /><span />
        </div>
      </div>
    )
  }

  return (
    <div className="app">
      <div className="app-body">
        <SessionSidebar
          sessions={sessionList}
          activeSessionId={sessionId}
          memoryRefreshTrigger={memoryRefreshTrigger}
          brandSlot={
            <div className="inbox-brand">
              <BrandMark size={28} />
              <div>
                <div className="inbox-brand-name">阿导</div>
                <div className="inbox-brand-tag">travel workspace</div>
              </div>
            </div>
          }
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
        <ChatPanel
          key={chatKey}
          sessionId={sessionId}
          onPlanUpdate={handlePlanUpdate}
          onMemoryRecall={handleMemoryRecall}
          onPhaseTransition={handlePhaseTransition}
          onStreamEnd={handleStreamEnd}
          documentTitle={plan?.destination ? `${plan.destination}规划` : '新行程'}
          phaseSlot={plan ? <PhaseIndicator currentPhase={plan.phase} overridePhase={phaseOverride?.phase ?? null} /> : null}
          headerActions={<ThemeToggle dark={dark} onToggle={toggleTheme} />}
        />
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
            <button
              className={`right-tab ${rightTab === 'memory' ? 'active' : ''}`}
              onClick={() => setRightTab('memory')}
            >
              Memory
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
              {plan?.deliverables && (
                <div className="sidebar-section">
                  <DeliverablesCard
                    sessionId={sessionId}
                    deliverables={plan.deliverables}
                  />
                </div>
              )}
              {plan && (
                <>
                  {showPhase2Workbench && (
                    <div className="sidebar-section">
                      <Phase2Workbench plan={plan} overrideStep={phaseOverride?.step} />
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
          ) : rightTab === 'trace' ? (
            <TraceViewer sessionId={sessionId} refreshTrigger={traceTrigger} />
          ) : (
            <MemoryTracePanel sessionId={sessionId} refreshTrigger={traceTrigger} />
          )}
        </div>
      </div>
    </div>
  )
}
