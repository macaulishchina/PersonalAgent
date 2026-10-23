import { Type } from "typebox";
import { readFileSync } from "node:fs";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";

interface PolicySpec {
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
  const policy = loadPolicy();
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
                    "Bearer " + process.env.ASSISTANT_MODEL_GATEWAY_KEY,
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
