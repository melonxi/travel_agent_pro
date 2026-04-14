import type { CandidateItem, PlanRisk, Preference, SkeletonPlan, TravelPlanState } from '../types/plan'

type Props = {
  plan: TravelPlanState
  overrideStep?: string | null
}

type BriefItem = {
  label: string
  value: string
}

const STEP_LABELS: Record<string, string> = {
  brief: '旅行画像',
  candidate: '候选筛选',
  skeleton: '骨架方案',
  lock: '锁定交通与住宿',
}

const BRIEF_KEY_LABELS: Record<string, string> = {
  destination: '目的地',
  dates: '日期',
  total_days: '行程长度',
  duration: '停留时长',
  departure_city: '出发地',
  goal: '旅行目标',
  '旅行目标': '旅行目标',
  pace: '节奏偏好',
  '节奏偏好': '节奏偏好',
  transport_preference: '交通偏好',
  '交通偏好': '交通偏好',
  accommodation_preference: '住宿偏好',
  '住宿偏好': '住宿偏好',
  must_do: '必做事项',
  '必去': '必做事项',
  avoid: '明确避开',
  '不去': '明确避开',
  season_notes: '时令提醒',
  weather: '天气提醒',
  pending_preferences: '还待确认',
}

const BUCKET_LABELS: Record<string, string> = {
  must: '必选',
  must_have: '必选',
  high_potential: '高潜力',
  candidate: '候选',
  optional: '可替代',
  alternative: '可替代',
  avoid: '不建议',
  not_recommended: '不建议',
}

function formatValue(value: unknown): string {
  if (value == null) return '未确认'
  if (typeof value === 'string' || typeof value === 'number') return String(value)
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (Array.isArray(value)) {
    return value
      .map((item) => formatValue(item))
      .filter(Boolean)
      .join(' · ')
  }
  if (typeof value === 'object') {
    const named = value as Record<string, unknown>
    if (typeof named.name === 'string') return named.name
    if (typeof named.title === 'string') return named.title
    if (typeof named.summary === 'string') return named.summary
    if (typeof named.start === 'string' && typeof named.end === 'string') {
      return `${named.start} → ${named.end}`
    }
    if (typeof named.total === 'number' && typeof named.currency === 'string') {
      return `${named.currency} ${named.total.toLocaleString()}`
    }
    if (typeof named.adults === 'number') {
      const children = typeof named.children === 'number' ? named.children : 0
      const bits = [`${named.adults} 位成人`]
      if (children > 0) bits.push(`${children} 位儿童`)
      return bits.join(' · ')
    }
    return Object.entries(named)
      .slice(0, 3)
      .map(([key, item]) => `${key}: ${formatValue(item)}`)
      .join(' · ')
  }
  return String(value)
}

function normalizeTradeoffs(value: SkeletonPlan['tradeoffs']): string[] {
  if (!value) return []
  if (Array.isArray(value)) return value.map((item) => String(item))
  return [String(value)]
}

function summarizePreference(items?: Preference[] | null): string {
  if (!items?.length) return '未写入'
  return items
    .map((item) => (item.value ? `${item.key}: ${item.value}` : item.key))
    .join(' · ')
}

function getCandidateTitle(item: CandidateItem | string, fallback: string): string {
  if (typeof item === 'string') return item
  const summary = typeof item.summary === 'string' ? item.summary : undefined
  return item.name ?? item.title ?? item.area ?? item.theme ?? summary ?? fallback
}

function getCandidateBucket(item: CandidateItem): string | null {
  const raw = item.bucket ?? item.category
  if (!raw) return null
  return BUCKET_LABELS[String(raw)] ?? String(raw)
}

function getRiskTitle(item: PlanRisk | string, fallback: string): string {
  if (typeof item === 'string') return item
  return item.title ?? item.name ?? item.summary ?? fallback
}

function buildBriefItems(plan: TravelPlanState): BriefItem[] {
  const brief = plan.trip_brief ?? {}
  const orderedKeys = [
    'goal',
    '旅行目标',
    'pace',
    '节奏偏好',
    'dates',
    'total_days',
    'duration',
    'budget',
    'travelers',
    'departure_city',
    'transport_preference',
    '交通偏好',
    'accommodation_preference',
    '住宿偏好',
    'must_do',
    '必去',
    'avoid',
    '不去',
    'season_notes',
    'weather',
    'pending_preferences',
  ]

  const seen = new Set<string>()
  const items: BriefItem[] = []

  for (const key of orderedKeys) {
    if (!(key in brief) || seen.has(key)) continue
    seen.add(key)
    const value = brief[key]
    if (value == null) continue
    if (Array.isArray(value) && value.length === 0) continue
    const text = formatValue(value)
    if (!text || text === '未确认') continue
    items.push({
      label: BRIEF_KEY_LABELS[key] ?? key,
      value: text,
    })
  }

  return items
}

