/** Recorded activity, not model reasoning or proof of a successful external action. */
export type TraceEvent = {
  seq: number;
  at: number;
  call_id: string;
  phase: "started" | "completed";
  kind: string;
  status?: string;
  server?: string;
  tool?: string;
  operation_id?: string;
  device_id?: string;
};

export type TraceStep = {
  id: string;
  order: number;
  started?: TraceEvent;
  ended?: TraceEvent;
  info: TraceEvent;
};

export function mergeTrace(previous: TraceEvent[], incoming: TraceEvent[]) {
  const events = new Map(previous.map((event) => [event.seq, event]));
  for (const event of incoming) events.set(event.seq, event);
  return [...events.values()].sort((a, b) => a.seq - b.seq).slice(0, 500);
}

export function traceSteps(events: TraceEvent[]): TraceStep[] {
  const steps = new Map<string, TraceStep>();
  for (const event of [...events].sort((a, b) => a.seq - b.seq)) {
    const step = steps.get(event.call_id) || {
      id: event.call_id,
      order: event.seq,
      info: event,
    };
    if (event.phase === "started") step.started = event;
    else step.ended = event;
    step.info = { ...step.started, ...step.ended } as TraceEvent;
    steps.set(step.id, step);
  }
  return [...steps.values()].sort((a, b) => a.order - b.order);
}

const names: Record<string, string> = {
  list_task_models: "查看可用模型",
  create_task: "创建后台任务",
  get_task: "查询任务进度",
  search_web: "搜索网页",
  fetch_webpage: "读取网页",
  capture_image: "获取画面",
};
export function stepTitle(step: TraceStep) {
  const item = step.info;
  if (item.kind === "mcpToolCall")
    return names[item.tool || ""] || item.tool || "调用工具";
  return (
    {
      commandExecution: "执行命令",
      fileChange: "修改文件",
      webSearch: "搜索网页",
    }[item.kind] || "工具活动"
  );
}

export function stepState(step: TraceStep, runState: string) {
  if (!step.ended) {
    return ["running", "dispatching", "waiting_user"].includes(runState)
      ? { label: "调用中", tone: "active" }
      : { label: "未收到结束记录", tone: "unknown" };
  }
  const state = step.ended.status;
  if (state === "failed" || state === "declined")
    return {
      label: state === "declined" ? "已拒绝" : "调用失败",
      tone: "error",
    };
  if (state === "canceled" || state === "cancelled" || state === "interrupted")
    return { label: "已中止", tone: "unknown" };
  return state === "completed"
    ? { label: "调用完成", tone: "complete" }
    : { label: "已结束 · 结果未确认", tone: "unknown" };
}

export function stepSeconds(step: TraceStep) {
  if (!step.started || !step.ended || step.ended.at < step.started.at)
    return null;
  return step.ended.at - step.started.at;
}
