import { useCallback, useEffect, useRef, useState } from "react";
import { X, Search, ListTodo } from "lucide-react";

export function TaskPanel({
  open,
  onClose,
  api,
  nonce,
}: {
  open: boolean;
  onClose: () => void;
  api: (path: string, options?: RequestInit) => Promise<any>;
  nonce: () => string;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [visible, setVisible] = useState(false);
  const [filter, setFilter] = useState("current");
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    const element = dialog.current!;
    if (!open) {
      setVisible(false);
      const delay = window.matchMedia("(prefers-reduced-motion: reduce)")
        .matches
        ? 0
        : 240;
      const timer = window.setTimeout(() => element.close(), delay);
      return () => window.clearTimeout(timer);
    }
    element.showModal();
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    // Give the closed pose a frame before starting the entrance transition.
    let secondFrame = 0;
    const frame = requestAnimationFrame(() => {
      secondFrame = requestAnimationFrame(() => setVisible(true));
    });
    return () => {
      cancelAnimationFrame(frame);
      cancelAnimationFrame(secondFrame);
      document.body.style.overflow = previousOverflow;
    };
  }, [open]);
  const [data, setData] = useState<{ tasks: any[]; runs: any[] }>({
    tasks: [],
    runs: [],
  });
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [reply, setReply] = useState<{ runId: string; action: string } | null>(
    null,
  );
  const [replyText, setReplyText] = useState("");
  const refresh = useCallback(async () => {
    try {
      setData(await api("tasks"));
      setLoaded(true);
    } catch (e) {
      setError(String(e));
    }
  }, []);
  useEffect(() => {
    if (!open) return;
    void refresh();
    const timer = setInterval(() => void refresh(), 2000);
    return () => clearInterval(timer);
  }, [refresh, open]);
  const act = async (action: string, args: object) => {
    setBusy(true);
    setError("");
    try {
      await api("tasks", {
        method: "POST",
        body: JSON.stringify({ action, arguments: args }),
      });
      await refresh();
      return true;
    } catch (e) {
      setError(String(e));
      return false;
    } finally {
      setBusy(false);
    }
  };
  const labels: Record<string, string> = {
    enabled: "已启用",
    paused: "已暂停",
    terminated: "已终止",
    archived: "已归档",
    queued: "排队中",
    dispatching: "正在启动",
    running: "执行中",
    waiting_user: "等待你回答",
    unknown: "结果待核对",
    completed: "已完成",
    failed: "失败",
    canceled: "已取消",
    missed: "已错过",
  };
  const filtered = data.tasks.filter((task) => {
    const history = ["archived", "terminated"].includes(task.state);
    return (
      (filter === "all" || (filter === "history" ? history : !history)) &&
      `${task.spec.title} ${task.spec.prompt}`
        .toLowerCase()
        .includes(query.trim().toLowerCase())
    );
  });
  return (
    <dialog
      ref={dialog}
      id="task-panel"
      className="task-layer"
      data-visible={visible}
      aria-label="后台任务"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="task-panel">
        <div className="task-sheet-handle" aria-hidden="true" />
        <div className="task-toolbar">
          <div className="task-heading">
            <div>
              <h2>后台任务</h2>
              <p className="task-hint">在这里查看进展，回到对话继续安排。</p>
            </div>
            <button
              className="icon-button"
              onClick={onClose}
              aria-label="关闭任务列表"
            >
              <X size={20} />
            </button>
          </div>
          <label className="task-search">
            <Search size={16} aria-hidden="true" />
            <input
              aria-label="搜索任务"
              placeholder="搜索任务…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </label>
          <div className="task-filters" role="group" aria-label="任务筛选">
            {[
              ["current", "当前"],
              ["history", "历史"],
              ["all", "全部"],
            ].map(([value, label]) => (
              <button
                key={value}
                aria-pressed={filter === value}
                onClick={() => setFilter(value)}
              >
                {label}
              </button>
            ))}
            <span>{filtered.length} 项</span>
          </div>
        </div>
        <div className="task-list">
          {error && <p role="alert">{error}</p>}
          {filtered.map((d) => (
            <section className="task-card" key={d.id}>
              <div className="task-card-heading">
                <strong>{d.spec.title}</strong>
                <span className="task-state">{labels[d.state] || d.state}</span>
              </div>
              <details className="task-description">
                <summary>{d.spec.prompt}</summary>
                <p>{d.spec.prompt}</p>
              </details>
              {d.next_at && (
                <small>
                  下次：{new Date(d.next_at * 1000).toLocaleString()}
                </small>
              )}
              <div className="task-actions">
                {!["archived", "terminated"].includes(d.state) && (
                  <>
                    <button
                      disabled={busy}
                      onClick={() =>
                        act(d.state === "paused" ? "resume" : "pause", {
                          task_id: d.id,
                        })
                      }
                    >
                      {d.state === "paused" ? "恢复计划" : "暂停计划"}
                    </button>
                    <button
                      disabled={busy}
                      onClick={() =>
                        act("run", { task_id: d.id, request_id: nonce() })
                      }
                    >
                      运行一次
                    </button>
                    <button
                      disabled={busy}
                      onClick={() => act("terminate", { task_id: d.id })}
                    >
                      终止任务
                    </button>
                  </>
                )}
                {d.state !== "archived" && (
                  <button
                    disabled={busy}
                    onClick={() => act("archive", { task_id: d.id })}
                  >
                    归档
                  </button>
                )}
              </div>
              {data.runs
                .filter((r) => r.task_id === d.id)
                .map((r) => (
                  <details
                    key={r.id}
                    className="task-run"
                    open={["running", "waiting_user", "unknown"].includes(
                      r.state,
                    )}
                  >
                    <summary>
                      {labels[r.state] || r.state} ·{" "}
                      {new Date(r.created * 1000).toLocaleString()}
                    </summary>
                    <small>
                      {r.data.spec.execution.model || "无需模型"}{" "}
                      {r.data.spec.execution.reasoning_effort || ""}
                    </small>
                    <p>{r.data.progress || "等待执行"}</p>
                    {r.data.artifacts?.map((f: any) => (
                      <p key={f.name}>
                        <a
                          href={`/api/tasks/${r.id}/artifacts/${encodeURIComponent(f.name)}`}
                        >
                          {f.name}
                        </a>{" "}
                        · {f.bytes} B
                      </p>
                    ))}
                    {r.data.questions?.map((q: any) => (
                      <p key={q.id}>{q.question}</p>
                    ))}
                    <div className="task-actions">
                      {["running", "waiting_user"].includes(r.state) && (
                        <button
                          disabled={busy}
                          onClick={() => {
                            setReply({ runId: r.id, action: "supplement" });
                            setReplyText("");
                          }}
                        >
                          补充 / 回答
                        </button>
                      )}
                      {[
                        "queued",
                        "running",
                        "waiting_user",
                        "dispatching",
                        "unknown",
                        "paused",
                      ].includes(r.state) && (
                        <button
                          disabled={busy}
                          onClick={() => act("cancel_run", { run_id: r.id })}
                        >
                          取消本次
                        </button>
                      )}
                      {["running", "queued"].includes(r.state) && (
                        <button
                          disabled={busy}
                          onClick={() => act("pause_run", { run_id: r.id })}
                        >
                          暂停本次
                        </button>
                      )}
                      {["paused", "failed", "unknown", "canceled"].includes(
                        r.state,
                      ) && (
                        <button
                          disabled={busy}
                          onClick={() => {
                            setReply({ runId: r.id, action: "continue" });
                            setReplyText("");
                          }}
                        >
                          继续
                        </button>
                      )}
                    </div>
                    {reply && reply.runId === r.id && (
                      <form
                        className="task-reply"
                        onSubmit={async (event) => {
                          event.preventDefault();
                          if (!reply || !replyText.trim() || busy) return;
                          if (
                            await act(reply.action, {
                              run_id: r.id,
                              text: replyText.trim(),
                            })
                          )
                            setReply(null);
                        }}
                      >
                        <textarea
                          autoFocus
                          aria-label="任务补充说明"
                          placeholder={
                            reply.action === "continue"
                              ? "说明如何继续；结果不确定时请先核对已有操作。"
                              : "补充要求，或回答任务的问题…"
                          }
                          value={replyText}
                          onChange={(event) => setReplyText(event.target.value)}
                        />
                        <div>
                          <button type="button" onClick={() => setReply(null)}>
                            取消
                          </button>
                          <button
                            type="submit"
                            disabled={busy || !replyText.trim()}
                          >
                            提交
                          </button>
                        </div>
                      </form>
                    )}
                  </details>
                ))}
            </section>
          ))}
          {!filtered.length && !error && (
            <div className="task-empty">
              <ListTodo size={28} strokeWidth={1.4} />
              <p>
                {!loaded
                  ? "正在载入任务…"
                  : query
                    ? "没有找到匹配的任务"
                    : filter === "current"
                      ? "暂时没有待办任务"
                      : "这里还没有任务"}
              </p>
              <small>
                {query
                  ? "试试其他关键词"
                  : "在聊天中说出安排，助手会为你创建任务。"}
              </small>
            </div>
          )}
        </div>
      </div>
    </dialog>
  );
}
