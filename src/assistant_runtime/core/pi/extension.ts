import { registerControlled } from "./controlled_tools.ts";
import { registerToolLifecycle } from "./tool_lifecycle.ts";
import { registerResultDelivery } from "./tool_results.ts";
import { registerDiscovery } from "./discovery.ts";
import { Type } from "typebox";
import { readFileSync } from "node:fs";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
import {
  createBashToolDefinition,
  createMcpExtension,
  createPowerShellToolDefinition,
} from "@earendil-works/pi-coding-agent";
import { windowsShellOperations } from "../../platforms/windows_shell.mjs";

interface PolicySpec {
  allowed_tools?: string[] | null;
  sandbox?: string;
  shell_enabled?: boolean;
  edit_enabled?: boolean;
  mcp_policies?: Record<
    string,
    {
      approval?: string;
      allow_tools?: string[] | null;
      deny_tools?: string[];
      tool_approvals?: Record<string, string>;
    }
  >;
}

function loadPolicy(): PolicySpec {
  const raw = process.env.ASSISTANT_PI_POLICY;
  if (!raw) return {};
  try {
    const value = JSON.parse(raw);
    if (!value || typeof value !== "object" || Array.isArray(value))
      throw new Error("Invalid policy");
    if (
      value.allowed_tools != null &&
      (!Array.isArray(value.allowed_tools) ||
        value.allowed_tools.some((n: any) => typeof n !== "string"))
    )
      throw new Error("Invalid allowed tools");
    const record = (v: any) => v && typeof v === "object" && !Array.isArray(v);
    const names = (v: any) =>
      Array.isArray(v) && v.every((n: any) => typeof n === "string");
    if (value.mcp_policies != null && !record(value.mcp_policies))
      throw new Error("Invalid MCP policies");
    for (const rule of Object.values(value.mcp_policies || {}) as any[]) {
      if (
        !record(rule) ||
        ![undefined, "approve", "reject"].includes(rule.approval) ||
        (rule.allow_tools != null && !names(rule.allow_tools)) ||
        (rule.deny_tools != null && !names(rule.deny_tools)) ||
        (rule.tool_approvals != null && !record(rule.tool_approvals)) ||
        Object.values(rule.tool_approvals || {}).some(
          (v) => !["approve", "reject"].includes(v as string),
        )
      )
        throw new Error("Invalid MCP policy");
    }
    return value as PolicySpec;
  } catch {
    throw new Error("Invalid execution tool policy; refusing to start");
  }
}

