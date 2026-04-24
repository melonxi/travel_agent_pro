import { useMemo, useState } from 'react'
import { useTrace } from '../hooks/useTrace'
import { useMemory } from '../hooks/useMemory'
import type { TraceIteration, TraceToolCall, MemoryRecallTelemetry } from '../types/trace'
import type {
  MemoryProfileItem,
  WorkingMemoryItem,
  EpisodeSlice,
  UseMemoryReturn,
} from '../types/memory'
import '../styles/memory-trace.css'

interface Props {
  sessionId: string | null
  refreshTrigger?: number
}

type HitKind = 'profile' | 'working' | 'slice'

interface ResolvedHit {
  kind: HitKind
  id: string
  title: string
  subtitle?: string
  reason?: string
  score?: number | null
}

const EXTRACTION_TOOL_HINTS = ['extract', 'extraction', 'decide_memory']

function iterateLatestWithRecall(iters: TraceIteration[] | undefined): TraceIteration | null {
  if (!iters) return null
  for (let i = iters.length - 1; i >= 0; i--) {
    if (iters[i].memory_recall) return iters[i]
  }
  return null
}

function findLatestExtraction(
  iters: TraceIteration[] | undefined,
): { iter: TraceIteration; tool: TraceToolCall } | null {
  if (!iters) return null
  for (let i = iters.length - 1; i >= 0; i--) {
    const iter = iters[i]
    for (const tool of iter.tool_calls) {
      const name = tool.name.toLowerCase()
      if (EXTRACTION_TOOL_HINTS.some((h) => name.includes(h))) {
        return { iter, tool }
      }
    }
  }
  return null
}

function profileLookup(memory: UseMemoryReturn): Map<string, MemoryProfileItem> {
  const map = new Map<string, MemoryProfileItem>()
  const all = [
    ...memory.profileBuckets.constraints,
    ...memory.profileBuckets.rejections,
    ...memory.profileBuckets.stable_preferences,
    ...memory.profileBuckets.preference_hypotheses,
  ]
  for (const item of all) map.set(item.id, item)
  return map
}

function workingLookup(memory: UseMemoryReturn): Map<string, WorkingMemoryItem> {
  const map = new Map<string, WorkingMemoryItem>()
  for (const item of memory.workingMemory.items) map.set(item.id, item)
  return map
}

function sliceLookup(memory: UseMemoryReturn): Map<string, EpisodeSlice> {
  const map = new Map<string, EpisodeSlice>()
  for (const slice of memory.episodeSlices) map.set(slice.id, slice)
  return map
}

function formatValue(value: unknown): string {
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (value && typeof value === 'object') return JSON.stringify(value)
  return '—'
}

function pickRerankScore(scores: Record<string, number | string | null> | undefined): number | null {
  if (!scores) return null
  const ranking = scores.ranking ?? scores.rerank_score ?? scores.score ?? scores.final
  if (typeof ranking === 'number') return ranking
  return null
}

function resolveRecallHits(
  recall: MemoryRecallTelemetry,
  memory: UseMemoryReturn,
): ResolvedHit[] {
  const hits: ResolvedHit[] = []
  const profiles = profileLookup(memory)
  const workings = workingLookup(memory)
  const slices = sliceLookup(memory)
  const reasons = recall.reranker_per_item_reason ?? {}
  const scores = recall.reranker_per_item_scores ?? {}

  for (const id of recall.profile_ids ?? []) {
    const item = profiles.get(id)
    hits.push({
      kind: 'profile',
      id,
      title: item ? formatValue(item.value) : `profile:${id.slice(0, 8)}`,
      subtitle: item?.domain,
      reason: reasons[id],
      score: pickRerankScore(scores[id]),
    })
  }
  for (const id of recall.working_memory_ids ?? []) {
    const item = workings.get(id)
    hits.push({
      kind: 'working',
      id,
      title: item ? item.content : `working:${id.slice(0, 8)}`,
      subtitle: item?.domains?.join(' · '),
      reason: reasons[id],
      score: pickRerankScore(scores[id]),
    })
  }
  for (const id of recall.slice_ids ?? []) {
    const item = slices.get(id)
    hits.push({
      kind: 'slice',
      id,
      title: item ? item.content : `slice:${id.slice(0, 8)}`,
      subtitle: item?.slice_type,
      reason: reasons[id],
      score: pickRerankScore(scores[id]),
    })
  }

  return hits
}

