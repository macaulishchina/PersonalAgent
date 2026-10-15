import { useEffect, useState, useRef } from "react";
import { Search, ListTodo, ArrowLeft, ChevronRight } from "lucide-react";
import { RUN_LABELS, planLabel, runSummary } from "./task-presentation";
import { formatDuration } from "./duration";
import { PanelHeader } from "./panel-header";
import { useTaskFeed } from "./task-feed";

function EndTaskButton({
  active,
  busy,
  label,
  onConfirm,
}: {
  active: boolean;
  busy: boolean;
  label: string;
  onConfirm: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const button = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!active || busy) setConfirming(false);
  }, [active, busy]);
  useEffect(() => {
    if (!confirming) return;
    const timeout = window.setTimeout(() => setConfirming(false), 5000);
    const outside = (event: PointerEvent) => {
      if (!button.current?.contains(event.target as Node)) setConfirming(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        setConfirming(false);
      }
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape, true);
    return () => {
      window.clearTimeout(timeout);
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape, true);
    };
  }, [confirming]);
  return (
    <button
      ref={button}
      className="task-end"
      disabled={busy}
      data-confirming={confirming}
      title={
        confirming
          ? "再次点击将停止后续触发，取消未结束的执行及子任务，并归档。"
          : "终止并归档；需要再次点击确认。"
      }
      onBlur={() => setConfirming(false)}
      onClick={() => {
        if (!confirming) {
          setConfirming(true);
          return;
        }
        setConfirming(false);
        onConfirm();
      }}
    >
      <span aria-live="polite">{confirming ? "确认终止" : label}</span>
    </button>
  );
}

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
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const backButton = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const previousSelection = useRef<string | null>(null);
  useEffect(() => {
    if (selectedId) backButton.current?.focus();
    else if (previousSelection.current) {
      const row = Array.from(
        list.current?.querySelectorAll<HTMLButtonElement>("[data-task-id]") ||
          [],
      ).find((el) => el.dataset.taskId === previousSelection.current);
      row?.focus();
    }
    previousSelection.current = selectedId;
  }, [selectedId]);
  const [filter, setFilter] = useState("temporary");
  const [includeArchived, setIncludeArchived] = useState(false);
  const [includeArchivedPlans, setIncludeArchivedPlans] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  useEffect(() => {
    const timer = window.setTimeout(() => setSearch(query.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [query]);
  const feed = useTaskFeed(
    api,
    `tasks?view=${filter}&include_archived=${filter === "temporary" ? includeArchived : includeArchivedPlans}&query=${encodeURIComponent(search)}&limit=20`,
    open && !selectedId,
  );
  const detail = useTaskFeed(
    api,
    `tasks?task_id=${encodeURIComponent(selectedId || "")}&limit=20`,
    open && !!selectedId,
    historyOpen,
  );
  const current = selectedId ? detail : feed;
  const data = current.data;
  const loaded = current.loaded;
  const [actionError, setActionError] = useState("");
  const error = actionError || current.error;
  const [busy, setBusy] = useState(false);
  const [reply, setReply] = useState<{ runId: string; action: string } | null>(
    null,
  );
  useEffect(() => setHistoryOpen(false), [selectedId]);
  const [replyText, setReplyText] = useState("");
  const refresh = async () => {
    await current.refresh();
  };
  const act = async (action: string, args: object) => {
    setBusy(true);
    setActionError("");
    try {
      await api("tasks", {
        method: "POST",
        body: JSON.stringify({ action, arguments: args }),
      });
      await refresh();
      return true;
    } catch (e) {
      setActionError(String(e));
      return false;
    } finally {
      setBusy(false);
    }
  };
  const filtered = feed.data.tasks;
  const activeStates = [
    "queued",
    "dispatching",
    "running",
    "waiting_user",
    "unknown",
    "paused",
  ];
  const selected = selectedId
    ? detail.data.task?.id === selectedId
      ? detail.data.task
      : feed.data.tasks.find((task) => task.id === selectedId)
    : null;
  const dateLabel = (value: number) =>
    new Date(value * 1000).toLocaleString(undefined, {
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  const renderRun = (r: any) => (
    <details
      key={r.id}
      className="task-run"
      open={["running", "waiting_user", "unknown"].includes(r.state)}
    >
      <summary>
        {RUN_LABELS[r.state] || r.state} ·{" "}
        {new Date(r.created * 1000).toLocaleString()}
      </summary>
      {r.data.finished_at && typeof r.data.duration_seconds === "number" && (
        <p
          className="task-hint"
          title="累计运行时间，包含等待处理时间，不含排队、暂停和待核实期间。"
        >
          累计执行耗时 {formatDuration(r.data.duration_seconds)}
        </p>
      )}
      {r.state !== "missed" && (
        <small>
          模型配置：{r.data.spec.execution.model || "不使用模型"}{" "}
          {r.data.spec.execution.reasoning_effort || ""}
        </small>
      )}
      <p>{runSummary(r)}</p>
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
        {!["archived", "terminated"].includes(selected?.state) &&
          ["paused", "failed", "unknown", "canceled"].includes(r.state) && (
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
            <button type="submit" disabled={busy || !replyText.trim()}>
              提交
            </button>
          </div>
        </form>
      )}
    </details>
  );
  return (
    <>
      <div className="task-toolbar activity-toolbar">
        <PanelHeader title="后台任务" onClose={onClose} />
      </div>
      <div className="task-toolbar">
        {selectedId && (
          <div className="task-heading">
            <button
              ref={backButton}
              className="task-back"
              onClick={() => {
                setSelectedId(null);
                setReply(null);
              }}
            >
              <ArrowLeft size={18} /> 返回任务
            </button>
          </div>
        )}
        {!selectedId && (
          <>
            <label className="task-search">
              <Search size={16} aria-hidden="true" />
              <input
                aria-label="搜索任务"
                placeholder="搜索任务…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </label>
            <div className="task-filterbar">
              <div
                className="task-filters task-categories"
                role="group"
                aria-label="任务筛选"
              >
                {[
                  ["temporary", "临时任务"],
                  ["planned", "计划"],
                ].map(([value, label]) => (
                  <button
                    key={value}
                    aria-pressed={filter === value}
                    onClick={() => {
                      setFilter(value);
                      setActionError("");
                    }}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <label className="task-scope-option">
                <input
                  type="checkbox"
                  checked={
                    filter === "temporary"
                      ? includeArchived
                      : includeArchivedPlans
                  }
                  onChange={(event) => {
                    if (filter === "temporary")
                      setIncludeArchived(event.target.checked);
                    else setIncludeArchivedPlans(event.target.checked);
                    setActionError("");
                  }}
                  title={
                    filter === "temporary"
                      ? "包含已结束、已终止和已归档的临时任务。"
                      : "同时显示已归档的计划。"
                  }
                />
                <span>包含归档</span>
              </label>
              <span className="task-scope-count">
                {filtered.length}
                {feed.data.before ? "+" : ""} 项
              </span>
            </div>
          </>
        )}
      </div>
      <div
        className="task-list task-browser"
        ref={(node) => {
          list.current = node;
          current.scroll.current = node;
        }}
        key={selectedId || "list"}
      >
        {error && <p role="alert">{error}</p>}
        {!selectedId &&
          filtered.map((task) => {
            const runs = data.runs.filter((r) => r.task_id === task.id);
            const latest = [...runs].sort((a, b) => b.created - a.created)[0];
            const active = runs.find((r) => activeStates.includes(r.state));
            const status = active ? RUN_LABELS[active.state] : planLabel(task);
            return (
              <button
                className="task-row"
                key={task.id}
                data-task-id={task.id}
                onClick={() => setSelectedId(task.id)}
              >
                <span className="task-row-content">
                  <span className="task-row-title">{task.spec.title}</span>
                  <span className="task-row-meta">
                    <span className={active ? "task-live-status" : ""}>
                      {status}
                    </span>
                    {task.next_at
                      ? ` · 下次 ${dateLabel(task.next_at)}`
                      : latest
                        ? ` · 最近 ${dateLabel(latest.created)}`
                        : ""}
                  </span>
                  {latest?.data.progress && (
                    <span className="task-row-preview">
                      {latest.data.progress}
                    </span>
                  )}
                </span>
                <ChevronRight size={17} aria-hidden="true" />
              </button>
            );
          })}
        {(selected ? [selected] : []).map((d) => (
          <section className="task-detail" key={d.id} aria-label={d.spec.title}>
            <div className="task-card-heading">
              <h3>{d.spec.title}</h3>
              <span className="task-state">{planLabel(d)}</span>
            </div>
            {d.category === "temporary" && (
              <p className="task-hint">
                临时任务 ·{" "}
                {d.spec.auto_archive
                  ? "执行结束后自动归档，勾选“包含归档”可查看结果。"
                  : "执行结束后保留。"}
              </p>
            )}
            <details className="task-description">
              <summary>任务说明</summary>
              <p>{d.spec.prompt}</p>
            </details>
            {d.next_at && <small>下次计划触发：{dateLabel(d.next_at)}</small>}
            <p className="task-hint">
              后续运行的自动结果通知：
              {d.spec.notify_completion === false
                ? "关闭"
                : d.spec.notify_voice
                  ? "开启，含语音"
                  : "开启"}
              <span className="task-notification-note">
                此开关不影响任务按要求主动发送的事件提醒。
              </span>
            </p>
            <div className="task-actions">
              {!["archived", "terminated"].includes(d.state) && (
                <>
                  <button
                    disabled={busy}
                    title="新增一次手动执行，不改变原有计划；资源空闲后启动。"
                    onClick={() =>
                      act("run", { task_id: d.id, request_id: nonce() })
                    }
                  >
                    触发一次
                  </button>
                  <button
                    disabled={busy}
                    title="仅控制后续触发，不停止已经启动的执行。"
                    onClick={() =>
                      act(d.state === "paused" ? "resume" : "pause", {
                        task_id: d.id,
                      })
                    }
                  >
                    {d.state === "paused" ? "恢复计划" : "暂停计划"}
                  </button>
                </>
              )}
              {d.state !== "archived" && (
                <EndTaskButton
                  key={d.id}
                  active={open}
                  busy={busy}
                  label={d.category === "temporary" ? "终止任务" : "终止计划"}
                  onConfirm={() => {
                    void act("archive", { task_id: d.id });
                  }}
                />
              )}
            </div>
            <div className="task-runs" aria-label="此任务的运行记录">
              <div className="task-runs-heading">运行记录</div>
              {data.runs
                .filter(
                  (r) => r.task_id === d.id && activeStates.includes(r.state),
                )
                .map(renderRun)}
              {data.runs.some(
                (r) => r.task_id === d.id && !activeStates.includes(r.state),
              ) && (
                <details
                  className="task-run-history"
                  open={historyOpen}
                  onToggle={(event) => setHistoryOpen(event.currentTarget.open)}
                >
                  <summary>
                    历史记录 ·{" "}
                    {
                      data.runs.filter(
                        (r) =>
                          r.task_id === d.id && !activeStates.includes(r.state),
                      ).length
                    }{" "}
                    {data.before ? "+" : ""} 条
                  </summary>
                  {data.runs
                    .filter(
                      (r) =>
                        r.task_id === d.id && !activeStates.includes(r.state),
                    )
                    .map(renderRun)}
                </details>
              )}
              {!data.runs.some((r) => r.task_id === d.id) && (
                <p className="task-hint">尚无运行记录</p>
              )}
            </div>
          </section>
        ))}
        {!selectedId && !filtered.length && !error && (
          <div className="task-empty">
            <ListTodo size={28} strokeWidth={1.4} />
            <p>
              {!loaded
                ? "正在载入任务…"
                : query
                  ? "没有找到匹配的任务"
                  : filter === "temporary"
                    ? includeArchived
                      ? "暂无临时任务"
                      : "暂无活跃临时任务"
                    : includeArchivedPlans
                      ? "暂无计划"
                      : "暂无未归档计划"}
            </p>
            <small>
              {query
                ? "试试其他关键词"
                : filter === "temporary"
                  ? "一次性委派在这里跟进，勾选“包含归档”可查看过往结果。"
                  : "在聊天中说出安排，助手会为你创建任务。"}
            </small>
          </div>
        )}
        {selectedId && !selected && !error && (
          <p className="task-hint">正在载入任务…</p>
        )}
        {(!selectedId || historyOpen) &&
          (current.data.before || (!loaded && current.loading)) && (
            <div
              ref={current.sentinel}
              className="task-load-more"
              role="status"
            >
              {current.loadingMore || !loaded
                ? "正在加载…"
                : "向下滚动加载更多"}
            </div>
          )}
        {current.error && (
          <button
            className="task-load-more"
            onClick={() => void current.retry()}
          >
            重试加载
          </button>
        )}
      </div>
    </>
  );
}
