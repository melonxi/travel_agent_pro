import { useState, useEffect, useMemo, useCallback } from 'react';
import type { MemoryItem, UseMemoryReturn } from '../types/memory';
import '../styles/memory-center.css';

type TabKey = 'active' | 'pending' | 'archived';

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
};

function formatRelativeTime(iso: string): string {
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

function MemoryCard({
  item,
  onConfirm,
  onReject,
  onDelete,
}: {
  item: MemoryItem;
  onConfirm?: (id: string) => void;
  onReject?: (id: string) => void;
  onDelete?: (id: string) => void;
}) {
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  const cardClass = [
    'memory-card',
    item.status === 'pending' && 'is-pending',
    (item.status === 'rejected' || item.status === 'obsolete') && 'is-archived',
  ]
    .filter(Boolean)
    .join(' ');

  const level = confidenceLevel(item.confidence);
  const domainLabel = DOMAIN_LABELS[item.domain] ?? item.domain;

  return (
    <div className={cardClass}>
      <div className="memory-card-body">
        <span className="memory-domain-tag">{domainLabel}</span>
        <div className="memory-content">{String(item.value)}</div>
        {item.source?.quote && (
          <div className="memory-source">{item.source.quote}</div>
        )}
      </div>

      <div className="memory-meta">
        <span
          className={`memory-badge scope-${item.scope === 'trip' ? 'trip' : 'global'}`}
        >
          {item.scope === 'trip' ? '旅程' : '全局'}
        </span>
        <span className="memory-badge domain">{item.domain}</span>
        <span className="memory-confidence">
          <span className={`memory-confidence-dot ${level}`} />
          {level === 'high' ? '高' : level === 'medium' ? '中' : '低'}
        </span>
        {item.scope === 'trip' && item.trip_id && (
          <span className="memory-trip-id">
            Trip: {item.trip_id.slice(0, 8)}
          </span>
        )}
        <span className="memory-time">
          {formatRelativeTime(item.created_at)}
        </span>
      </div>

      {item.status === 'pending' && (
        <div className="memory-actions">
          <button
            className="memory-action-btn confirm"
            onClick={() => onConfirm?.(item.id)}
          >
            ✓ 确认
          </button>
          <button
            className="memory-action-btn reject"
            onClick={() => onReject?.(item.id)}
          >
            ✗ 拒绝
          </button>
        </div>
      )}

      {item.status === 'active' && !confirmingDelete && (
        <div className="memory-actions">
          <button
            className="memory-action-btn delete"
            onClick={() => setConfirmingDelete(true)}
          >
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

export default function MemoryCenter({
  open,
  onClose,
  memory,
}: MemoryCenterProps) {
  const [activeTab, setActiveTab] = useState<TabKey>('active');

  const {
    memories,
    loading,
    error,
    fetchMemories,
    confirmMemory,
    rejectMemory,
    deleteMemory,
  } = memory;

  // Fetch fresh data when drawer opens
  useEffect(() => {
    if (open) {
      fetchMemories();
    }
  }, [open, fetchMemories]);

  // ESC to close
  useEffect(() => {
    if (!open) return;
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handleKey);
    return () => document.removeEventListener('keydown', handleKey);
  }, [open, onClose]);

  // Lock body scroll when open
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

  const grouped = useMemo(() => {
    const active = memories.filter((m) => m.status === 'active');
    const pending = memories.filter((m) => m.status === 'pending');
    const archived = memories.filter(
      (m) => m.status === 'rejected' || m.status === 'obsolete',
    );
    return { active, pending, archived };
  }, [memories]);

  const currentItems = grouped[activeTab];

  const handleOverlayClick = useCallback(
    (e: React.MouseEvent) => {
      if (e.target === e.currentTarget) onClose();
    },
    [onClose],
  );

  const tabs: Array<{ key: TabKey; label: string; count: number }> = [
    { key: 'active', label: '活跃', count: grouped.active.length },
    { key: 'pending', label: '待确认', count: grouped.pending.length },
    { key: 'archived', label: '已归档', count: grouped.archived.length },
  ];

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
          {tabs.map(({ key, label, count }) => (
            <button
              key={key}
              className={`memory-tab${activeTab === key ? ' is-active' : ''}`}
              onClick={() => setActiveTab(key)}
            >
              {label}
              <span className="memory-tab-count">{count}</span>
            </button>
          ))}
        </div>

        {error && (
          <div className="memory-error">
            <span className="memory-error-text">{error}</span>
            <button className="memory-retry-btn" onClick={fetchMemories}>
              重试
            </button>
          </div>
        )}

        <div className="memory-list">
          {loading && <SkeletonCards />}

          {!loading && !error && currentItems.length === 0 && (
            <div className="memory-empty">
              <div className="memory-empty-icon">🧠</div>
              <div className="memory-empty-text">
                暂无
                {activeTab === 'active'
                  ? '活跃'
                  : activeTab === 'pending'
                    ? '待确认'
                    : '已归档'}
                记忆数据。与 Agent 对话后，系统会自动提取和保存用户偏好。
              </div>
            </div>
          )}

          {!loading &&
            currentItems.map((item) => (
              <MemoryCard
                key={item.id}
                item={item}
                onConfirm={confirmMemory}
                onReject={rejectMemory}
                onDelete={deleteMemory}
              />
            ))}
        </div>
      </div>
    </div>
  );
}
