/** Exact identity lookup around Pi's native metadata search and bounded loadout. */
import { createToolSearchExtension } from "@earendil-works/pi-coding-agent";

function nativeSearch(pi: any) {
  let definition: any;
  createToolSearchExtension()({
    ...pi,
    registerTool: (tool: any) => {
      definition = tool;
    },
  });
  return definition;
}

function exactMatches(query: string, tools: any[]) {
  const shortName = (tool: any) =>
    tool.name.startsWith("mcp__")
      ? tool.name.slice(5).split("__").slice(1).join("__")
      : tool.name;
  const namespaces = (tool: any) => [
    tool.name.startsWith("mcp__")
      ? tool.name.slice(5).split("__")[0]
      : undefined,
    tool.namespace?.name,
    ...(tool.namespace?.aliases || []),
  ];
  const qualifiedName = query
    .trim()
    .match(/^([A-Za-z][A-Za-z0-9_-]*)[.:/]([A-Za-z][A-Za-z0-9_-]+)$/);
  if (qualifiedName)
    return {
      explicit: true,
      tools: tools.filter(
        (tool) =>
          shortName(tool) === qualifiedName[2] &&
          namespaces(tool).includes(qualifiedName[1]),
      ),
    };
  const words = new Set(query.match(/[A-Za-z0-9_-]+/g) || []);
  const full = [...words].filter((word) => word.startsWith("mcp__"));
  if (full.length)
    return {
      explicit: true,
      tools: tools.filter((tool) => words.has(tool.name)),
    };
  const matches = tools.filter((tool) => {
    const short = shortName(tool);
    return words.has(short) && (short.includes("_") || query.trim() === short);
  });
  // A namespace qualifies a short name when multiple servers export it.
  const qualified = matches.filter((tool) =>
    namespaces(tool).some((name) => words.has(name)),
  );
  return {
    explicit: matches.length > 0,
    tools: qualified.length ? qualified : matches,
  };
}

