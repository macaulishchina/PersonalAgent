/** Plan state, per-execution state and empty summaries have distinct meanings. */
export const PLAN_LABELS: Record<string, string> = {
  enabled: "计划已启用",
  paused: "后续触发已暂停",
  terminated: "任务已终止",
  archived: "任务已归档",
};

export const RUN_LABELS: Record<string, string> = {
  queued: "等待启动",
  dispatching: "正在启动",
  running: "执行中",
  waiting_user: "等待你处理",
  unknown: "执行状态待核实",
  paused: "本次已暂停",
  completed: "本次已完成",
  failed: "本次执行失败",
  canceled: "本次已取消",
  missed: "本次已跳过",
};

export const RUN_SUMMARIES: Record<string, string> = {
  queued: "本次执行尚未启动。",
  dispatching: "正在启动本次执行。",
  running: "本次正在执行，暂无文字进展。",
  waiting_user: "需要你处理后才能继续。",
  unknown: "本次执行的状态尚未确认，请先核实已有操作，再决定是否继续。",
  paused: "本次执行已暂停；后续是否触发由任务计划控制。",
  completed: "本次执行已结束，未提供文字摘要。",
  failed: "本次执行失败。",
  canceled: "本次执行已取消。",
  missed: "本轮未启动，已跳过。该记录未保存具体原因。",
};

export function planLabel(task: any): string {
  if (task.state !== "enabled") return PLAN_LABELS[task.state] || task.state;
  if (task.spec.trigger?.kind === "event") return "事件监听已启用";
  return task.next_at != null ? "定时计划已启用" : "无待触发计划";
}

export function runSummary(run: any): string {
  if (run.state === "missed") {
    if (run.data.skip_reason === "overlap")
      return "触发时同一任务已有未结束的执行，为避免重复，本轮未启动。";
    if (run.data.skip_reason === "late")
      return "已超过允许补跑的时限，本轮未启动。";
  }
  return run.data.progress || RUN_SUMMARIES[run.state] || "暂无更多信息";
}

export function businessUnsettled(run: any): boolean {
  return ["pending", "unknown", "waiting_user"].includes(
    run.data?.business_result?.state,
  );
}

export function runLabel(run: any): string {
  const state = run.data?.business_result?.state;
  const outcome: Record<string, string> = {
    pending: "正在核验业务结果",
    unknown: "业务结果待核验",
    waiting_user: "业务操作需要你处理",
    partial: "业务部分完成",
    failed: "业务未完成",
    cancelled: "业务操作已取消",
  };
  if (outcome[state]) {
    return `${run.state === "completed" ? "执行已结束" : RUN_LABELS[run.state] || run.state} · ${outcome[state]}`;
  }
  return RUN_LABELS[run.state] || run.state;
}
