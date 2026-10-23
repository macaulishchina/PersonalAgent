/** Bound the native Pi loadout; ranking/discovery stays in Pi's tool_search. */
export function registerDiscovery(
  pi: any,
  allowed: string[] | undefined,
  enabled: boolean,
  limit: number,
) {
  let tick = 0;
  const used = new Map<string, number>();
  const base = () =>
    (Array.isArray(allowed) ? allowed : pi.getActiveTools()).filter(
      (name: string) => !name.startsWith("mcp__") && name !== "tool_search",
    );
  const reset = () => {
    used.clear();
    tick = 0;
    pi.setActiveTools([...base(), ...(enabled ? ["tool_search"] : [])]);
  };
  pi.on("session_start", reset);
  pi.on("before_agent_start", reset);
  pi.on("tool_result", async (event: any) => {
    if (event.toolName === "tool_search") {
      for (const name of event.details?.loaded || []) used.set(name, ++tick);
    } else if (event.toolName.startsWith("mcp__"))
      used.set(event.toolName, ++tick);
  });
  // Runs after the preceding tool batch settles, before the next model call.
  // Never deactivate tools while parallel business operations are in flight.
  pi.on("context_with_system", async (event: any, ctx: any) => {
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
      .filter((tool: any) => selected.includes(tool.name));
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
