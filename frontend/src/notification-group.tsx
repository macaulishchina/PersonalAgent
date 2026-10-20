import { useEffect, useRef, useState, type ReactNode } from "react";
import { CheckCheck, ChevronDown } from "lucide-react";
import type { Notice } from "./notifications";

export type NoticeGroup = {
  id: string;
  title: string;
  total: number;
  unread: number;
  important: number;
  read_before: number;
  latest: Notice;
};
type History = { items: Notice[]; before: string | null };

export function NotificationGroup({
  group,
  api,
  busy,
  onRead,
  renderNotice,
}: {
  group: NoticeGroup;
  api: (path: string, options?: RequestInit) => Promise<any>;
  busy: boolean;
  onRead: () => void;
  renderNotice: (notice: Notice, compact?: boolean) => ReactNode;
}) {
  const [expanded, setExpanded] = useState(false);
  const [page, setPage] = useState<History>({ items: [], before: null });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const sequence = useRef(0);
  const inFlight = useRef(false);
  const load = async (before: string | null = null) => {
    if (inFlight.current) return;
    inFlight.current = true;
    const seq = ++sequence.current;
    setLoading(true);
    setError("");
    try {
      const result: History = await api("notifications", {
        method: "POST",
        body: JSON.stringify({
          action: "group",
          arguments: {
            group_id: group.id,
            before,
            limit: 20,
            snapshot: group.read_before,
          },
        }),
      });
      if (seq !== sequence.current) return;
      setPage((old) => ({
        ...result,
        items: before
          ? [
              ...old.items,
              ...result.items.filter(
                (n) => !old.items.some((i) => i.id === n.id),
              ),
            ]
          : result.items,
      }));
    } catch (e) {
      if (seq === sequence.current) setError(String(e));
    } finally {
      if (seq === sequence.current) {
        inFlight.current = false;
        setLoading(false);
      }
    }
  };
  useEffect(() => {
    sequence.current++;
    inFlight.current = false;
    setPage({ items: [], before: null });
    setError("");
    setLoading(false);
    if (expanded && group.total > 1) void load();
    return () => {
      sequence.current++;
      inFlight.current = false;
    };
  }, [
    expanded,
    group.id,
    group.latest.id,
    group.latest.revision,
    group.total,
    group.read_before,
  ]);

  return (
    <section className="notification-group" aria-label={group.title}>
      <div className="notification-group-heading">
        <span className="notification-group-name" title={group.title}>
          {group.title}
        </span>
        <span className="notification-group-count">
          {group.total} 条进展
          {group.unread > 0 && (
            <span className="notification-group-unread">
              {" "}
              · {group.unread} 未读
            </span>
          )}
          {group.important > 0 && <span> · {group.important} 重要</span>}
        </span>
      </div>
      {renderNotice(group.latest, !expanded)}
      <div className="notification-group-controls">
        <button
          className="notification-group-toggle"
          aria-expanded={expanded}
          aria-controls={`notice-group-${group.id}`}
          onClick={() => setExpanded(!expanded)}
        >
          <ChevronDown size={14} aria-hidden="true" />
          {expanded
            ? "收起进展"
            : group.total > 1
              ? `展开 ${group.total} 条进展`
              : "查看详情"}
        </button>
        {group.unread > 0 && (
          <button
            className="notification-group-toggle"
            disabled={busy}
            title="标记本组当前所有未读进展；之后的新进展保持未读"
            aria-label={`本组已读：${group.title}`}
            onClick={onRead}
          >
            <CheckCheck size={14} aria-hidden="true" /> 本组已读
          </button>
        )}
      </div>
      <div
        hidden={!expanded}
        id={`notice-group-${group.id}`}
        className="notification-group-history"
      >
        {expanded &&
          page.items
            .filter((item) => item.id !== group.latest.id)
            .map((item) => renderNotice(item))}
        {expanded && error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        {expanded && loading && <p className="task-hint">正在加载进展…</p>}
        {expanded && (page.before || error) && (
          <button
            className="older"
            disabled={loading}
            onClick={() => void load(page.before)}
          >
            加载更早进展
          </button>
        )}
      </div>
    </section>
  );
}
