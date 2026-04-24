import { useState, useEffect, useCallback } from 'react';
import type { MouseEvent, ReactNode } from 'react';
import type {
  ArchivedTripEpisode,
  EpisodeSlice,
  MemoryProfileItem,
  UseMemoryReturn,
  WorkingMemoryItem,
} from '../types/memory';
import '../styles/memory-center.css';

type TabKey = 'profile' | 'hypotheses' | 'episodes' | 'slices';

interface MemoryCenterProps {
  open: boolean;
  onClose: () => void;
  memory: UseMemoryReturn;
}

const DOMAIN_LABELS: Record<string, string> = {
  destination: '目的地偏好',
  hotel: '住宿风格',
  food: '饮食限制',
  budget: '预算偏好',
  transport: '交通方式',
  activity: '活动偏好',
  general: '通用偏好',
  flight: '航班偏好',
  accommodation: '住宿偏好',
  attraction: '景点偏好',
  pace: '节奏偏好',
};

function formatRelativeTime(iso: string): string {
  if (!iso) return '未知时间';

  const diff = Date.now() - new Date(iso).getTime();
  const min = Math.floor(diff / 60000);
  if (min < 1) return '刚刚';
  if (min < 60) return `${min} 分钟前`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} 小时前`;
  const day = Math.floor(hr / 24);
  if (day < 30) return `${day} 天前`;
  return `${Math.floor(day / 30)} 个月前`;
}

function confidenceLevel(c: number): 'high' | 'medium' | 'low' {
  if (c >= 0.7) return 'high';
  if (c >= 0.4) return 'medium';
  return 'low';
}

function formatValue(value: unknown): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value) || (typeof value === 'object' && value !== null)) {
    return JSON.stringify(value, null, 2);
  }
  return '未提供';
}

function getDomainLabel(domain: string): string {
  return DOMAIN_LABELS[domain] ?? domain;
}

function ProfileCard({
  item,
  bucket,
  onConfirm,
  onReject,
  onDelete,
}: {
  item: MemoryProfileItem;
  bucket: 'constraints' | 'rejections' | 'stable_preferences' | 'preference_hypotheses';
  onConfirm?: (id: string) => void;
  onReject?: (id: string) => void;
  onDelete?: (id: string) => void;
}) {
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const level = confidenceLevel(item.confidence);
  const isPending = item.status === 'pending';
  const isRejected = item.status === 'rejected';
  const firstQuoteRef = item.source_refs.find(
    (ref) => typeof ref.quote === 'string' && ref.quote.trim().length > 0,
  );
  const firstQuote =
    typeof firstQuoteRef?.quote === 'string' ? firstQuoteRef.quote : undefined;
  const cardClass = [
    'memory-card',
    isPending && 'is-pending',
    isRejected && 'is-rejected',
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <div className={cardClass}>
      <div className="memory-card-header">
        <span className="memory-domain-pill">{getDomainLabel(item.domain)}</span>
        <span
          className={`memory-confidence-pill level-${level}`}
          title={`系统置信度 ${(item.confidence * 100).toFixed(0)}%`}
        >
          <span className="memory-confidence-dot" />
          {level === 'high' ? '高' : level === 'medium' ? '中' : '低'}
        </span>
        <span className="memory-time">{formatRelativeTime(item.updated_at || item.created_at)}</span>
      </div>

      <div className="memory-card-body">
        <div className="memory-content">{formatValue(item.value)}</div>
        {firstQuote && (
          <div className="memory-source">
            <span className="memory-source-label">原话</span>
            <span className="memory-source-text">{firstQuote}</span>
          </div>
        )}
        {item.applicability && (
          <div className="memory-applicability">{item.applicability}</div>
        )}
      </div>

      {(isPending || (item.status === 'active' && bucket !== 'preference_hypotheses')) && (
        <div className="memory-actions">
          {isPending && (
            <>
              <button className="memory-action-btn confirm" onClick={() => onConfirm?.(item.id)}>
                确认
              </button>
              <button className="memory-action-btn reject" onClick={() => onReject?.(item.id)}>
                拒绝
              </button>
            </>
          )}
          {item.status === 'active' && !confirmingDelete && (
            <button className="memory-action-btn delete" onClick={() => setConfirmingDelete(true)}>
              删除
            </button>
          )}
          {item.status === 'active' && confirmingDelete && (
            <div className="memory-delete-confirm">
              <span>确认删除？</span>
              <button
                className="memory-action-btn danger"
                onClick={() => {
                  onDelete?.(item.id);
                  setConfirmingDelete(false);
                }}
              >
                删除
              </button>
              <button className="memory-action-btn" onClick={() => setConfirmingDelete(false)}>
                取消
              </button>
            </div>
          )}
        </div>
      )}

      {bucket === 'preference_hypotheses' && isPending && (
        <div className="memory-hint">偏好假设：系统观察到的信号，确认后将沉淀为稳定偏好</div>
      )}
    </div>
  );
}

function EpisodeCard({ episode }: { episode: ArchivedTripEpisode }) {
  const dateLabel =
    episode.dates && typeof episode.dates.start === 'string' && typeof episode.dates.end === 'string'
      ? `${episode.dates.start} - ${episode.dates.end}`
      : null;
  const lessonCount = Array.isArray(episode.lesson_log) ? episode.lesson_log.length : 0;
  const decisionCount = Array.isArray(episode.decision_log) ? episode.decision_log.length : 0;

  return (
    <div className="memory-card">
      <div className="memory-card-header">
        <span className="memory-domain-pill dest">{episode.destination ?? '历史旅行'}</span>
        {dateLabel && <span className="memory-time">{dateLabel}</span>}
        <span className="memory-time">{formatRelativeTime(episode.created_at)}</span>
      </div>
      <div className="memory-card-body">
        <div className="memory-content">{episode.final_plan_summary || '暂无摘要'}</div>
        {lessonCount > 0 && (
          <div className="memory-source">
            <span className="memory-source-label">复盘</span>
            <span className="memory-source-text">
              {episode.lesson_log
                .map((lesson) => String(lesson.content ?? lesson.kind ?? ''))
                .filter(Boolean)
                .join('；')}
            </span>
          </div>
        )}
      </div>
      <div className="memory-meta-line">
        <span>决策 {decisionCount}</span>
        <span>·</span>
        <span>复盘 {lessonCount}</span>
      </div>
    </div>
  );
}

function SliceCard({ slice }: { slice: EpisodeSlice }) {
  return (
    <div className="memory-card">
      <div className="memory-card-header">
        <span className="memory-domain-pill">{slice.slice_type}</span>
        <span className="memory-time">{formatRelativeTime(slice.created_at)}</span>
      </div>
      <div className="memory-card-body">
        <div className="memory-content">{slice.content}</div>
        {slice.applicability && <div className="memory-applicability">{slice.applicability}</div>}
      </div>
      {(slice.domains.length > 0 || slice.keywords.length > 0) && (
        <div className="memory-meta-line">
          {slice.domains.map((domain) => (
            <span key={domain}>{getDomainLabel(domain)}</span>
          ))}
          {slice.keywords.slice(0, 3).map((keyword) => (
            <span key={keyword} className="muted">
              #{keyword}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function WorkingMemoryCard({ item }: { item: WorkingMemoryItem }) {
  return (
    <div className="memory-card">
      <div className="memory-card-header">
        <span className="memory-domain-pill">{item.kind}</span>
        <span className="memory-phase-pill">P{item.phase}</span>
        <span className="memory-time">{formatRelativeTime(item.created_at)}</span>
      </div>
      <div className="memory-card-body">
        <div className="memory-content">{item.content}</div>
        {item.reason && (
          <div className="memory-source">
            <span className="memory-source-label">原因</span>
            <span className="memory-source-text">{item.reason}</span>
          </div>
        )}
      </div>
      {item.domains.length > 0 && (
        <div className="memory-meta-line">
          {item.domains.map((domain) => (
            <span key={domain}>{getDomainLabel(domain)}</span>
          ))}
        </div>
      )}
    </div>
  );
}

function Section({
  title,
  count,
  children,
}: {
  title: string;
  count: number;
  children: ReactNode;
}) {
  return (
    <section>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: 8,
          marginTop: 4,
        }}
      >
        <strong style={{ fontSize: '0.9rem', color: 'var(--text-primary)' }}>{title}</strong>
        <span className="memory-tab-count">{count}</span>
      </div>
      {children}
    </section>
  );
}

function SkeletonCards() {
  return (
    <>
      {[0, 1, 2].map((i) => (
        <div key={i} className="memory-skeleton">
          <div className="memory-skeleton-line short" />
          <div className="memory-skeleton-line long" />
          <div className="memory-skeleton-line medium" />
        </div>
      ))}
    </>
  );
}

function EmptyState({ text }: { text: string }) {
  return (
    <div className="memory-empty">
      <div className="memory-empty-icon">🧠</div>
      <div className="memory-empty-text">{text}</div>
    </div>
  );
}

export default function MemoryCenter({
  open,
  onClose,
  memory,
}: MemoryCenterProps) {
  const [activeTab, setActiveTab] = useState<TabKey>('profile');

  const {
    profileBuckets,
    workingMemory,
    episodes,
    episodeSlices,
    loading,
    error,
    actions,
  } = memory;

  const { fetchMemories } = actions;

  useEffect(() => {
    if (open) {
      void fetchMemories();
    }
  }, [open, fetchMemories]);

  useEffect(() => {
    if (!open) return;
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handleKey);
    return () => document.removeEventListener('keydown', handleKey);
  }, [open, onClose]);

  useEffect(() => {
    if (open) {
      document.body.style.overflow = 'hidden';
    } else {
      document.body.style.overflow = '';
    }
    return () => {
      document.body.style.overflow = '';
    };
  }, [open]);

  const profilePendingCount =
    profileBuckets.constraints.filter((item) => item.status === 'pending').length +
    profileBuckets.rejections.filter((item) => item.status === 'pending').length +
    profileBuckets.stable_preferences.filter((item) => item.status === 'pending').length;
  const hypothesisPendingCount = profileBuckets.preference_hypotheses.filter((item) => item.status === 'pending').length;

  const handleOverlayClick = useCallback(
    (e: MouseEvent) => {
      if (e.target === e.currentTarget) onClose();
    },
    [onClose],
  );

  const tabs: Array<{ key: TabKey; label: string; count: number; pending?: number }> = [
    {
      key: 'profile',
      label: '长期画像',
      count:
        profileBuckets.constraints.length +
        profileBuckets.rejections.length +
        profileBuckets.stable_preferences.length,
      pending: profilePendingCount,
    },
    {
      key: 'hypotheses',
      label: '偏好假设',
      count: profileBuckets.preference_hypotheses.length,
      pending: hypothesisPendingCount,
    },
    { key: 'episodes', label: '历史旅行', count: episodes.length },
    { key: 'slices', label: '历史切片', count: episodeSlices.length },
  ];

  const renderProfileTab = () => {
    const hasContent =
      profileBuckets.constraints.length > 0 ||
      profileBuckets.rejections.length > 0 ||
      profileBuckets.stable_preferences.length > 0;

    if (!hasContent) {
      return <EmptyState text="暂无长期画像。系统会在多轮对话后沉淀稳定偏好、约束和明确拒绝。" />;
    }

    return (
      <>
        {profileBuckets.constraints.length > 0 && (
          <Section title="长期约束" count={profileBuckets.constraints.length}>
            {profileBuckets.constraints.map((item) => (
              <ProfileCard
                key={item.id}
                item={item}
                bucket="constraints"
                onDelete={actions.deleteMemory}
              />
            ))}
          </Section>
        )}

        {profileBuckets.rejections.length > 0 && (
          <Section title="明确拒绝" count={profileBuckets.rejections.length}>
            {profileBuckets.rejections.map((item) => (
              <ProfileCard
                key={item.id}
                item={item}
                bucket="rejections"
                onDelete={actions.deleteMemory}
              />
            ))}
          </Section>
        )}

        {profileBuckets.stable_preferences.length > 0 && (
          <Section title="稳定偏好" count={profileBuckets.stable_preferences.length}>
            {profileBuckets.stable_preferences.map((item) => (
              <ProfileCard
                key={item.id}
                item={item}
                bucket="stable_preferences"
                onDelete={actions.deleteMemory}
              />
            ))}
          </Section>
        )}
      </>
    );
  };

  const renderHypothesesTab = () => {
    const hasContent = profileBuckets.preference_hypotheses.length > 0;

    if (!hasContent) {
      return <EmptyState text="暂无偏好假设。系统有不确定但值得观察的信号时，会先记录在这里。" />;
    }

    return (
      <>
        {profileBuckets.preference_hypotheses.length > 0 && (
          <Section title="偏好假设" count={profileBuckets.preference_hypotheses.length}>
            {profileBuckets.preference_hypotheses.map((item) => (
              <ProfileCard
                key={item.id}
                item={item}
                bucket="preference_hypotheses"
                onConfirm={actions.confirmMemory}
                onReject={actions.rejectMemory}
                onDelete={actions.deleteMemory}
              />
            ))}
          </Section>
        )}
      </>
    );
  };

  const renderEpisodesTab = () => {
    const hasContent = episodes.length > 0;

    if (!hasContent) {
      return <EmptyState text="暂无历史旅行。完成更多行程后，这里会沉淀整段旅行的决策结果。" />;
    }

    return (
      <>
        {episodes.length > 0 && (
          <Section title="历史旅行" count={episodes.length}>
            {episodes.map((episode) => (
              <EpisodeCard
                key={episode.id}
                episode={episode}
              />
            ))}
          </Section>
        )}
      </>
    );
  };

  const renderSlicesTab = () => {
    if (episodeSlices.length === 0) {
      return <EmptyState text="暂无历史切片。与当前问题相关的旅行片段沉淀后，会优先展示在这里。" />;
    }

    return (
      <Section title="历史切片" count={episodeSlices.length}>
        {episodeSlices.map((slice) => (
          <SliceCard
            key={slice.id}
            slice={slice}
          />
        ))}
      </Section>
    );
  };

  return (
    <div
      className={`memory-overlay${open ? ' is-open' : ''}`}
      onClick={handleOverlayClick}
    >
      <div className="memory-drawer">
        <div className="memory-header">
          <h2>记忆管理</h2>
          <button className="memory-close-btn" onClick={onClose} aria-label="关闭">
            ×
          </button>
        </div>

        <div className="memory-tabs">
          {tabs.map(({ key, label, count, pending }) => (
            <button
              key={key}
              className={`memory-tab${activeTab === key ? ' is-active' : ''}`}
              onClick={() => setActiveTab(key)}
            >
              {label}
              <span className="memory-tab-count">{count}</span>
              {pending ? <span className="memory-tab-count">待 {pending}</span> : null}
            </button>
          ))}
        </div>

        {error && (
          <div className="memory-error">
            <span className="memory-error-text">{error}</span>
            <button className="memory-retry-btn" onClick={actions.fetchMemories}>
              重试
            </button>
          </div>
        )}

        <div className="memory-list">
          {loading && <SkeletonCards />}
          {!loading && !error && (workingMemory?.items?.length ?? 0) > 0 && (
            <Section title="当前会话工作记忆" count={workingMemory?.items.length ?? 0}>
              {(workingMemory?.items ?? []).map((item) => (
                <WorkingMemoryCard
                  key={item.id}
                  item={item}
                />
              ))}
            </Section>
          )}
          {!loading && !error && activeTab === 'profile' && renderProfileTab()}
          {!loading && !error && activeTab === 'hypotheses' && renderHypothesesTab()}
          {!loading && !error && activeTab === 'episodes' && renderEpisodesTab()}
          {!loading && !error && activeTab === 'slices' && renderSlicesTab()}
        </div>
      </div>
    </div>
  );
}
