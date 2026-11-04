/** Evidence-driven outcomes around Pi's native pipeline, not an execution loop. */
import { createHash, createHmac, randomBytes } from "node:crypto";

function canonical(value: any): string {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (value && typeof value === "object")
    return (
      "{" +
      Object.keys(value)
        .sort()
        .map((k) => JSON.stringify(k) + ":" + canonical(value[k]))
        .join(",") +
      "}"
    );
  return JSON.stringify(value) ?? "null";
}
export function ownerOutcome(result: any, depth = 0): any {
  if (!result || typeof result !== "object" || depth > 4) return undefined;
  const value =
    result._meta?.["personal_agent.tool_outcome"] || result.tool_outcome;
  if (
    value &&
    ["not_dispatched", "dispatched", "unknown"].includes(value.dispatch)
  )
    return value;
  return ownerOutcome(result.structuredContent, depth + 1);
}

export function registerToolLifecycle(
  pi: any,
  policy: any,
  maximum: number,
  internal: string[],
  origin: (
    name: string,
  ) => { server: string; tool: string; readOnly?: boolean } | undefined,
  readMaximum = 3,
) {
  const secret = randomBytes(32);
  const version = (value: any) =>
    createHash("sha256").update(canonical(value)).digest("hex").slice(0, 16);
  const permissionVersion = version(policy);
  const calls = new Map<string, any>();
  const rejected = new Map<string, any>();
  const uncertain = new Set<string>();
  const readFailures = new Map<string, { outcome: any; attempts: number }>();
  let stopped = false;
  let truncated = false;
  const fingerprint = (name: string, args: any) =>
    createHmac("sha256", secret)
      .update(
        canonical({
          name,
          args,
          policy,
          schema: pi.getAllTools().find((tool: any) => tool.name === name)
            ?.parameters,
        }),
      )
      .digest("hex");
  const refusal = (code: string, message: string) => ({
    code,
    stage: "authorization",
    dispatch: "not_dispatched",
    outcome: "failed",
    message,
    next_action: "change_policy",
  });
  pi.on("before_agent_start", () => {
    calls.clear();
    rejected.clear();
    uncertain.clear();
    readFailures.clear();
    stopped = false;
    truncated = false;
  });
  pi.on("message_end", (event: any) => {
    if (event.message.role === "assistant")
      truncated = event.message.stopReason === "length";
  });
  pi.on("tool_execution_start", (event: any) => {
    calls.set(event.toolCallId, {
      key: fingerprint(event.toolName, event.args),
      started: performance.now(),
      parent: event.parentToolCallId,
      definition: version(
        pi.getAllTools().find((t: any) => t.name === event.toolName)
          ?.parameters,
      ),
      readOnly:
        ["read", "read_result", "tool_search"].includes(event.toolName) ||
        (internal.includes(origin(event.toolName)?.server) &&
          origin(event.toolName)?.readOnly === true),
    });
  });
  pi.on("tool_call", (event: any) => {
    const entry = calls.get(event.toolCallId);
    let outcome;
    if (stopped)
      outcome = refusal(
        "repeated_invalid_call",
        "This turn stopped after repeated unchanged invalid calls.",
      );
    else if (
      Array.isArray(policy.allowed_tools) &&
      !policy.allowed_tools.includes(event.toolName) &&
      event.toolName !== "tool_search"
    )
      outcome = refusal(
        "tool_scope_denied",
        "Tool is outside this execution scope. No approval dialog is available.",
      );
    else if (event.toolName.startsWith("mcp__")) {
      const identity = origin(event.toolName);
      const server = identity?.server;
      const name = identity?.tool;
      const rule = policy.mcp_policies?.[server];
      if (
        rule &&
        (rule.approval === "reject" ||
          rule.deny_tools?.includes(name) ||
          (Array.isArray(rule.allow_tools) &&
            !rule.allow_tools.includes(name)) ||
          (rule.tool_approvals?.[name] || rule.approval || "approve") !==
            "approve")
      )
        outcome = refusal(
          "tool_permission_denied",
          "Tool is disabled by policy. No approval dialog is available; repeated calls cannot grant permission.",
        );
    }
    const previousRead = entry?.readOnly
      ? readFailures.get(entry.key)
      : undefined;
    const readLimit =
      previousRead?.outcome.retryable === true
        ? readMaximum
        : previousRead?.outcome.retryable === false
          ? 1
          : Math.min(2, readMaximum);
    if (!outcome && previousRead && previousRead.attempts >= readLimit) {
      entry.readBlocked = true;
      outcome = {
        ...previousRead.outcome,
        stage: "preparation",
        dispatch: "not_dispatched",
        outcome: "failed",
        retryable: false,
        message: `Read attempt limit reached; last error: ${previousRead.outcome.message}`,
        next_action:
          previousRead.outcome.retryable === false
            ? previousRead.outcome.next_action
            : "inspect_source",
      };
    }
    if (!outcome && entry && uncertain.has(entry.key))
      outcome = {
        code: "prior_result_unknown",
        stage: "preparation",
        dispatch: "not_dispatched",
        outcome: "unknown",
        message:
          "An identical call in this turn has an unconfirmed outcome. Query its receipt or ask the user; it will not be dispatched again.",
        next_action: "inspect_operation",
      };
    if (!outcome && entry && rejected.has(entry.key))
      outcome = rejected.get(entry.key).outcome;
    if (entry) {
      entry.validated = performance.now();
      entry.refusal = outcome;
    }
    if (outcome) return { block: true, reason: outcome.message };
  });
  pi.on("tool_execution_end", (event: any, ctx: any) => {
    const entry = calls.get(event.toolCallId);
    const server = origin(event.toolName)?.server;
    let outcome =
      internal.includes(server) ||
      ["read_result", "request_user_input"].includes(event.toolName)
        ? ownerOutcome(event.result)
        : undefined;
    if (!outcome && event.durationMs === undefined) {
      outcome = entry?.refusal || {
        code: truncated
          ? "incomplete_tool_arguments"
          : pi.getAllTools().some((t: any) => t.name === event.toolName)
            ? "invalid_arguments"
            : "tool_unavailable",
        stage: "validation",
        dispatch: "not_dispatched",
        outcome: "failed",
        message: truncated
          ? "Model output was truncated; no operation was dispatched."
          : "Tool was not executed. Inspect its current definition and availability before correcting the call.",
        next_action: "inspect_definition",
      };
    }
    if (!outcome)
      outcome = {
        code: event.isError
          ? entry?.readOnly
            ? "tool_read_failed"
            : "tool_result_unconfirmed"
          : "tool_returned",
        stage: "return",
        dispatch: "dispatched",
        outcome: event.isError
          ? entry?.readOnly
            ? "failed"
            : "unknown"
          : "success",
        message: event.isError
          ? entry?.readOnly
            ? "Read failed; retry the read once or correct its inputs. No write operation was requested."
            : "The tool reported an error after dispatch; verify the operation before repeating it."
          : "Tool returned; business completion is defined by its receipt.",
        next_action: event.isError
          ? entry?.readOnly
            ? "retry_read"
            : "inspect_operation"
          : "use_result",
      };
    if (
      entry?.readOnly &&
      outcome.outcome === "failed" &&
      outcome.dispatch === "dispatched"
    ) {
      const attempts = (readFailures.get(entry.key)?.attempts || 0) + 1;
      if (!readFailures.has(entry.key) && readFailures.size >= 256)
        readFailures.delete(readFailures.keys().next().value);
      readFailures.set(entry.key, { outcome, attempts });
      outcome = { ...outcome, read_attempt: attempts };
    } else if (entry?.readOnly && outcome.outcome === "success")
      readFailures.delete(entry.key);
    if (
      entry &&
      outcome.outcome === "unknown" &&
      outcome.dispatch !== "not_dispatched"
    )
      uncertain.add(entry.key);
    if (event.isError && outcome.dispatch === "not_dispatched" && entry) {
      const previous = rejected.get(entry.key);
      const repeats =
        (previous?.outcome.code === outcome.code ? previous.repeats : 0) + 1;
      if (!previous && rejected.size >= 256)
        rejected.delete(rejected.keys().next().value);
      rejected.set(entry.key, { outcome, repeats });
      outcome = { ...outcome, repeats };
      if (repeats >= maximum) {
        stopped = true;
        outcome = {
          ...outcome,
          code: entry?.readBlocked
            ? "repeated_read_failure"
            : "repeated_invalid_call",
        };
      }
    }
    const now = performance.now();
    outcome = {
      ...outcome,
      call_id: event.toolCallId,
      ...(entry?.parent ? { parent_call_id: entry.parent } : {}),
      definition_version: entry?.definition,
      permission_version: permissionVersion,
      duration_ms: event.durationMs,
      validation_ms: entry?.validated
        ? Math.max(0, Math.round(entry.validated - entry.started))
        : undefined,
      waiting_ms:
        entry?.ready && entry?.validated
          ? Math.max(0, Math.round(entry.ready - entry.validated))
          : undefined,
      result_processing_ms:
        entry?.ready && event.durationMs !== undefined
          ? Math.max(0, Math.round(now - entry.ready - event.durationMs))
          : undefined,
      preparation_ms: entry
        ? Math.max(0, Math.round(now - entry.started - (event.durationMs || 0)))
        : undefined,
    };
    event.result.details = { ...event.result.details, tool_outcome: outcome };
    if (event.isError)
      event.result.content = [
        {
          type: "text",
          text: `[${outcome.code}; ${outcome.dispatch}; next=${outcome.next_action}] ${outcome.message}`,
        },
        ...(event.result.content || []),
      ];
    ctx.ui.notify(
      "__assistant_tool_outcome__:" + JSON.stringify(outcome),
      "info",
    );
    calls.delete(event.toolCallId);
    if (stopped) {
      ctx.ui.notify(
        "__assistant_tool_stop__:" +
          JSON.stringify({
            code: outcome.code,
            message: outcome.message,
          }),
        "warning",
      );
      ctx.abort();
    }
  });
  return {
    ready: (event: any) => {
      const entry = calls.get(event.toolCallId);
      if (entry) entry.ready = performance.now();
    },
  };
}
