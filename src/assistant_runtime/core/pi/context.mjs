// Pinned Pi context projection only. No credentials, tools or model calls.
import {
  readFileSync,
  writeFileSync,
  openSync,
  fsyncSync,
  closeSync,
  statSync,
} from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { createHash } from "node:crypto";
const sdk = await import(
  pathToFileURL(join(process.argv[2], "dist/index.js")).href
);
let input = "";
for await (const chunk of process.stdin) input += chunk;
const p = JSON.parse(input);
function clean(message) {
  if (message.role === "system") return null;
  if (
    message.role === "custom" &&
    message.customType === "injected_context" &&
    typeof message.content === "string" &&
    message.content.startsWith("[developer]")
  )
    return null; // Framework source runtime configuration, not business history.
  const keys = {
    user: ["role", "content", "timestamp"],
    assistant: [
      "role",
      "content",
      "api",
      "provider",
      "model",
      "usage",
      "stopReason",
      "timestamp",
    ],
    toolResult: [
      "role",
      "toolCallId",
      "toolName",
      "content",
      "isError",
      "timestamp",
    ],
    compactionSummary: ["role", "summary", "tokensBefore", "timestamp"],
    branchSummary: ["role", "summary", "fromId", "timestamp"],
    custom: ["role", "customType", "content", "display", "timestamp"],
    bashExecution: [
      "role",
      "command",
      "output",
      "exitCode",
      "cancelled",
      "truncated",
      "excludeFromContext",
      "timestamp",
    ],
  }[message.role];
  if (!keys) throw new Error("context_unsupported_message");
  const result = Object.fromEntries(
    keys.filter((k) => k in message).map((k) => [k, message[k]]),
  );
  if (Array.isArray(result.content))
    result.content = result.content.flatMap((part) => {
      if (part.type === "thinking") return [];
      const fields = {
        text: ["type", "text"],
        image: ["type", "data", "mimeType"],
        toolCall: ["type", "id", "name", "arguments"],
      }[part.type];
      if (!fields) throw new Error("context_unsupported_content");
      return [
        Object.fromEntries(
          fields.filter((k) => k in part).map((k) => [k, part[k]]),
        ),
      ];
    });
  if (result.role === "assistant" && !result.content.length) return null;
  return result;
}
if (p.action === "export") {
  if (statSync(p.path).size > 1024 * 1024 * 1024)
    throw new Error("context_history_too_large");
  const raw = readFileSync(p.path, "utf8");
  const records = sdk.parseSessionEntries(raw);
  const header = records[0];
  const entries = records.slice(1);
  if (
    header?.type !== "session" ||
    header.version !== sdk.CURRENT_SESSION_VERSION ||
    !entries.some((e) => e.id === p.leaf)
  )
    throw new Error("context_checkpoint_unavailable");
  const messages = sdk
    .buildSessionProjection(entries, p.leaf)
    .messages.map(clean)
    .filter(Boolean);
  process.stdout.write(
    JSON.stringify({
      format: "pi-context-v1",
      messages,
      compacted: messages.some((m) => m.role === "compactionSummary"),
    }),
  );
} else if (p.action === "seed") {
  if (p.bundle.format !== "pi-context-v1")
    throw new Error("context_format_unsupported");
  const manager = sdk.SessionManager.inMemory(p.cwd);
  const timestamps = [];
  for (const original of p.bundle.messages) {
    const m = clean(original);
    if (!m) continue;
    // Source request usage is not a capacity measurement for the target session.
    // Zeroing it lets Pi use its existing full-context estimator before dispatch.
    if (m.role === "assistant")
      m.usage = {
        input: 0,
        output: 0,
        cacheRead: 0,
        cacheWrite: 0,
        totalTokens: 0,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
      };
    timestamps.push(new Date(m.timestamp).toISOString());
    if (m.role === "compactionSummary")
      manager.appendCompaction(m.summary, null, m.tokensBefore);
    else if (m.role === "branchSummary")
      manager.appendBranchSummary(m.fromId, m.summary);
    else manager.appendMessage(m);
  }
  if (p.additional)
    manager.appendMessage({
      role: "user",
      content: [{ type: "text", text: p.additional }],
      timestamp: Date.now(),
    });
  const entries = manager.getEntries().map((entry, i) => ({
    ...entry,
    ...(timestamps[i] ? { timestamp: timestamps[i] } : {}),
  }));
  const raw =
    [manager.getHeader(), ...entries].map((e) => JSON.stringify(e)).join("\n") +
    "\n";
  const fd = openSync(p.path, "wx", 0o600);
  try {
    writeFileSync(fd, raw);
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
  process.stdout.write(JSON.stringify({ seeded: true }));
} else if (p.action === "waiting_checkpoint" || p.action === "waiting_answer") {
  if (statSync(p.path).size > 1024 * 1024 * 1024)
    throw new Error("context_history_too_large");
  const raw = readFileSync(p.path);
  // Do not let the SDK's tolerant reader turn a torn tail into a complete wait.
  const records = raw.toString("utf8").trimEnd().split("\n").map(JSON.parse);
  const parsed = sdk.parseSessionEntries(raw.toString("utf8"));
  const entries = parsed.slice(1);
  const leaf = entries.at(-1)?.id;
  const messages = sdk.buildSessionProjection(entries, leaf).messages;
  const index = messages.findLastIndex((m) => m.role === "assistant");
  const calls = (messages[index]?.content || []).filter((b) => b.type === "toolCall");
  const results = messages.slice(index + 1).filter((m) => m.role === "toolResult");
  const pending = calls.filter((c) => !results.some((r) => r.toolCallId === c.id));
  const digest = createHash("sha256").update(raw).digest("hex");
  if (
    records[0]?.type !== "session" || records[0]?.version !== sdk.CURRENT_SESSION_VERSION ||
    parsed.length !== records.length || raw.at(-1) !== 10 ||
    pending.length !== 1 || pending[0].id !== p.call_id ||
    pending[0].name !== "request_user_input" ||
    messages.slice(index + 1).some((m) => m.role !== "toolResult")
  ) {
    process.stdout.write(JSON.stringify({ available: false, code: "waiting_boundary_incomplete" }));
  } else if (p.action === "waiting_checkpoint") {
    const fd = openSync(p.path, "r+");
    try { fsyncSync(fd); } finally { closeSync(fd); }
    process.stdout.write(JSON.stringify({ available: true, leaf, digest, size: raw.length,
      questions: pending[0].arguments.questions }));
  } else {
    if (digest !== p.digest || leaf !== p.leaf) throw new Error("waiting_checkpoint_changed");
    const manager = sdk.SessionManager.open(p.path);
    manager.appendMessage({ role: "toolResult", toolCallId: p.call_id,
      toolName: "request_user_input", content: [{ type: "text", text: p.text }],
      isError: false, timestamp: Date.now() });
    const fd = openSync(p.path, "r+");
    try { fsyncSync(fd); } finally { closeSync(fd); }
    process.stdout.write(JSON.stringify({ appended: true }));
  }
} else throw new Error("context_operation_unsupported");
