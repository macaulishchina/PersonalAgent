/** Presentation follows recorded channels and identities, never prose guesses. */
export type MessageOrigin = {
  result_ref?: { id: string; version: string; bytes?: number } | null;
  presentation?: {
    phase?: string | null;
    body?: "complete" | "excerpt";
    turn_status?: string;
    result_offset?: number;
    result_length?: number;
    task_reply?: boolean;
  } | null;
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

export function isExecutionStep(message: MessageOrigin) {
  return (
    message.role === "assistant" &&
    !isReport(message) &&
    (message.presentation?.phase === "tool" ||
      (message.kind === "detail" &&
        ["调用工具", "执行命令", "修改文件", "检索资料"].includes(
          message.text?.split("\n", 1)[0] || "",
        )))
  );
}

/** Preserve order and identity: only consecutive process entries share a fold. */
export function replySections<T extends MessageOrigin>(messages: T[]) {
  const sections: { id: string; process: boolean; messages: T[] }[] = [];
  for (const message of messages) {
    const process =
      isExecutionStep(message) || message.presentation?.phase === "commentary";
    const last = sections.at(-1);
    if (process && last?.process) last.messages.push(message);
    else sections.push({ id: message.id, process, messages: [message] });
  }
  return sections;
}

/** Retry identity is supplied by the tool lifecycle; labels alone prove no relation. */
export function groupToolRetries<
  T extends MessageOrigin & { tool_summary?: { retry_group?: string } | null },
>(messages: T[]) {
  const entries: T[][] = [];
  const retries = new Map<string, T[]>();
  for (const message of messages) {
    const group = message.tool_summary?.retry_group;
    const key =
      group && message.turn_id && isExecutionStep(message)
        ? JSON.stringify([message.turn_id, group])
        : null;
    const previous = key ? retries.get(key) : undefined;
    if (previous) previous.push(message);
    else {
      const entry = [message];
      entries.push(entry);
      if (key) retries.set(key, entry);
    }
  }
  return entries;
}

export function isReplyBody(message: MessageOrigin) {
  return (
    message.role === "assistant" &&
    message.kind === "message" &&
    message.presentation?.phase !== "commentary" &&
    Boolean(message.text?.trim() || message.attachments?.length)
  );
}

/** Media belongs to the reply turn; tool records retain their original evidence. */
export function replyMedia<A extends { id: string }>(
  groups: {
    id: string;
    role: string;
    messages: (Omit<MessageOrigin, "attachments"> & {
      attachments?: readonly A[];
    })[];
  }[],
) {
  const replies = new Map<
    string,
    { group: string; answered: boolean; references: Map<string, A> }
  >();
  for (const group of groups) {
    if (group.role !== "assistant") continue;
    for (const message of group.messages) {
      if (isReport(message)) continue;
      const key = message.turn_id
        ? "turn:" + message.turn_id
        : "group:" + group.id;
      let reply = replies.get(key);
      if (!reply) {
        reply = { group: group.id, answered: false, references: new Map() };
        replies.set(key, reply);
      }
      if (isReplyBody(message)) {
        reply.group = group.id;
        reply.answered = true;
      } else if (message.attachments?.length && !reply.answered)
        reply.group = group.id;
      for (const reference of message.attachments || [])
        if (!reply.references.has(reference.id))
          reply.references.set(reference.id, reference);
    }
  }
  const galleries = new Map<string, A[]>();
  const byTurn = new Map<string, ReadonlySet<string>>();
  for (const [key, reply] of replies) {
    if (key.startsWith("turn:"))
      byTurn.set(key.slice("turn:".length), new Set(reply.references.keys()));
    const previous = galleries.get(reply.group) || [];
    galleries.set(reply.group, [
      ...new Map(
        [...previous, ...reply.references.values()].map((ref) => [ref.id, ref]),
      ).values(),
    ]);
  }
  return { galleries, byTurn };
}

export function replySettled(
  messages: MessageOrigin[],
  activeTurn: string | null | undefined,
  stateKnown: boolean,
) {
  if (
    messages.some((m) =>
      ["completed", "failed", "unknown", "interrupted", "cancelled"].includes(
        m.presentation?.turn_status || "",
      ),
    )
  )
    return true;
  const turn = messages.find((m) => m.turn_id)?.turn_id;
  return Boolean(turn && stateKnown && turn !== activeTurn);
}

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

export function isTaskReply(message: MessageOrigin) {
  return isReport(message) && message.presentation?.task_reply === true;
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
    if (isTaskReply(message)) return "task-reply";
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
