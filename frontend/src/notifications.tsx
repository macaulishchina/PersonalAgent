import { useEffect, useState, useRef } from "react";
import {
  Bell,
  Check,
  CheckCheck,
  ArrowUpRight,
  Quote,
  RefreshCw,
} from "lucide-react";
import { RUN_LABELS } from "./task-presentation";
import { formatDuration } from "./duration";
import { PanelHeader } from "./panel-header";
import { NotificationGroup, type NoticeGroup } from "./notification-group";
import { NotificationBody } from "./notification-body";

export type Notice = {
  id: string;
  source: string;
  revision: number;
  unread: boolean;
  expired: boolean;
  created: number;
  payload: {
    kind?: "progress" | "result" | "attention";
    title: string;
    body: string;
    url: string;
    priority: string;
    origin?: { task_title?: string; duration_seconds?: number };
  };
  execution?: {
    kind: "progress" | "result" | "attention";
    state: string | null;
    duration_seconds?: number | null;
  };
  deliveries: { channel: string; state: string; error?: string }[];
};
type Page = {
  items: NoticeGroup[];
  unread: number;
  before: string | null;
  read_before?: number;
};
export function NotificationPanel({
  open,
  onClose,
  api,
  onReference,
  unreadCount,
}: {
  unreadCount: number;
  open: boolean;
  onClose: () => void;
  api: (path: string, options?: RequestInit) => Promise<any>;
  onReference: (notice: { id: string; title: string }) => void;
}) {
  const [data, setData] = useState<Page>({
    items: [],
    unread: 0,
    before: null,
  });
  const [unreadOnly, setUnreadOnly] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const refreshInFlight = useRef(false);
  const [loaded, setLoaded] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const moreInFlight = useRef(false);
  const requestSequence = useRef(0);
  const load = async (more = false) => {
    if (more && (moreInFlight.current || !data.before)) return;
    moreInFlight.current = more;
    setLoadingMore(more);
    const seq = ++requestSequence.current;
    try {
      const result: Page = await api(
        `notifications?grouped=true&unread=${unreadOnly}${more && data.before ? `&before=${encodeURIComponent(data.before)}` : ""}`,
      );
      if (seq !== requestSequence.current) return;
      setData((old) => ({
        ...result,
        items: more
          ? [
              ...old.items,
              ...result.items.filter(
                (n) => !old.items.some((i) => i.id === n.id),
              ),
            ]
          : result.items,
      }));
      setLoaded(true);
    } catch (e) {
      if (seq === requestSequence.current) setError(String(e));
    } finally {
      if (seq === requestSequence.current) {
        moreInFlight.current = false;
        setLoadingMore(false);
      }
    }
  };
  useEffect(() => {
    if (!open) return;
    setError("");
    setLoaded(false);
    void load();
    return () => {
      requestSequence.current++;
    };
  }, [open, unreadOnly, unreadCount]);
  useEffect(() => {
    if (!open) return;
    const pending = data.items
      .map((group) => group.latest)
      .filter(
        (item) =>
          item.execution &&
          [
            "queued",
            "dispatching",
            "running",
            "waiting_user",
            "paused",
            "unknown",
          ].includes(item.execution.state || ""),
      );
    if (!pending.length) return;
    let canceled = false;
    let inFlight = false;
    const timer = window.setInterval(async () => {
      if (document.hidden || inFlight) return;
      inFlight = true;
      const results = await Promise.allSettled(
        pending.map((item) =>
          api("notifications", {
            method: "POST",
            body: JSON.stringify({ action: "get", arguments: { id: item.id } }),
          }),
        ),
      );
      inFlight = false;
      if (canceled) return;
      setData((old) => ({
        ...old,
        items: old.items.map((item) => {
          const result = results.find(
            (r) => r.status === "fulfilled" && r.value.id === item.latest.id,
          );
          return result?.status === "fulfilled" &&
            result.value.revision === item.latest.revision
            ? {
                ...item,
                latest: { ...item.latest, execution: result.value.execution },
              }
            : item;
        }),
      }));
    }, 5000);
    return () => {
      canceled = true;
      window.clearInterval(timer);
    };
  }, [open, data.items]);
  const refresh = async () => {
    if (refreshInFlight.current || busy) return;
    refreshInFlight.current = true;
    setRefreshing(true);
    setError("");
    try {
      // Apply the response immediately; keep only the visual feedback visible
      // long enough to perceive even when the local request finishes instantly.
      await Promise.all([
        load(),
        new Promise<void>((resolve) => window.setTimeout(resolve, 500)),
      ]);
    } finally {
      refreshInFlight.current = false;
      setRefreshing(false);
    }
  };
  const act = async (action: string, item: Notice, channel?: string) => {
    setBusy(true);
    setError("");
    try {
      await api("notifications", {
        method: "POST",
        body: JSON.stringify({
          action,
          arguments: {
            id: item.id,
            revision: item.revision,
            ...(channel ? { channel } : {}),
          },
        }),
      });
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  const readAll = async () => {
    setBusy(true);
    setError("");
    try {
      await api("notifications", {
        method: "POST",
        body: JSON.stringify({
          action: "read_all",
          arguments: { before: data.read_before },
        }),
      });
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  const readGroup = async (group: NoticeGroup) => {
    setBusy(true);
    setError("");
    try {
      await api("notifications", {
        method: "POST",
        body: JSON.stringify({
          action: "read_group",
          arguments: { group_id: group.id, before: group.read_before },
        }),
      });
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  const renderNotice = (item: Notice) => (
    <article
      className={`notification-card ${item.unread ? "unread" : ""}`}
      key={item.id}
    >
      <h3>
        {item.unread && (
          <span
            className="notification-unread-dot"
            role="img"
            aria-label="未读"
          />
        )}
        {item.payload.priority === "important" && (
          <span
            className="notification-important"
            title="重要通知"
            aria-label="重要通知"
          >
            重要
          </span>
        )}
        <span className="notification-type">
          {item.payload.kind === "progress"
            ? "进展"
            : item.payload.kind === "result"
              ? "结果"
              : "提醒"}
        </span>
        {item.payload.title}
      </h3>
      <NotificationBody key={item.revision} text={item.payload.body} />
      {item.payload.url && (
        <a
          className="notification-source"
          href={item.payload.url}
          target="_blank"
          rel="noopener noreferrer"
        >
          查看来源
          <ArrowUpRight size={13} />
        </a>
      )}
      {item.payload.origin?.task_title && (
        <p className="task-hint">
          {item.execution?.kind === "progress"
            ? "任务进展"
            : item.execution?.kind === "result"
              ? "运行结果"
              : "提醒来自"}
          ：{item.payload.origin.task_title}
          {typeof (
            item.execution?.duration_seconds ??
            item.payload.origin.duration_seconds
          ) === "number" && (
            <>
              {" "}
              · 累计执行耗时{" "}
              {formatDuration(
                (item.execution?.duration_seconds ??
                  item.payload.origin.duration_seconds)!,
              )}
            </>
          )}
          {item.execution &&
            [
              "queued",
              "dispatching",
              "running",
              "waiting_user",
              "paused",
              "unknown",
            ].includes(item.execution.state || "") &&
            ` · ${RUN_LABELS[item.execution.state!] || "状态待核实"}${item.execution.state === "running" ? "，耗时待结束后统计" : ""}`}
        </p>
      )}
      {item.deliveries
        .filter((d) => ["unknown", "failed"].includes(d.state))
        .map((d) => (
          <div className="notification-delivery" key={d.channel}>
            <span>
              {d.channel === "voice"
                ? "语音播报"
                : d.channel === "context"
                  ? "助手上下文同步"
                  : "会话显示"}
              {d.state === "unknown" ? "未确认" : "失败"}
            </span>
            {!item.expired && (
              <button
                disabled={busy}
                title={d.error}
                onClick={() => act("retry", item, d.channel)}
              >
                {d.channel === "voice" ? "重新播报" : "重试"}
              </button>
            )}
          </div>
        ))}
      <footer className="notification-footer">
        <time
          dateTime={new Date(item.created * 1000).toISOString()}
          title={item.expired ? "提醒已过期，内容保留" : undefined}
        >
          {new Date(item.created * 1000).toLocaleString("zh-CN", {
            month: "numeric",
            day: "numeric",
            hour: "2-digit",
            minute: "2-digit",
          })}
        </time>
        <div className="notification-actions">
          {item.unread && (
            <button
              className="notification-mark-read"
              disabled={busy}
              aria-label={`标为已读：${item.payload.title}`}
              onClick={() => void act("read", item)}
            >
              <Check size={14} aria-hidden="true" /> 标为已读
            </button>
          )}
          <button
            className="notification-quote"
            aria-label={`引用通知：${item.payload.title}`}
            title="引用到输入框"
            onClick={() => {
              onReference({ id: item.id, title: item.payload.title });
              onClose();
            }}
          >
            <Quote size={14} aria-hidden="true" /> 引用
          </button>
        </div>
      </footer>
    </article>
  );
  return (
    <>
      <div className="task-toolbar activity-toolbar">
        <PanelHeader
          title="通知"
          onClose={onClose}
          actions={
            <button
              className="notification-read-all"
              disabled={busy || !data.unread || data.read_before === undefined}
              onClick={() => void readAll()}
            >
              <CheckCheck size={14} aria-hidden="true" /> 全部已读
            </button>
          }
        />
        <div className="task-filters" role="group" aria-label="通知筛选">
          <button aria-pressed={unreadOnly} onClick={() => setUnreadOnly(true)}>
            未读 {data.unread || ""}
          </button>
          <button
            aria-pressed={!unreadOnly}
            onClick={() => setUnreadOnly(false)}
          >
            全部
          </button>
          <button
            className="notification-refresh"
            disabled={busy || refreshing}
            aria-busy={refreshing}
            onClick={() => void refresh()}
          >
            <RefreshCw
              size={14}
              className={refreshing ? "spin" : undefined}
              aria-hidden="true"
            />
            <span aria-live="polite">{refreshing ? "刷新中" : "刷新"}</span>
          </button>
        </div>
      </div>
      <div
        className="task-list notification-list"
        aria-busy={loadingMore}
        onScroll={(event) => {
          const el = event.currentTarget;
          // Load only on scrolling, not merely because collapsed groups fit on
          // screen. Otherwise a single source could silently fetch every page.
          if (
            el.scrollTop > 0 &&
            el.scrollHeight - el.scrollTop - el.clientHeight < 100 &&
            data.before &&
            loaded &&
            !busy &&
            !refreshing &&
            !loadingMore &&
            !error
          ) {
            void load(true);
          }
        }}
      >
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        {data.items.map((group) =>
          group.total === 1 ? (
            renderNotice(group.latest)
          ) : (
            <NotificationGroup
              key={group.id}
              group={group}
              api={api}
              busy={busy}
              onRead={() => void readGroup(group)}
              renderNotice={renderNotice}
            />
          ),
        )}
        {!data.items.length && !error && (
          <div className="task-empty">
            <Bell size={28} strokeWidth={1.4} />
            <p>
              {!loaded
                ? "正在载入…"
                : unreadOnly
                  ? "没有未读通知"
                  : "还没有通知"}
            </p>
            <small>有新消息时，助手会将它放进收件箱。</small>
          </div>
        )}
        {data.before && (
          <button
            className="older"
            disabled={busy || refreshing || loadingMore}
            onClick={() => {
              setError("");
              void load(true);
            }}
          >
            {loadingMore ? "正在加载…" : "加载更早的通知"}
          </button>
        )}
      </div>
    </>
  );
}
