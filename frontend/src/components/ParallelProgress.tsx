import type { ParallelWorkerStatus } from '../types/plan'

interface Props {
  totalDays: number
  workers: ParallelWorkerStatus[]
  hint?: string | null
}

const STATUS_ICON: Record<ParallelWorkerStatus['status'], string> = {
  running: '⏳',
  done: '✅',
  failed: '❌',
  retrying: '🔄',
}

function renderTail(w: ParallelWorkerStatus): string {
  if (w.status === 'running') {
    const tool = w.current_tool ? `调用 ${w.current_tool}` : '思考中'
    if (w.iteration && w.max_iterations) {
      return `${tool} · ${w.iteration}/${w.max_iterations} 轮`
    }
    return tool
  }
  if (w.status === 'done') {
    return w.activity_count != null
      ? `完成 · ${w.activity_count} 个活动`
      : '完成'
  }
  if (w.status === 'failed') {
    return w.error ? `失败 · ${w.error}` : '失败'
  }
  if (w.status === 'retrying') {
    if (w.iteration && w.max_iterations) {
      return `重试 · ${w.iteration}/${w.max_iterations} 轮`
    }
    return '重试中'
  }
  return ''
}

export default function ParallelProgress({ totalDays, workers, hint }: Props) {
  const doneCount = workers.filter(w => w.status === 'done').length
  const progress = totalDays > 0 ? (doneCount / totalDays) * 100 : 0
  const allDone = doneCount === totalDays

  return (
    <div className="message assistant" data-testid="parallel-progress">
      <div className="parallel-progress-card">
        <div className="parallel-progress-header">
          <span className="parallel-progress-icon">{allDone ? '✨' : '⚡'}</span>
          <span className="parallel-progress-title">
            {allDone ? '行程规划完成' : '并行规划行程中'}
          </span>
          <span className="parallel-progress-count">{doneCount}/{totalDays}</span>
        </div>

        <div className="parallel-progress-workers">
          {workers.map(w => (
            <div
              key={w.day}
              className={`parallel-worker parallel-worker--${w.status}`}
            >
              <span className="parallel-worker-icon">{STATUS_ICON[w.status]}</span>
              <span className="parallel-worker-label">第 {w.day} 天</span>
              {w.theme && (
                <span className="parallel-worker-theme">{w.theme}</span>
              )}
              <span className="parallel-worker-status">{renderTail(w)}</span>
            </div>
          ))}
        </div>

        <div className="parallel-progress-bar-track">
          <div
            className="parallel-progress-bar-fill"
            style={{ width: `${progress}%` }}
          />
        </div>

        {hint && <div className="parallel-progress-hint">{hint}</div>}
      </div>
    </div>
  )
}
