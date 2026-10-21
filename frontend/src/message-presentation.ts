/** Presentation follows recorded channels and identities, never prose guesses. */
export type MessageOrigin = {
  id: string;
  role: string;
  kind: string;
  source: string;
  task_id?: string | null;
  turn_id?: string | null;
  report_group?: {
    id: string;
    title: string;
    run_id: string | null;
    task_id: string | null;
    important: boolean;
  };
};

/** Only adjacent, explicitly associated reports may share a card. */
export function sameReportGroup(a: MessageOrigin, b: MessageOrigin) {
  const x = a.report_group,
    y = b.report_group;
  return Boolean(
    isReport(a) &&
    isReport(b) &&
    x?.id &&
    y?.id &&
    x.id === y.id &&
    x.run_id === y.run_id &&
    x.task_id === y.task_id,
  );
}

export function groupMessages<T extends MessageOrigin & { updated: number }>(
  messages: T[],
) {
  const groups: {
    id: string;
    role: string;
    turn_id?: string | null;
    messages: T[];
  }[] = [];
  for (const message of messages) {
    const last = groups.at(-1);
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
    if (last && previous && (sameReportGroup(previous, message) || sameReply)) {
      last.messages.push(message);
      last.turn_id ||= message.turn_id;
    } else {
      groups.push({
        id: message.id,
        role: message.role,
        turn_id: message.turn_id,
        messages: [message],
      });
    }
  }
  return groups;
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
