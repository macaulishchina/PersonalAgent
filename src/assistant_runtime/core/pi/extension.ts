import { registerDiscovery } from "./discovery.ts";
import { Type } from "typebox";
import { readFileSync, mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
import {
  createBashToolDefinition,
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
    return JSON.parse(raw) as PolicySpec;
  } catch {
    return {};
  }
}

export default function (pi: any) {
  if (process.platform === "win32") {
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
  const discoveryEnabled = process.env.ASSISTANT_PI_DISCOVERY === "1";
  registerDiscovery(
    pi,
    policy.allowed_tools ?? undefined,
    discoveryEnabled,
    Number(process.env.ASSISTANT_MAX_LOADED_TOOLS || 12),
  );
  const resultLimit = Number(
    process.env.ASSISTANT_TOOL_RESULT_MAX_CHARS || 6000,
  );
  // Keep full received text on disk; model context receives a bounded preview.
  // Preserve image blocks and error status, and never turn an executed write into a retry.
  pi.on("tool_result", async (event: any, ctx: any) => {
    // A reference is useful only when this execution can read the saved evidence.
    if (
      !pi
        .getActiveTools()
        .some((name: string) => ["read", "bash", "powershell"].includes(name))
    )
      return;
    const texts = event.content.filter((block: any) => block.type === "text");
    const text = texts.map((block: any) => block.text).join("\n");
    const structured =
      event.structuredContent === undefined
        ? ""
        : JSON.stringify(event.structuredContent);
    if (text.length + structured.length <= resultLimit) return;
    try {
      const directory = join(ctx.cwd, "tool-results");
      mkdirSync(directory, { recursive: true, mode: 0o700 });
      const path = join(directory, randomUUID() + ".json");
      writeFileSync(
        path,
        JSON.stringify(
          {
            tool: event.toolName,
            call_id: event.toolCallId,
            is_error: event.isError,
            content: texts,
            structured_content: event.structuredContent,
          },
          null,
          2,
        ),
        { mode: 0o600, flag: "wx" },
      );
      return {
        content: [
          {
            type: "text",
            text:
              `[Partial tool result preview; is_error=${event.isError === true}; received text stored at ${path}. Omitted items are not evidence of absence or success. Read selected ranges or parse this file for needed fields; do not repeat the operation to recover its output. Upstream truncation, if any, is unchanged.]\n` +
              (text || structured).slice(0, resultLimit),
          },
          ...event.content.filter((block: any) => block.type !== "text"),
        ],
        isError: event.isError,
      };
    } catch {
      // Storage failure must not hide evidence or misreport a tool's business outcome.
      return;
    }
  });
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
                  Authorization:
                    "Bearer " + process.env.PA_EXECUTION_TOKEN,
                  ...provider.headers,
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
                    if (["dispatch", "response"].includes(event.type)) continue;
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
            data: { questions: params.questions },
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

  pi.on("tool_call", async (event: any, ctx: any) => {
    const name: string = event.toolName || "";
    if (
      Array.isArray(policy.allowed_tools) &&
      !policy.allowed_tools.includes(name) &&
      !(name === "tool_search" && discoveryEnabled)
    ) {
      return {
        block: true,
        reason: `Tool ${name} is outside this execution scope`,
      };
    }
    if (name.startsWith("mcp__")) {
      const rest = name.slice(5);
      const sep = rest.indexOf("__");
      if (sep > 0) {
        const server = rest.slice(0, sep);
        const tool = rest.slice(sep + 2);
        const serverPolicy = policy.mcp_policies?.[server];
        if (serverPolicy) {
          const denied = new Set(serverPolicy.deny_tools || []);
          const allowed = serverPolicy.allow_tools;
          const toolApproval =
            serverPolicy.tool_approvals?.[tool] ||
            serverPolicy.approval ||
            "approve";
          if (
            denied.has(tool) ||
            (Array.isArray(allowed) && !allowed.includes(tool)) ||
            toolApproval === "reject"
          ) {
            return {
              block: true,
              reason: `Tool ${tool} is disabled by policy`,
            };
          }
          if (toolApproval === "prompt") {
            const reply = await ctx.ui.input(
              "__assistant_rpc__",
              JSON.stringify({
                kind: "approval.requested",
                data: { server, tool, arguments: event.input },
              }),
            );
            const parsed = reply ? JSON.parse(reply) : {};
            if (parsed?.decision !== "approve") {
              return { block: true, reason: `Tool ${tool} was not approved` };
            }
          }
        }
      }
    }
    return undefined;
  });
}