function lockSummary(plan: TravelPlanState): BriefItem[] {
  const items: BriefItem[] = []
  if (plan.selected_skeleton_id) {
    items.push({ label: '当前骨架', value: plan.selected_skeleton_id })
  }
  if (plan.accommodation) {
    items.push({
      label: '已确认住宿',
      value: plan.accommodation.hotel ?? plan.accommodation.area,
    })
  } else if (plan.accommodation_options?.length) {
    items.push({
      label: '住宿候选',
      value: `${plan.accommodation_options.length} 个区域/酒店候选`,
    })
  }
  if (plan.selected_transport?.summary) {
    items.push({
      label: '已确认交通',
      value: String(plan.selected_transport.summary),
    })
  } else if (plan.transport_options?.length) {
    items.push({
      label: '交通候选',
      value: `${plan.transport_options.length} 组方案`,
    })
  }
  return items
}

export default function Phase3Workbench({ plan, overrideStep }: Props) {
  const activeStep = overrideStep ?? plan.phase3_step ?? 'brief'
  const shortlist = plan.shortlist ?? []
  const candidatePool = plan.candidate_pool ?? []
  const skeletons = plan.skeleton_plans ?? []
  const risks = plan.risks ?? []
  const alternatives = plan.alternatives ?? []
  const briefItems = buildBriefItems(plan)
  const displayCandidates = shortlist.length ? shortlist : candidatePool
  const lockItems = lockSummary(plan)

  if (
    plan.phase !== 3 &&
    !briefItems.length &&
    !shortlist.length &&
    !candidatePool.length &&
    !skeletons.length &&
    !risks.length &&
    !alternatives.length &&
    !lockItems.length
  ) {
    return null
  }

  return (
    <div className="phase3-workbench">
      <div className="section-title">规划工作台</div>

      <div className="p3-steprail">
        {Object.entries(STEP_LABELS).map(([key, label]) => {
          const index = Object.keys(STEP_LABELS).indexOf(key)
          const activeIndex = Object.keys(STEP_LABELS).indexOf(activeStep)
          const isActive = key === activeStep
          const isDone = index < activeIndex
          return (
            <div
              key={key}
              className={`p3-stepchip ${isActive ? 'active' : ''} ${isDone ? 'done' : ''}`}
            >
              <span className="p3-stepdot">{isDone ? '✓' : index + 1}</span>
              <span>{label}</span>
            </div>
          )
        })}
      </div>

      <div className="p3-grid">
        <section className="p3-card p3-brief">
          <div className="p3-cardhead">
            <span>旅行画像</span>
            <strong>{briefItems.length} 项</strong>
          </div>
          {briefItems.length ? (
            <div className="p3-briefgrid">
              {briefItems.map((item) => (
                <div key={`${item.label}-${item.value}`} className="p3-kv">
                  <div className="p3-k">{item.label}</div>
                  <div className="p3-v">{item.value}</div>
                </div>
              ))}
            </div>
          ) : (
            <div className="p3-empty">用户明确表达的边界条件会在这里沉淀成易读的旅行画像。</div>
          )}
          <div className="p3-footnote">偏好状态：{summarizePreference(plan.preferences)}</div>
        </section>

        <section className="p3-card">
          <div className="p3-cardhead">
            <span>{shortlist.length ? '候选筛选' : '候选池'}</span>
            <strong>{displayCandidates.length} 项</strong>
          </div>
          {displayCandidates.length ? (
            <div className="p3-list">
              {displayCandidates.slice(0, 5).map((item, index) => (
                <article key={`${getCandidateTitle(item, `候选 ${index + 1}`)}-${index}`} className="p3-item">
                  <div className="p3-itemtop">
                    <h4>{getCandidateTitle(item, `候选 ${index + 1}`)}</h4>
                    {typeof item !== 'string' && getCandidateBucket(item) && (
                      <span className="p3-tag">{getCandidateBucket(item)}</span>
                    )}
                  </div>
                  {typeof item === 'string' ? (
                    <p>{item}</p>
                  ) : (
                    <>
                      {(item.why || item.summary) && <p>{String(item.why ?? item.summary)}</p>}
                      <div className="p3-meta">
                        {item.area && <span>{item.area}</span>}
                        {item.theme && <span>{item.theme}</span>}
                        {item.time_cost && <span>{formatValue(item.time_cost)}</span>}
                      </div>
                      {item.why_not && <div className="p3-warning">取舍提醒：{item.why_not}</div>}
                    </>
                  )}
                </article>
              ))}
            </div>
          ) : (
            <div className="p3-empty">还没有形成结构化候选。完成筛选后，这里会显示必选、高潜力、可替代和不建议项。</div>
          )}
        </section>

        <section className="p3-card p3-skeletons">
          <div className="p3-cardhead">
            <span>骨架方案</span>
            <strong>{skeletons.length} 套</strong>
          </div>
          {skeletons.length ? (
            <div className="p3-skeletonlist">
              {skeletons.map((item, index) => {
                const planId = item.id ?? item.name ?? item.title ?? `plan-${index + 1}`
                const isSelected = plan.selected_skeleton_id === item.id || plan.selected_skeleton_id === planId
                const tradeoffs = normalizeTradeoffs(item.tradeoffs)
                return (
                  <article key={planId} className={`p3-skeleton ${isSelected ? 'selected' : ''}`}>
                    <div className="p3-itemtop">
                      <h4>{item.title ?? item.name ?? item.style ?? `方案 ${index + 1}`}</h4>
                      <span className="p3-tag">{isSelected ? '当前选中' : item.style ?? '待比较'}</span>
                    </div>
                    {(item.summary || item.fatigue || item.budget_level) && (
                      <p>{item.summary ?? [item.fatigue, item.budget_level].filter(Boolean).join(' · ')}</p>
                    )}
                    <div className="p3-meta">
                      {item.fatigue && <span>疲劳度 {item.fatigue}</span>}
                      {item.budget_level && <span>预算 {item.budget_level}</span>}
                      {Array.isArray(item.days) && <span>{item.days.length} 天结构</span>}
                    </div>
                    {!!tradeoffs.length && (
                      <div className="p3-tradeoffs">
                        {tradeoffs.slice(0, 3).map((tradeoff, tradeoffIndex) => (
                          <span key={`${tradeoff}-${tradeoffIndex}`}>{tradeoff}</span>
                        ))}
                      </div>
                    )}
                  </article>
                )
              })}
            </div>
          ) : (
            <div className="p3-empty">还没有沉淀成结构化骨架。生成 2-3 套方案后，这里会显示轻松版 / 平衡版 / 高密度版的核心差异。</div>
          )}
        </section>

        <section className="p3-card">
          <div className="p3-cardhead">
            <span>锁定区</span>
            <strong>{lockItems.length || 0} 项</strong>
          </div>
          {lockItems.length ? (
            <div className="p3-lockgrid">
              {lockItems.map((item) => (
                <div key={`${item.label}-${item.value}`} className="p3-lockitem">
                  <div className="p3-k">{item.label}</div>
                  <div className="p3-v">{item.value}</div>
                </div>
              ))}
            </div>
          ) : (
            <div className="p3-empty">选定骨架后，这里会显示交通候选、住宿候选和最终锁定项。</div>
          )}
        </section>

        <section className="p3-card">
          <div className="p3-cardhead">
            <span>风险与备选</span>
            <strong>{risks.length + alternatives.length}</strong>
          </div>
          {risks.length || alternatives.length ? (
            <div className="p3-list">
              {risks.slice(0, 3).map((item, index) => (
                <article key={`${getRiskTitle(item, `风险 ${index + 1}`)}-${index}`} className="p3-item risk">
                  <div className="p3-itemtop">
                    <h4>{getRiskTitle(item, `风险 ${index + 1}`)}</h4>
                    {item.level && <span className="p3-tag">{item.level}</span>}
                  </div>
                  <p>{item.description ?? item.summary ?? '需要留意的执行风险'}</p>
                  {item.mitigation && <div className="p3-warning">缓解方式：{item.mitigation}</div>}
                </article>
              ))}
              {alternatives.slice(0, 2).map((item, index) => (
                <article key={`alternative-${index}`} className="p3-item alt">
                  <div className="p3-itemtop">
                    <h4>{formatValue((item as Record<string, unknown>).title ?? (item as Record<string, unknown>).name ?? `备选 ${index + 1}`)}</h4>
                    <span className="p3-tag">备选</span>
                  </div>
                  <p>{formatValue(item)}</p>
                </article>
              ))}
            </div>
          ) : (
            <div className="p3-empty">锁定交通与住宿时发现的风险、雨天替代和关键备选，会集中显示在这里。</div>
          )}
        </section>
      </div>
    </div>
  )
}
