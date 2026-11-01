import { useEffect, useRef, useState } from "react";
import {
  Check,
  Circle,
  AlertCircle,
  LoaderCircle,
  RefreshCw,
} from "lucide-react";
import { formatDuration } from "./duration";
import {
  mergeTrace,
  traceSteps,
  stepTitle,
  stepState,
  stepSeconds,
  type TraceEvent,
} from "./task-trace-model";

type Api = (path: string, options?: RequestInit) => Promise<any>;

export function TaskTrace({
  runId,
  runState,
  open,
  active,
  onOpen,
  api,
}: {
  runId: string;
  runState: string;
  open: boolean;
  active: boolean;
  onOpen: (value: boolean) => void;
  api: Api;
}) {
  const apiRef = useRef(api);
  apiRef.current = api;
  const cache = useRef<TraceEvent[]>([]);
  const [items, setItems] = useState<TraceEvent[]>([]);
  const [lifecycle, setLifecycle] = useState<any[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [truncated, setTruncated] = useState(false);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    cache.current = [];
    setItems([]);
    setLoaded(false);
    setError("");
    setTruncated(false);
  }, [runId]);
  useEffect(() => {
    if (!open || !active) return;
    let disposed = false;
    let flight: AbortController | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const ongoing = [
      "queued",
      "dispatching",
      "running",
      "waiting_user",
      "unknown",
      "paused",
    ].includes(runState);
    const load = async () => {
      if (disposed || document.hidden || flight) return;
      const controller = new AbortController();
      flight = controller;
      setBusy(true);
      setError("");
      try {
        let after = cache.current.at(-1)?.seq || 0;
        // Storage caps each run at 500 events. Drain pages in order, including
        // completions for earlier calls; never repeatedly fetch only the tail.
        for (let page = 0; page < 5; page++) {
          const result = await apiRef.current(
            `tasks/${encodeURIComponent(runId)}/trace?after=${after}&limit=100`,
            { signal: controller.signal },
          );
          if (disposed || controller.signal.aborted) return;
          if (result.run_id !== runId) throw new Error("执行记录归属不匹配");
          setLifecycle(result.lifecycle?.items || []);
          const events: TraceEvent[] = result.items;
          cache.current = mergeTrace(cache.current, events);
          setItems(cache.current);
          setTruncated(!!result.truncated);
          setLoaded(true);
          const next = cache.current.at(-1)?.seq || after;
          if (result.next == null || next <= after) break;
          after = next;
        }
      } catch (e) {
        if (!disposed && !controller.signal.aborted) setError(String(e));
      } finally {
        if (flight === controller) flight = null;
        if (!disposed) {
          setBusy(false);
          if ((ongoing || controller.signal.aborted) && !document.hidden)
            timer = setTimeout(
              () => void load(),
              controller.signal.aborted ? 0 : 3000,
            );
        }
      }
    };
    const visibility = () => {
      clearTimeout(timer);
      if (document.hidden) flight?.abort();
      else void load();
    };
    void load();
    document.addEventListener("visibilitychange", visibility);
    return () => {
      disposed = true;
      clearTimeout(timer);
      flight?.abort();
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [runId, runState, open, active, refresh]);
  const steps = traceSteps(items);
  return (
    <details
      className="task-trace"
      open={open}
      onToggle={(event) => {
        if (event.target === event.currentTarget)
          onOpen(event.currentTarget.open);
      }}
    >
      <summary>
        执行过程
        {loaded && (
          <span className="task-trace-count">{steps.length} 次工具活动</span>
        )}
      </summary>
      {open && (
        <div>
          <div className="task-trace-toolbar">
            <span>
              {busy
                ? "正在更新…"
                : ["running", "dispatching", "waiting_user"].includes(runState)
                  ? "展开时自动更新"
                  : "已保存的过程记录"}
            </span>
            <button
              disabled={busy || !active}
              onClick={() => setRefresh((value) => value + 1)}
              aria-label="刷新执行过程"
            >
              <RefreshCw size={13} />
              刷新
            </button>
          </div>
          {error && (
            <p role="alert" className="task-trace-error">
              {error}{" "}
              <button onClick={() => setRefresh((value) => value + 1)}>
                重试
              </button>
            </p>
          )}
          {!steps.length && !error && (
            <p className="task-hint">
              {!loaded
                ? "正在加载执行过程…"
                : "暂未记录到工具活动。较早的执行可能没有这些数据。"}
            </p>
          )}
          {!!lifecycle.length && (
            <details className="task-hint">
              <summary>调度与恢复记录</summary>
              <ol>
                {lifecycle.map((item) => (
                  <li key={item.seq}>
                    {new Date(item.at * 1000).toLocaleString()} ·{" "}
                    {(
                      {
                        state: "状态变化",
                        dispatch: "派发证据",
                        waiting: "等待原因",
                        reconciliation: "执行核对",
                        operator_resolution: "人工收尾",
                        launch_error: "发起异常",
                        stop_requested: "请求停止",
                        stop_unconfirmed: "停止未确认",
                        progress: "进展记录",
                        archive_unresolved: "归档后仍待核实",
                        restart: "重启核对",
                      } as Record<string, string>
                    )[item.kind] || item.kind}
                    <div>
                      {item.data.stage || item.data.type || ""}
                      {item.kind === "state"
                        ? ` ${item.data.before} → ${item.data.after}`
                        : ""}
                    </div>
                    {item.data.reason &&
                      typeof item.data.reason === "string" && (
                        <p>{item.data.reason}</p>
                      )}
                  </li>
                ))}
              </ol>
            </details>
          )}
          {!!steps.length && (
            <ol className="task-trace-list">
              {steps.map((step) => {
                const state = stepState(step, runState);
                const seconds = stepSeconds(step);
                const Icon =
                  state.tone === "error"
                    ? AlertCircle
                    : state.tone === "active"
                      ? LoaderCircle
                      : state.tone === "complete"
                        ? Check
                        : Circle;
                return (
                  <li key={step.id} data-state={state.tone}>
                    <Icon
                      className="task-trace-dot"
                      size={14}
                      aria-hidden="true"
                    />
                    <div className="task-trace-step">
                      <div className="task-trace-heading">
                        <span>{stepTitle(step)}</span>
                        <span className="task-trace-state">{state.label}</span>
                      </div>
                      <div className="task-trace-meta">
                        <time
                          dateTime={new Date(
                            (step.started || step.ended)!.at * 1000,
                          ).toISOString()}
                        >
                          {new Date(
                            (step.started || step.ended)!.at * 1000,
                          ).toLocaleTimeString()}
                        </time>
                        {seconds != null && (
                          <span title="开始和结束记录的接收时间间隔">
                            {" "}
                            · 约 {formatDuration(seconds)}
                          </span>
                        )}
                        {step.info.server && <span> · {step.info.server}</span>}
                      </div>
                      {(step.info.device_id || step.info.operation_id) && (
                        <div className="task-trace-meta">
                          {step.info.device_id && (
                            <>设备：{step.info.device_id} </>
                          )}
                          {step.info.operation_id && (
                            <>操作：{step.info.operation_id}</>
                          )}
                        </div>
                      )}
                    </div>
                  </li>
                );
              })}
            </ol>
          )}
          {truncated && (
            <p className="task-hint">记录已达到保存上限，后续步骤可能缺失。</p>
          )}
          {loaded && !!steps.length && (
            <p className="task-trace-note">
              工具状态来自执行器记录；“调用完成”不代表外部操作已经成功。
            </p>
          )}
        </div>
      )}
    </details>
  );
}