function kindLabel(kind: HitKind): string {
  switch (kind) {
    case 'profile':
      return '长期画像'
    case 'working':
      return '会话工作记忆'
    case 'slice':
      return '历史切片'
  }
}

function decisionLabel(decision: string | undefined): string {
  if (!decision) return '未知'
  const map: Record<string, string> = {
    skip: '已跳过',
    skipped: '已跳过',
    recall: '已召回',
    executed: '已召回',
    no_hit: '无命中',
    forced: '强制召回',
    fallback: '回退',
  }
  return map[decision.toLowerCase()] ?? decision
}

function EmptyState({ text }: { text: string }) {
  return (
    <div className="mt-empty">
      <div className="mt-empty-icon">·</div>
      <div className="mt-empty-text">{text}</div>
    </div>
  )
}

function SummaryBar({ recall, iteration }: { recall: MemoryRecallTelemetry; iteration: TraceIteration }) {
  const selectedCount = recall.reranker_selected_ids?.length ?? 0
  const candidateCount = recall.candidate_count ?? 0
  const decision = decisionLabel(recall.final_recall_decision)
  const intent = recall.reranker_intent_label
  const fallback = recall.reranker_fallback && recall.reranker_fallback !== 'none' ? recall.reranker_fallback : null

  return (
    <div className="mt-summary">
      <div className="mt-summary-top">
        <span className={`mt-summary-decision ${selectedCount > 0 ? 'is-hit' : 'is-miss'}`}>
          {decision}
        </span>
        <span className="mt-summary-iter">iter #{iteration.index}</span>
      </div>
      <div className="mt-summary-stats">
        <span><b>{selectedCount}</b> 条入上下文</span>
        <span className="mt-dim">候选 {candidateCount}</span>
        {fallback && <span className="mt-summary-fallback">fallback: {fallback}</span>}
      </div>
      {intent && (
        <div className="mt-summary-intent">
          <span className="mt-dim">意图：</span>
          {intent}
        </div>
      )}
      {recall.gate_reason && (
        <div className="mt-summary-reason" title={recall.gate_reason}>
          {recall.gate_reason}
        </div>
      )}
    </div>
  )
}

function HitCard({ hit }: { hit: ResolvedHit }) {
  const [expanded, setExpanded] = useState(false)
  const hasDetail = Boolean(hit.reason || hit.score !== null)
  return (
    <div className="mt-hit">
      <div className="mt-hit-head" onClick={() => hasDetail && setExpanded((v) => !v)}>
        <span className={`mt-hit-kind kind-${hit.kind}`}>{kindLabel(hit.kind)}</span>
        <span className="mt-hit-title">{hit.title}</span>
        {hit.score !== null && hit.score !== undefined && (
          <span className="mt-hit-score" title="rerank 分">{hit.score.toFixed(2)}</span>
        )}
      </div>
      {hit.subtitle && <div className="mt-hit-sub">{hit.subtitle}</div>}
      {expanded && hit.reason && (
        <div className="mt-hit-reason">{hit.reason}</div>
      )}
    </div>
  )
}

