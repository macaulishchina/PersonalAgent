import { useEffect, useRef, useState, type ReactNode } from "react";
import { ArrowUpRight, ChevronDown } from "lucide-react";
import {
  needsAttention,
  reportContent,
  reportTask,
  type MessageOrigin,
} from "./message-presentation";

type Notice = { source?: string; payload?: { origin?: { task_id?: string } } };

export function ReportCollection({
  messages,
  children,
  onOpenTask,
}: {
  messages: (MessageOrigin & { text: string })[];
  children: ReactNode;
  onOpenTask: (id: string) => void;
}) {
  const latest = messages.at(-1)!;
  const important = messages.filter(needsAttention);
  const focus = important.at(-1) || latest;
  const title = latest.report_group!.title;
  const summary = reportContent(focus.text).title;
  const heading =
    summary === title || summary.startsWith(title + " · ")
      ? summary
      : `${title} · ${summary}`;
  const taskId = reportTask(latest);
  return (
    <details className="task-report report-collection">
      <summary title={heading}>
        <span className="task-report-title">{heading}</span>
        {important.length > 0 && (
          <span className="report-important">{important.length} 条需关注</span>
        )}
        <span className="report-count">{messages.length} 条记录</span>
        <ChevronDown size={14} aria-hidden="true" />
      </summary>
      <div className="task-report-content">
        {taskId && (
          <button
            className="tool-result-link task-report-link"
            onClick={() => onOpenTask(taskId)}
          >
            查看对应任务 <ArrowUpRight size={14} aria-hidden="true" />
          </button>
        )}
        {children}
      </div>
    </details>
  );
}

export function TaskReport({
  message,
  renderBody,
  children,
  loadNotice,
  onOpenTask,
  onResolvedTask,
  showSource = false,
}: {
  message: MessageOrigin & { text: string };
  renderBody: (text: string) => ReactNode;
  children?: ReactNode;
  loadNotice: (id: string) => Promise<Notice>;
  onOpenTask: (id: string) => void;
  onResolvedTask: (id: string) => void;
  showSource?: boolean;
}) {
  const { title, body } = reportContent(message.text);
  const source = showSource ? message.report_group?.title : undefined;
  const heading =
    source && title !== source && !title.startsWith(source + " · ")
      ? `${source} · ${title}`
      : title;
  const [linkedTask, setLinkedTask] = useState<string | null>(null);
  const taskId = reportTask(message) || linkedTask;
  const [loading, setLoading] = useState(false);
  const [lookupFailed, setLookupFailed] = useState(false);
  const attempted = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const resolve = async (open: boolean) => {
    if (!open || taskId || attempted.current) return;
    attempted.current = true;
    const noticeId = message.turn_id?.match(
      /^notification:([A-Za-z0-9_-]{1,128})$/,
    )?.[1];
    if (!noticeId) return;
    setLoading(true);
    try {
      const notice = await loadNotice(noticeId);
      const id = notice.payload?.origin?.task_id;
      if (
        mounted.current &&
        typeof id === "string" &&
        /^[A-Za-z0-9_-]{1,128}$/.test(id) &&
        notice.source === "task:" + id
      ) {
        setLinkedTask(id);
        onResolvedTask(id);
      }
    } catch {
      if (mounted.current) setLookupFailed(true);
    } finally {
      if (mounted.current) setLoading(false);
    }
  };
  return (
    <details
      className="task-report"
      onToggle={(event) => void resolve(event.currentTarget.open)}
    >
      <summary title={heading}>
        <span className="task-report-title">{heading}</span>
        {message.report_group?.kind && (
          <span className="notification-type">
            {
              { progress: "进展", result: "结果", attention: "提醒" }[
                message.report_group.kind
              ]
            }
          </span>
        )}
        <ChevronDown size={14} aria-hidden="true" />
      </summary>
      <div className="task-report-content">
        {body && renderBody(body)}
        {children}
        {taskId ? (
          <button
            className="tool-result-link task-report-link"
            onClick={() => onOpenTask(taskId)}
          >
            查看对应任务 <ArrowUpRight size={14} aria-hidden="true" />
          </button>
        ) : loading ? (
          <small>正在读取任务关联…</small>
        ) : lookupFailed ? (
          <small>任务关联暂时无法读取，报告正文仍可查看。</small>
        ) : null}
      </div>
    </details>
  );
}