export default function (pi: any) {
  registerControlled(pi);
  if (process.platform === "win32" && !process.env.ASSISTANT_CONTROL_TOKEN) {
    pi.registerTool(
      createPowerShellToolDefinition(process.cwd(), {
        operations: windowsShellOperations("powershell"),
      }),
    );
    pi.registerTool(
      createBashToolDefinition(process.cwd(), {
        operations: windowsShellOperations("bash"),
      }),
    );
  }
  const policy = loadPolicy();
  const internal = JSON.parse(
    process.env.ASSISTANT_INTERNAL_MCP_SERVERS || "[]",
  );
  // Pi assigns canonical names (including collision suffixes). Keep its original
  // registration identity; never reverse-guess it from the sanitized name.
  const origins = new Map<
    string,
    { server: string; tool: string; readOnly: boolean }
  >();
  const origin = (name: string) => origins.get(name);
  const lifecycle = registerToolLifecycle(
    pi,
    policy,
    Number(process.env.ASSISTANT_INVALID_TOOL_CALL_LIMIT || 3),
    internal,
    origin,
    Number(process.env.ASSISTANT_TOOL_READ_ATTEMPT_LIMIT || 3),
  );
  if (!process.env.ASSISTANT_CONTROL_TOKEN) {
    createMcpExtension()({
      ...pi,
      registerTool: (tool: any) => {
        const slash = tool.label?.indexOf("/") ?? -1;
        const server =
          tool.name.startsWith("mcp__") && slash > 0
            ? tool.label.slice(0, slash)
            : undefined;
        if (server)
          origins.set(tool.name, {
            server,
            tool: tool.label.slice(slash + 1),
            readOnly:
              tool.annotations?.readOnlyHint === true &&
              tool.annotations?.destructiveHint === false,
          });
        pi.registerTool({
          ...tool,
          executionMode:
            internal.includes(server) && tool.annotations?.readOnlyHint === true
              ? "parallel"
              : "sequential",
        });
      },
    });
  }
  const discoveryEnabled = process.env.ASSISTANT_PI_DISCOVERY === "1";
  registerDiscovery(
    pi,
    policy.allowed_tools ?? undefined,
    discoveryEnabled,
    Number(process.env.ASSISTANT_MAX_LOADED_TOOLS || 12),
    process.env.ASSISTANT_STABLE_EXECUTION_PROMPT === "1",
    Number(process.env.ASSISTANT_TOOL_SCHEMA_BYTES || 65536),
    origin,
  );
  registerResultDelivery(pi, policy.allowed_tools);
  const modelPath = process.env.ASSISTANT_PI_MODELS;
  if (modelPath) {
    const provider = JSON.parse(readFileSync(modelPath, "utf8")).providers
      .assistant_api;
    if (provider.models.some((m: any) => m.api === "assistant-native")) {
      pi.registerProvider("assistant_api", {
        ...provider,
        api: "assistant-native",
        streamSimple: (model: any, context: any, options: any) => {
          const stream = createAssistantMessageEventStream();
          void (async () => {
            let terminal = false;
            try {
              const initial = {
                model: model.id,
                context,
                effort:
                  options.reasoning === "off" || !options.reasoning
                    ? "none"
                    : options.reasoning,
              };
              const payload =
                (await options.onPayload?.(initial, model)) ?? initial;
              const response = await fetch(provider.baseUrl + "/native", {
                method: "POST",
                signal: options.signal,
                headers: {
                  "Content-Type": "application/json",
                  Authorization: "Bearer " + process.env.PA_EXECUTION_TOKEN,
                  ...provider.headers,
                  "x-assistant-control":
                    process.env.ASSISTANT_CONTROL_TOKEN || "",
                },
                body: JSON.stringify(payload),
              });
              await options.onResponse?.(
                {
                  status: response.status,
                  headers: Object.fromEntries(response.headers),
                },
                model,
              );
              if (!response.ok || !response.body)
                throw new Error(`native_transport_http_${response.status}`);
              let buffer = "";
              const reader = response.body
                .pipeThrough(new TextDecoderStream())
                .getReader();
              try {
                while (true) {
                  const next = await reader.read();
                  if (next.done) break;
                  buffer += next.value;
                  if (buffer.length > 8 * 1024 * 1024)
                    throw new Error("native_frame_too_large");
                  let end;
                  while ((end = buffer.indexOf("\n")) !== -1) {
                    const event = JSON.parse(buffer.slice(0, end));
                    buffer = buffer.slice(end + 1);
                    if (["dispatch", "response", "retry"].includes(event.type))
                      continue;
                    for (const key of ["partial", "message", "error"]) {
                      if (event[key])
                        Object.assign(event[key], {
                          provider: model.provider,
                          model: model.id,
                          api: model.api,
                        });
                    }
                    if (event.type === "done" || event.type === "error")
                      terminal = true;
                    stream.push(event);
                  }
                }
              } finally {
                await reader.cancel();
              }
              if (!terminal) throw new Error("native_stream_outcome_unknown");
            } catch {
              if (!terminal)
                stream.push({
                  type: "error",
                  reason: options.signal?.aborted ? "aborted" : "error",
                  error: {
                    role: "assistant",
                    content: [],
                    api: model.api,
                    provider: model.provider,
                    model: model.id,
                    stopReason: options.signal?.aborted ? "aborted" : "error",
                    errorMessage: "Native model transport did not complete",
                    timestamp: Date.now(),
                    usage: {
                      input: 0,
                      output: 0,
                      cacheRead: 0,
                      cacheWrite: 0,
                      totalTokens: 0,
                      cost: {
                        input: 0,
                        output: 0,
                        cacheRead: 0,
                        cacheWrite: 0,
                        total: 0,
                      },
                    },
                  },
                });
            } finally {
              stream.end();
            }
          })();
          return stream;
        },
      });
    }
  }

  pi.on("session_before_compact", async (_event: any, ctx: any) => {
    const reply = await ctx.ui.input("__assistant_compaction__", "");
    if (!reply || JSON.parse(reply).allowed !== true) {
      ctx.abort();
      return { cancel: true };
    }
  });

  if (process.env.ASSISTANT_PI_ENABLE_USER_INPUT === "1") {
    pi.registerTool({
      name: "request_user_input",
      label: "Request User Input",
      description:
        "Suspend execution and ask the user one or more clarifying questions when required to proceed.",
      parameters: Type.Object({
        questions: Type.Array(
          Type.Object({
            id: Type.String(),
            header: Type.Optional(Type.String()),
            question: Type.String(),
          }),
          { minItems: 1 },
        ),
      }),
      execute: async (
        _id: string,
        params: any,
        _signal: any,
        _onUpdate: any,
        ctx: any,
      ) => {
        const reply = await ctx.ui.input(
          "__assistant_rpc__",
          JSON.stringify({
            kind: "input.requested",
            data: { questions: params.questions, tool_call_id: _id },
          }),
        );
        if (!reply) {
          throw new Error("User input request was cancelled");
        }
        return {
          content: [{ type: "text", text: reply }],
          details: {},
        };
      },
    });
  }

  if (process.env.ASSISTANT_PI_ENABLE_GOAL === "1") {
    pi.registerTool({
      name: "update_task_goal",
      label: "Update Task Goal",
      description:
        "Report progress to the task owner. The framework owns goal completion.",
      parameters: Type.Object({
        status: Type.Union([
          Type.Literal("active"),
          Type.Literal("paused"),
          Type.Literal("complete"),
        ]),
        objective: Type.Optional(Type.String()),
        summary: Type.Optional(Type.String()),
      }),
      execute: async (
        _id: string,
        params: any,
        _signal: any,
        _onUpdate: any,
        ctx: any,
      ) => {
        const goal = {
          status: params.status,
          ...(params.objective ? { objective: params.objective } : {}),
          ...(params.summary ? { summary: params.summary } : {}),
        };
        pi.appendEntry("task_goal", goal);
        ctx.ui.notify("__assistant_goal__:" + JSON.stringify(goal), "info");
        return {
          content: [{ type: "text", text: JSON.stringify(goal) }],
          details: goal,
        };
      },
    });
  }

  pi.registerCommand("inject_context", {
    description: "Inject confirmed context messages without starting a turn",
    handler: async (args: string, _ctx: any) => {
      if (!args?.trim()) return;
      const items = JSON.parse(
        Buffer.from(args.trim(), "base64").toString("utf-8"),
      );
      if (!Array.isArray(items)) return;
      const lines: string[] = [];
      for (const item of items) {
        const role = item.role || "user";
        const content = Array.isArray(item.content)
          ? item.content.map((c: any) => c.text || "").join("")
          : String(item.text || "");
        if (content) {
          lines.push(`[${role}] ${content}`);
        }
      }
      if (lines.length > 0) {
        await pi.sendMessage(
          {
            customType: "injected_context",
            content: lines.join("\n\n"),
            display: false,
            details: { count: lines.length },
          },
          { triggerTurn: false },
        );
      }
    },
  });

  pi.registerCommand("goal_set", {
    description: "Set or update the session task goal",
    handler: async (args: string, ctx: any) => {
      if (!args?.trim()) return;
      const goal = JSON.parse(
        Buffer.from(args.trim(), "base64").toString("utf-8"),
      );
      pi.appendEntry("task_goal", goal);
      ctx.ui.notify("__assistant_goal__:" + JSON.stringify(goal), "info");
    },
  });

  pi.registerCommand("assistant_tool_contract_v1", {
    description: "Internal tool contract readiness marker",
    handler: async () => {},
  });

  pi.on("tool_call", lifecycle.ready);
}