function HitsSection({ hits }: { hits: ResolvedHit[] }) {
  if (hits.length === 0) {
    return <EmptyState text="本轮未将任何记忆写入上下文" />
  }
  const groups: Record<HitKind, ResolvedHit[]> = { profile: [], working: [], slice: [] }
  for (const hit of hits) groups[hit.kind].push(hit)

  return (
    <div className="mt-hits">
      {(['profile', 'working', 'slice'] as HitKind[]).map((kind) =>
        groups[kind].length > 0 ? (
          <div key={kind} className="mt-hits-group">
            <div className="mt-hits-group-head">
              <span>{kindLabel(kind)}</span>
              <span className="mt-dim">{groups[kind].length}</span>
            </div>
            {groups[kind].map((hit) => (
              <HitCard key={`${hit.kind}:${hit.id}`} hit={hit} />
            ))}
          </div>
        ) : null,
      )}
    </div>
  )
}

function parsePreview(preview: string): unknown {
  if (!preview) return null
  try {
    return JSON.parse(preview)
  } catch {
    return preview
  }
}

function ExtractionSection({
  payload,
}: {
  payload: { iter: TraceIteration; tool: TraceToolCall } | null
}) {
  if (!payload) {
    return (
      <div className="mt-section">
        <div className="mt-section-head">本轮记忆提取</div>
        <EmptyState text="本轮未触发记忆提取" />
      </div>
    )
  }
  const { iter, tool } = payload
  const args = parsePreview(tool.arguments_preview)
  const result = parsePreview(tool.result_preview)

  return (
    <div className="mt-section">
      <div className="mt-section-head">
        <span>本轮记忆提取</span>
        <span className="mt-dim">iter #{iter.index}</span>
      </div>
      <div className="mt-extract">
        <div className="mt-extract-row">
          <span className="mt-extract-label">工具</span>
          <span className="mt-extract-value mt-mono">{tool.name}</span>
        </div>
        <div className="mt-extract-row">
          <span className="mt-extract-label">状态</span>
          <span className={`mt-extract-status status-${tool.status}`}>{tool.status}</span>
        </div>
        {args !== null && (
          <details className="mt-extract-details">
            <summary>输入</summary>
            <pre>{typeof args === 'string' ? args : JSON.stringify(args, null, 2)}</pre>
          </details>
        )}
        {result !== null && (
          <details className="mt-extract-details">
            <summary>输出</summary>
            <pre>{typeof result === 'string' ? result : JSON.stringify(result, null, 2)}</pre>
          </details>
        )}
      </div>
    </div>
  )
}

export default function MemoryTracePanel({ sessionId, refreshTrigger = 0 }: Props) {
  const { trace, loading, error, refresh } = useTrace(sessionId, refreshTrigger)
  const memory = useMemory('default_user', sessionId, refreshTrigger)

  const latestRecallIter = useMemo(
    () => iterateLatestWithRecall(trace?.iterations),
    [trace],
  )
  const latestExtraction = useMemo(
    () => findLatestExtraction(trace?.iterations),
    [trace],
  )
  const hits = useMemo(() => {
    if (!latestRecallIter?.memory_recall) return []
    return resolveRecallHits(latestRecallIter.memory_recall, memory)
  }, [latestRecallIter, memory])

  if (!sessionId) {
    return (
      <div className="memory-trace-panel">
        <EmptyState text="选择一个会话以查看本轮记忆追踪" />
      </div>
    )
  }

  return (
    <div className="memory-trace-panel">
      {error && (
        <div className="mt-error">
          <span>{error}</span>
          <button onClick={() => void refresh()}>重试</button>
        </div>
      )}

      {loading && !trace && <div className="mt-loading">加载中…</div>}

      <div className="mt-section">
        <div className="mt-section-head">
          <span>本轮记忆召回</span>
          {latestRecallIter && <span className="mt-dim">iter #{latestRecallIter.index}</span>}
        </div>
        {latestRecallIter?.memory_recall ? (
          <>
            <SummaryBar recall={latestRecallIter.memory_recall} iteration={latestRecallIter} />
            <HitsSection hits={hits} />
          </>
        ) : (
          <EmptyState text="本会话尚未触发记忆召回" />
        )}
      </div>

      <ExtractionSection payload={latestExtraction} />
    </div>
  )
}
