import { useEffect, useRef, useState, type ReactNode } from "react";
import { ArrowUpRight, ChevronDown } from "lucide-react";
import {
  reportContent,
  reportTask,
  type MessageOrigin,
} from "./message-presentation";

type Notice = { source?: string; payload?: { origin?: { task_id?: string } } };

export function TaskReport({
  message,
  renderBody,
  children,
  loadNotice,
  onOpenTask,
  onResolvedTask,
}: {
  message: MessageOrigin & { text: string };
  renderBody: (text: string) => ReactNode;
  children?: ReactNode;
  loadNotice: (id: string) => Promise<Notice>;
  onOpenTask: (id: string) => void;
  onResolvedTask: (id: string) => void;
}) {
  const { title, body } = reportContent(message.text);
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
      <summary title={title}>
        <span className="task-report-title">{title}</span>
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
