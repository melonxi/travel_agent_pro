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

const STATUS_TEXT: Record<ParallelWorkerStatus['status'], string> = {
  running: '规划中',
  done: '完成',
  failed: '失败',
  retrying: '重试中',
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
              <span className="parallel-worker-status">{STATUS_TEXT[w.status]}</span>
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
