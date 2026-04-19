import { useState, useEffect, useMemo, useCallback } from 'react';
import type { MouseEvent, ReactNode } from 'react';
import type {
  EpisodeSlice,
  MemoryItem,
  MemoryProfileItem,
  TripEpisode,
  UseMemoryReturn,
  WorkingMemoryItem,
} from '../types/memory';
import '../styles/memory-center.css';

type TabKey = 'profile' | 'hypotheses' | 'episodes' | 'slices';

interface MemoryCenterProps {
  open: boolean;
  onClose: () => void;
  memory: UseMemoryReturn;
  recalledIds?: string[];
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

function getProfileBucketLabel(bucket: 'constraints' | 'rejections' | 'stable_preferences' | 'preference_hypotheses'): string {
  switch (bucket) {
    case 'constraints':
      return '长期约束';
    case 'rejections':
      return '明确拒绝';
    case 'stable_preferences':
      return '稳定偏好';
    case 'preference_hypotheses':
      return '偏好假设';
  }
}

function ProfileCard({
  item,
  bucket,
  recalled,
  onConfirm,
  onReject,
  onDelete,
}: {
  item: MemoryProfileItem;
  bucket: 'constraints' | 'rejections' | 'stable_preferences' | 'preference_hypotheses';
  recalled?: boolean;
  onConfirm?: (id: string) => void;
  onReject?: (id: string) => void;
  onDelete?: (id: string) => void;
}) {
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const level = confidenceLevel(item.confidence);
  const cardClass = ['memory-card', recalled && 'is-recalled']
    .filter(Boolean)
    .join(' ');
  const firstQuoteRef = item.source_refs.find(
    (ref) => typeof ref.quote === 'string' && ref.quote.trim().length > 0,
  );
  const firstQuote =
    typeof firstQuoteRef?.quote === 'string' ? firstQuoteRef.quote : undefined;

  return (
    <div className={cardClass}>
      <div className="memory-card-body">
        <span className="memory-domain-tag">{getDomainLabel(item.domain)}</span>
        <div className="memory-content">{formatValue(item.value)}</div>
        {item.applicability && <div className="memory-source">{item.applicability}</div>}
        {firstQuote && <div className="memory-source">{firstQuote}</div>}
      </div>

      <div className="memory-meta">
        <span className="memory-badge scope-global">{getProfileBucketLabel(bucket)}</span>
        <span className="memory-badge domain">{item.key}</span>
        <span className="memory-confidence">
          <span className={`memory-confidence-dot ${level}`} />
          {level === 'high' ? '高' : level === 'medium' ? '中' : '低'}
        </span>
        {item.status && <span className="memory-badge domain">{item.status}</span>}
        <span className="memory-time">{formatRelativeTime(item.updated_at || item.created_at)}</span>
      </div>

      {item.status === 'pending' && (
        <div className="memory-actions">
          <button className="memory-action-btn confirm" onClick={() => onConfirm?.(item.id)}>
            ✓ 确认
          </button>
          <button className="memory-action-btn reject" onClick={() => onReject?.(item.id)}>
            ✗ 拒绝
          </button>
        </div>
      )}

      {item.status === 'active' && !confirmingDelete && (
        <div className="memory-actions">
          <button className="memory-action-btn delete" onClick={() => setConfirmingDelete(true)}>
            删除
          </button>
        </div>
      )}

      {item.status === 'active' && confirmingDelete && (
        <div className="memory-delete-confirm">
          <span>确定删除此记忆？</span>
          <button
            className="confirm-yes"
            onClick={() => {
              onDelete?.(item.id);
              setConfirmingDelete(false);
            }}
          >
            确定
          </button>
          <button onClick={() => setConfirmingDelete(false)}>取消</button>
        </div>
      )}
    </div>
  );
}

function LegacyMemoryCard({
  item,
  recalled,
  onConfirm,
  onReject,
  onDelete,
}: {
  item: MemoryItem;
  recalled?: boolean;
  onConfirm?: (id: string) => void;
  onReject?: (id: string) => void;
  onDelete?: (id: string) => void;
}) {
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const cardClass = [
    'memory-card',
    item.status === 'pending' && 'is-pending',
    (item.status === 'rejected' || item.status === 'obsolete') && 'is-archived',
    recalled && 'is-recalled',
  ]
    .filter(Boolean)
    .join(' ');
  const level = confidenceLevel(item.confidence);

  return (
    <div className={cardClass}>
      <div className="memory-card-body">
        <span className="memory-domain-tag">{getDomainLabel(item.domain)}</span>
        <div className="memory-content">{formatValue(item.value)}</div>
        {item.source?.quote && <div className="memory-source">{item.source.quote}</div>}
      </div>

      <div className="memory-meta">
        <span
          className={`memory-badge scope-${item.scope === 'trip' ? 'trip' : 'global'}`}
        >
          {item.scope === 'trip' ? '旧版旅程' : '旧版画像'}
        </span>
        <span className="memory-badge domain">{item.key}</span>
        <span className="memory-confidence">
          <span className={`memory-confidence-dot ${level}`} />
          {level === 'high' ? '高' : level === 'medium' ? '中' : '低'}
        </span>
        <span className="memory-badge domain">{item.status}</span>
        {item.scope === 'trip' && item.trip_id && (
          <span className="memory-trip-id">Trip: {item.trip_id.slice(0, 8)}</span>
        )}
        <span className="memory-time">{formatRelativeTime(item.created_at)}</span>
      </div>

      {item.status === 'pending' && (
        <div className="memory-actions">
          <button className="memory-action-btn confirm" onClick={() => onConfirm?.(item.id)}>
            ✓ 确认
          </button>
          <button className="memory-action-btn reject" onClick={() => onReject?.(item.id)}>
            ✗ 拒绝
          </button>
        </div>
      )}

      {item.status === 'active' && !confirmingDelete && (
        <div className="memory-actions">
          <button className="memory-action-btn delete" onClick={() => setConfirmingDelete(true)}>
            删除
          </button>
        </div>
      )}

      {item.status === 'active' && confirmingDelete && (
        <div className="memory-delete-confirm">
          <span>确定删除此记忆？</span>
          <button
            className="confirm-yes"
            onClick={() => {
              onDelete?.(item.id);
              setConfirmingDelete(false);
            }}
          >
            确定
          </button>
          <button onClick={() => setConfirmingDelete(false)}>取消</button>
        </div>
      )}
    </div>
  );
}

function EpisodeCard({ episode, recalled }: { episode: TripEpisode; recalled?: boolean }) {
  const cardClass = ['memory-card', recalled && 'is-recalled'].filter(Boolean).join(' ');

  return (
    <div className={cardClass}>
      <div className="memory-card-body">
        <span className="memory-domain-tag">{episode.destination ?? '历史旅行'}</span>
        <div className="memory-content">{episode.final_plan_summary || '暂无摘要'}</div>
        {episode.lessons.length > 0 && (
          <div className="memory-source">复盘：{episode.lessons.join('；')}</div>
        )}
      </div>

      <div className="memory-meta">
        <span className="memory-badge scope-trip">历史旅行</span>
        {episode.dates && <span className="memory-badge domain">{episode.dates}</span>}
        <span className="memory-badge domain">接受 {episode.accepted_items.length}</span>
        <span className="memory-badge domain">拒绝 {episode.rejected_items.length}</span>
        <span className="memory-time">{formatRelativeTime(episode.created_at)}</span>
      </div>
    </div>
  );
}

function SliceCard({ slice, recalled }: { slice: EpisodeSlice; recalled?: boolean }) {
  const cardClass = ['memory-card', recalled && 'is-recalled'].filter(Boolean).join(' ');

  return (
    <div className={cardClass}>
      <div className="memory-card-body">
        <span className="memory-domain-tag">{slice.slice_type}</span>
        <div className="memory-content">{slice.content}</div>
        {slice.applicability && <div className="memory-source">{slice.applicability}</div>}
      </div>

      <div className="memory-meta">
        <span className="memory-badge scope-trip">历史切片</span>
        {slice.domains.map((domain) => (
          <span key={domain} className="memory-badge domain">
            {domain}
          </span>
        ))}
        {slice.keywords.slice(0, 2).map((keyword) => (
          <span key={keyword} className="memory-badge domain">
            {keyword}
          </span>
        ))}
        <span className="memory-time">{formatRelativeTime(slice.created_at)}</span>
      </div>
    </div>
  );
}

function WorkingMemoryCard({ item, recalled }: { item: WorkingMemoryItem; recalled?: boolean }) {
  const cardClass = ['memory-card', recalled && 'is-recalled'].filter(Boolean).join(' ');

  return (
    <div className={cardClass}>
      <div className="memory-card-body">
        <span className="memory-domain-tag">{item.kind}</span>
        <div className="memory-content">{item.content}</div>
        {item.reason && <div className="memory-source">{item.reason}</div>}
      </div>

      <div className="memory-meta">
        <span className="memory-badge scope-trip">当前会话工作记忆</span>
        {item.domains.map((domain) => (
          <span key={domain} className="memory-badge domain">
            {getDomainLabel(domain)}
          </span>
        ))}
        <span className="memory-badge domain">P{item.phase}</span>
        <span className="memory-time">{formatRelativeTime(item.created_at)}</span>
      </div>
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
  recalledIds = [],
}: MemoryCenterProps) {
  const [activeTab, setActiveTab] = useState<TabKey>('profile');

  const {
    profileBuckets,
    sessionWorkingMemory,
    episodes,
    slices,
    legacyMemories,
    pendingMemories,
    loading,
    error,
    actions,
  } = memory;

  useEffect(() => {
    if (open) {
      void actions.fetchMemories();
    }
  }, [open, actions]);

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

  const legacyProfileMemories = useMemo(
    () => legacyMemories.filter((item) => item.scope !== 'trip' && item.status === 'active'),
    [legacyMemories],
  );
  const legacyTripMemories = useMemo(
    () => legacyMemories.filter((item) => item.scope === 'trip' && item.status === 'active'),
    [legacyMemories],
  );
  const profilePendingCount =
    profileBuckets.constraints.filter((item) => item.status === 'pending').length +
    profileBuckets.rejections.filter((item) => item.status === 'pending').length +
    profileBuckets.stable_preferences.filter((item) => item.status === 'pending').length;
  const hypothesisPendingCount =
    profileBuckets.preference_hypotheses.filter((item) => item.status === 'pending').length +
    pendingMemories.length;

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
        profileBuckets.stable_preferences.length +
        legacyProfileMemories.length,
      pending: profilePendingCount,
    },
    {
      key: 'hypotheses',
      label: '偏好假设',
      count: profileBuckets.preference_hypotheses.length + pendingMemories.length,
      pending: hypothesisPendingCount,
    },
    { key: 'episodes', label: '历史旅行', count: episodes.length + legacyTripMemories.length },
    { key: 'slices', label: '历史切片', count: slices.length },
  ];

  const renderProfileTab = () => {
    const hasContent =
      profileBuckets.constraints.length > 0 ||
      profileBuckets.rejections.length > 0 ||
      profileBuckets.stable_preferences.length > 0 ||
      legacyProfileMemories.length > 0;

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
                recalled={recalledIds.includes(item.id)}
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
                recalled={recalledIds.includes(item.id)}
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
                recalled={recalledIds.includes(item.id)}
                onDelete={actions.deleteMemory}
              />
            ))}
          </Section>
        )}

        {legacyProfileMemories.length > 0 && (
          <Section title="旧版画像兼容" count={legacyProfileMemories.length}>
            {legacyProfileMemories.map((item) => (
              <LegacyMemoryCard
                key={item.id}
                item={item}
                recalled={recalledIds.includes(item.id)}
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

  const renderHypothesesTab = () => {
    const hasContent =
      profileBuckets.preference_hypotheses.length > 0 || pendingMemories.length > 0;

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
                recalled={recalledIds.includes(item.id)}
                onConfirm={actions.confirmMemory}
                onReject={actions.rejectMemory}
                onDelete={actions.deleteMemory}
              />
            ))}
          </Section>
        )}

        {pendingMemories.length > 0 && (
          <Section title="旧版待确认记忆" count={pendingMemories.length}>
            {pendingMemories.map((item) => (
              <LegacyMemoryCard
                key={item.id}
                item={item}
                recalled={recalledIds.includes(item.id)}
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
    const hasContent = episodes.length > 0 || legacyTripMemories.length > 0;

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
                recalled={recalledIds.includes(episode.id)}
              />
            ))}
          </Section>
        )}

        {legacyTripMemories.length > 0 && (
          <Section title="旧版旅程记忆兼容" count={legacyTripMemories.length}>
            {legacyTripMemories.map((item) => (
              <LegacyMemoryCard
                key={item.id}
                item={item}
                recalled={recalledIds.includes(item.id)}
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

  const renderSlicesTab = () => {
    if (slices.length === 0) {
      return <EmptyState text="暂无历史切片。与当前问题相关的旅行片段沉淀后，会优先展示在这里。" />;
    }

    return (
      <Section title="历史切片" count={slices.length}>
        {slices.map((slice) => (
          <SliceCard
            key={slice.id}
            slice={slice}
            recalled={recalledIds.includes(slice.id)}
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
          {!loading && !error && sessionWorkingMemory.items.length > 0 && (
            <Section title="当前会话工作记忆" count={sessionWorkingMemory.items.length}>
              {sessionWorkingMemory.items.map((item) => (
                <WorkingMemoryCard
                  key={item.id}
                  item={item}
                  recalled={recalledIds.includes(item.id)}
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
