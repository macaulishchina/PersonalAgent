/** Presentation follows recorded channels and identities, never prose guesses. */
export type MessageOrigin = {
  id: string;
  role: string;
  kind: string;
  source: string;
  task_id?: string | null;
  turn_id?: string | null;
};

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
