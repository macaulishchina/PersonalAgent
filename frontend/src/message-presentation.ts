/** Presentation follows recorded channels and identities, never prose guesses. */
export type MessageOrigin = {
  id: string;
  role: string;
  kind: string;
  source: string;
  text?: string;
  attachments?: readonly unknown[];
  tool_summary?: unknown;
  task_id?: string | null;
  turn_id?: string | null;
  report_group?: {
    id: string;
    kind?: "progress" | "result" | "attention";
    title: string;
    run_id: string | null;
    task_id: string | null;
    important: boolean;
  };
};

/** Durable association, scoped to its recorded task/run; purpose is a member property. */
function reportGroupKey(message: MessageOrigin) {
  const group = message.report_group;
  return isReport(message) && group?.id
    ? "report:" + JSON.stringify([group.id, group.task_id, group.run_id])
    : null;
}

export function needsAttention(message: MessageOrigin) {
  return Boolean(
    message.report_group?.important ||
    message.report_group?.kind === "attention",
  );
}

export function groupMessages<T extends MessageOrigin & { updated: number }>(
  messages: T[],
) {
  type Group = {
    id: string;
    role: string;
    turn_id?: string | null;
    messages: T[];
  };
  const ordinary: Group[] = [];
  const reports = new Map<string, Group>();
  let last: Group | undefined;
  for (const message of messages) {
    // Provider-only/empty drafts are not replies. Keep the durable record but
    // suppress its bubble, including old failed drafts loaded from history.
    if (
      message.role === "assistant" &&
      message.kind === "message" &&
      typeof message.text === "string" &&
      !message.text.trim() &&
      !message.attachments?.length &&
      !message.tool_summary
    )
      continue;
    const key = reportGroupKey(message);
    if (key) {
      const existing = reports.get(key);
      if (existing) existing.messages.push(message);
      else
        reports.set(key, { id: key, role: message.role, messages: [message] });
      // Removing reports must not accidentally join unrelated conversational replies.
      last = undefined;
      continue;
    }
    const previous = last?.messages.at(-1);
    const sameTurn = message.turn_id && message.turn_id === last?.turn_id;
    const nextTurn =
      message.turn_id && last?.turn_id && message.turn_id !== last.turn_id;
    const nearby =
      previous && Math.abs(message.updated - previous.updated) <= 300;
    const sameReply =
      previous &&
      last?.role === message.role &&
      messageChannel(previous) === messageChannel(message) &&
      !isReport(message) &&
      !isReport(previous) &&
      !nextTurn &&
      (sameTurn || nearby);
    if (last && sameReply) {
      last.messages.push(message);
      last.turn_id ||= message.turn_id;
    } else {
      last = {
        id: message.id,
        role: message.role,
        turn_id: message.turn_id,
        messages: [message],
      };
      ordinary.push(last);
    }
  }
  // Only report projections move. Ordinary conversation retains its recorded order.
  const collections = [...reports.values()];
  for (const group of collections) {
    group.messages.sort((a, b) => a.updated - b.updated);
  }
  const updated = (group: Group) => group.messages.at(-1)!.updated;
  collections.sort((a, b) => updated(a) - updated(b));
  const result: Group[] = [];
  let index = 0;
  for (const group of ordinary) {
    while (
      index < collections.length &&
      updated(collections[index]) < group.messages[0].updated
    ) {
      result.push(collections[index++]);
    }
    result.push(group);
  }
  result.push(...collections.slice(index));
  return result;
}

export function isReport(message: MessageOrigin) {
  return (
    message.role === "assistant" &&
    ["task", "notification"].includes(message.kind)
  );
}

export function reportTask(message: MessageOrigin): string | null {
  if (!isReport(message)) return null;
  if (message.task_id) return message.task_id;
  // Older task definition messages already carry a durable task identity.
  return message.kind === "task" && message.id.startsWith("job:")
    ? message.id.slice(4) || null
    : null;
}

export function messageChannel(message: MessageOrigin) {
  if (isReport(message)) {
    if (message.report_group?.kind === "attention") return "attention";
    return reportTask(message) ||
      message.kind === "task" ||
      message.id.startsWith("run:")
      ? "report"
      : "notification";
  }
  if (message.kind === "legacy") return "legacy";
  return message.source === "voice" ? "voice" : "text";
}

export function reportContent(text: string) {
  const lines = text.trim().split("\n");
  const title = (lines.shift() || "任务报告").replace(/^\s*[#>]\s*/, "");
  return { title, body: lines.join("\n").trim() };
}