export function registerDiscovery(
  pi: any,
  allowed: string[] | undefined,
  enabled: boolean,
  limit: number,
  stableExecution = false,
) {
  const searchable = () =>
    pi
      .getAllTools()
      .filter(
        (tool: any) =>
          ["deferred", "codemode", ...(stableExecution ? ["direct"] : [])].includes(tool.exposure) &&
          (!Array.isArray(allowed) || allowed.includes(tool.name)),
      );
  const native = nativeSearch({ ...pi, getAllTools: searchable });
  const report = (ctx: any, mode: string, names: string[]) =>
    ctx.ui.notify(
      "__assistant_discovery__:" + JSON.stringify({ mode, tools: names }),
      "info",
    );
  pi.registerTool({
    ...native,
    description:
      native.description +
      "\nWhen a tool name is known, query its exact full name (mcp__server__tool) or qualified name (server.tool_name). Exact names take priority, including already loaded tools. A short name shared by multiple servers requires qualification. Loading never executes an operation.",
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
    async execute(id: string, input: any, signal: any, update: any, ctx: any) {
      if (!enabled)
        throw new Error(
          "Tool discovery is unavailable in this execution scope",
        );
      const count = input.limit ?? 8;
      if (!Number.isInteger(count) || count < 1 || count > limit)
        throw new Error(`Search limit must be between 1 and ${limit}`);
      if (signal?.aborted) throw new Error("Tool discovery cancelled");
      const exact = exactMatches(input.query, searchable());
      if (!exact.explicit) {
        const result = await native.execute(id, input, signal, update, ctx);
        report(ctx, "metadata", result.details?.loaded || []);
        return result;
      }
      const names = exact.tools.map((tool: any) => tool.name);
      const full = input.query.trim().startsWith("mcp__");
      if (names.length > 1 && !full) {
        report(ctx, "ambiguous", names.slice(0, count));
        return {
          content: [
            {
              type: "text",
              text:
                "Tool name is ambiguous. Search an exact full name:\n" +
                names.slice(0, count).join("\n"),
            },
          ],
          details: {
            loaded: [],
            discovery: "ambiguous",
            candidates: names.slice(0, count),
          },
        };
      }
      if (names.length > count)
        throw new Error("Too many exact tools; use a focused query");
      pi.setActiveTools([...pi.getActiveTools(), ...names]);
      report(ctx, names.length ? "exact" : "unavailable", names);
      return {
        content: [
          {
            type: "text",
            text: names.length
              ? "Tools are loaded and available from your next call:\n" +
                exact.tools
                  .map(
                    (tool: any) =>
                      `- ${tool.name}: ${tool.description.trim().split(/\r?\n/)[0]}`,
                  )
                  .join("\n")
              : "No available tool matches this exact name in the current execution scope. Nothing was executed.",
          },
        ],
        details: {
          loaded: names,
          discovery: names.length ? "exact" : "unavailable",
        },
      };
    },
  });
  const explicit = (allowed || []).filter((name) => name.startsWith("mcp__"));
  let prepare = false;
  let tick = 0;
  const used = new Map<string, number>();
  const base = () =>
    (Array.isArray(allowed) ? allowed : pi.getActiveTools()).filter(
      (name: string) => !name.startsWith("mcp__") && name !== "tool_search",
    );
  const reset = () => {
    used.clear();
    prepare = stableExecution && enabled && explicit.length > 0 && explicit.length <= Math.min(4, limit);
    tick = 0;
    pi.setActiveTools([...base(), ...(enabled ? ["tool_search"] : [])]);
  };
  pi.on("session_start", reset);
  pi.on("before_agent_start", () => {
    // Reuse a bounded warm loadout in this live session, but drop removed or
    // newly hidden identities before the model sees them. Resume starts cold.
    const available = new Set(searchable().map((tool: any) => tool.name));
    const warm = enabled
      ? pi
          .getActiveTools()
          .filter(
            (name: string) => name.startsWith("mcp__") && available.has(name),
          )
      : [];
    pi.setActiveTools([
      ...base(),
      ...(enabled ? ["tool_search"] : []),
      ...warm,
    ]);
  });
  pi.on("tool_result", async (event: any) => {
    if (event.toolName === "tool_search") {
      for (const name of event.details?.loaded || []) used.set(name, ++tick);
    } else if (event.toolName.startsWith("mcp__"))
      used.set(event.toolName, ++tick);
  });
  pi.on("message_end", async (event: any, ctx: any) => {
    // A deferred direct call was rejected before execution. Load only that known,
    // permitted identity; never execute/replay the business operation here.
    const message = event.message;
    if (
      !enabled ||
      message.role !== "toolResult" ||
      !message.isError ||
      message.durationMs !== undefined ||
      !message.toolName?.startsWith("mcp__") ||
      pi.getActiveTools().includes(message.toolName) ||
      message.content?.length !== 1 ||
      message.content[0]?.type !== "text" ||
      message.content[0].text !== `Tool ${message.toolName} not found`
    )
      return;
    const loaded = searchable().some(
      (tool: any) => tool.name === message.toolName,
    );
    if (loaded) {
      pi.setActiveTools([...pi.getActiveTools(), message.toolName]);
      used.set(message.toolName, ++tick);
    }
    report(
      ctx,
      loaded ? "load_after_not_found" : "unavailable",
      loaded ? [message.toolName] : [],
    );
    return {
      message: {
        ...message,
        content: [
          {
            type: "text",
            text:
              `Tool ${message.toolName} was not found in the active loadout, so this call did not execute. ` +
              (loaded
                ? "It is now loaded for your next call. "
                : `Use tool_search with query="${message.toolName}" to wait for discovery and check availability in this scope. `) +
              "Submit the intended call once after loading, preserving its request ID if present; do not repeat any other completed or unknown operation.",
          },
        ],
        details: {
          ...message.details,
          discovery: loaded
            ? "loaded_not_executed"
            : "unavailable_not_executed",
          loaded: loaded ? [message.toolName] : [],
        },
      },
    };
  });
  // Runs after the preceding tool batch settles, before the next model call.
  // Never deactivate tools while parallel business operations are in flight.
  pi.on("context_with_system", async (event: any, ctx: any) => {
    if (prepare) {
      prepare = false;
      const candidates = searchable().filter((tool: any) => explicit.includes(tool.name))
        .sort((a: any, b: any) => a.name.localeCompare(b.name));
      const chars = Buffer.byteLength(JSON.stringify(candidates.map((tool: any) => ({
        name: tool.name, description: tool.description, parameters: tool.parameters,
      }))));
      const ready = candidates.length === explicit.length && chars <= 16384;
      pi.setActiveTools([
        ...pi.getActiveTools().filter((name: string) => !name.startsWith("mcp__")),
        ...(ready ? candidates.map((tool: any) => tool.name) : []),
      ]);
      report(ctx, ready ? "preloaded" : "preload_deferred", ready ? explicit : []);
    }
    const active: string[] = pi.getActiveTools();
    const loaded = active.filter((name) => name.startsWith("mcp__"));
    const ranked = [...loaded].sort(
      (a, b) => (used.get(b) || 0) - (used.get(a) || 0),
    );
    const keep = new Set(ranked.slice(0, limit));
    const evicted = loaded.filter((name) => !keep.has(name));
    if (evicted.length)
      pi.setActiveTools(
        active.filter((name) => !name.startsWith("mcp__") || keep.has(name)),
      );
    for (const name of used.keys()) if (!keep.has(name)) used.delete(name);
    const selected: string[] = pi.getActiveTools();
    const definitions = pi
      .getAllTools()
      .filter((tool: any) => selected.includes(tool.name))
      .sort((a: any, b: any) => a.name.localeCompare(b.name));
    ctx.ui.notify(
      "__assistant_tools__:" +
        JSON.stringify({
          active: selected,
          evicted,
          catalog: pi.getAllTools().length,
          schema_chars: JSON.stringify(
            definitions.map((tool: any) => ({
              name: tool.name,
              description: tool.description,
              parameters: tool.parameters,
            })),
          ).length,
        }),
      "info",
    );
    // Pi records schema changes in its durable transcript. Project only the current
    // loadout into requests, or evicted definitions would still occupy history.
    // Keep every user/tool message and system section; only declarations change.
    const schema = new Map<string, any>();
    for (const message of event.messages)
      for (const tool of message.toolsAdded || []) schema.set(tool.name, tool);
    let declared = false;
    const messages = event.messages.map((message: any) => {
      if (message.role !== "system") return message;
      const { toolsAdded, toolsRemoved, ...rest } = message;
      // A relative cwd is accurate for this scratch-bound execution. Real tool cwd
      // and persisted session metadata remain untouched; no identity is reused.
      if (stableExecution && rest.sections?.cwd)
        rest.sections = { ...rest.sections, cwd: "<cwd>\n.\n</cwd>" };
      if (declared) return rest;
      declared = true;
      return {
        ...rest,
        toolsAdded: definitions.map(
          (tool: any) =>
            schema.get(tool.name) || {
              name: tool.name,
              description: tool.description,
              parameters: tool.parameters,
            },
        ),
      };
    });
    return { messages };
  });
  pi.on("tool_call", async (event: any) => {
    if (event.toolName !== "tool_search") return;
    if (!enabled)
      return {
        block: true,
        reason: "Tool discovery is unavailable in this execution scope",
      };
    const count = event.input.limit ?? 8;
    if (!Number.isInteger(count) || count < 1 || count > limit)
      return {
        block: true,
        reason: `Search limit must be between 1 and ${limit}; use a focused query with an explicit limit`,
      };
  });
}
