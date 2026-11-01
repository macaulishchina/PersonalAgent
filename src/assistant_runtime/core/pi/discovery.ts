/** Pi owns execution and ranking; presentation never grants or revokes authority. */
import { createToolSearchExtension } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

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
const shortName = (tool: any) =>
  tool.originalName || tool.name.slice(5).split("__").slice(1).join("__");
const namespaces = (tool: any) => [
  tool.originalServer,
  tool.name.slice(5).split("__")[0],
  tool.namespace?.name,
  ...(tool.namespace?.aliases || []),
];
const declaration = (tool: any) => ({
  name: tool.name,
  description: tool.description,
  parameters: tool.parameters,
});

function exactMatches(query: string, tools: any[]) {
  const tokens = query.match(/[A-Za-z0-9_-]+(?:[.:/][A-Za-z0-9_-]+)?/g) || [];
  const found = new Map<string, any>();
  const ambiguous = new Set<string>();
  let explicit = false;
  for (const token of tokens) {
    const qualified = token.match(/^([^.:/]+)[.:/]([^.:/]+)$/);
    const matches = tools.filter((tool) =>
      qualified
        ? shortName(tool) === qualified[2] &&
          namespaces(tool).includes(qualified[1])
        : token.startsWith("mcp__")
          ? tool.name === token
          : shortName(tool) === token,
    );
    if (qualified || token.startsWith("mcp__") || matches.length)
      explicit = true;
    const scoped = matches.filter((tool) =>
      namespaces(tool).some((name) => tokens.includes(name)),
    );
    const selected = scoped.length ? scoped : matches;
    if (selected.length > 1)
      selected.forEach((tool) => ambiguous.add(tool.name));
    else selected.forEach((tool) => found.set(tool.name, tool));
  }
  return { explicit, tools: [...found.values()], ambiguous: [...ambiguous] };
}

export function registerDiscovery(
  pi: any,
  allowed: string[] | undefined,
  enabled: boolean,
  limit: number,
  stableExecution = false,
  byteLimit = 65536,
  origin: (name: string) => { server: string; tool: string } | undefined = () =>
    undefined,
) {
  const catalog = () =>
    pi
      .getAllTools()
      .filter(
        (tool: any) =>
          tool.name.startsWith("mcp__") &&
          tool.exposure !== "hidden" &&
          (!Array.isArray(allowed) || allowed.includes(tool.name)),
      )
      .map((tool: any) => ({
        ...tool,
        originalName: origin(tool.name)?.tool,
        originalServer: origin(tool.name)?.server,
      }));
  const visible = new Set<string>();
  const used = new Map<string, number>();
  let tick = 0;
  let prepare = false;
  const base = () =>
    (Array.isArray(allowed) ? allowed : pi.getActiveTools()).filter(
      (name: string) => !name.startsWith("mcp__") && name !== "tool_search",
    );
  const sync = () =>
    pi.setActiveTools([
      ...base(),
      ...(enabled ? ["tool_search", ...catalog().map((t: any) => t.name)] : []),
    ]);
  const select = (names: string[]) => {
    for (const name of names) {
      visible.add(name);
      used.set(name, ++tick);
    }
    sync();
  };
  // Search deals with model-visible definitions, not Pi's callable set.
  const native = nativeSearch({
    ...pi,
    getAllTools: () =>
      catalog().map((t: any) => ({ ...t, exposure: "deferred" })),
    getActiveTools: () => [],
    setActiveTools: select,
  });
  pi.registerTool({
    ...native,
    parameters: Type.Object({
      query: Type.String({
        description:
          "Exact names, qualified names or metadata keywords. Empty with namespace lists that source.",
      }),
      namespace: Type.Optional(Type.String()),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: limit })),
    }),
    description:
      "Search tool metadata and select current definitions. Known registered and authorized exact tool names can be called directly. Search to learn parameters; multiple distinct names are supported. Qualify shared short names. Use namespace to browse a source. Discovery never executes business actions.",
    reachesTools: (input: any) => {
      if (input.namespace)
        return [
          "mcp__" +
            input.namespace
              .replace(/^mcp__/, "")
              .replace(/[^A-Za-z0-9_]/g, "_"),
        ];
      const words =
        input.query.match(/[A-Za-z0-9_-]+(?:[.:/][A-Za-z0-9_-]+)?/g) || [];
      const scoped = words.filter(
        (word: string) => word.startsWith("mcp__") || /[.:/]/.test(word),
      );
      return scoped.length
        ? scoped.map((word: string) =>
            word.startsWith("mcp__")
              ? word
              : "mcp__" + word.split(/[.:/]/)[0].replace(/[^A-Za-z0-9_]/g, "_"),
          )
        : "all";
    },
    prepareLoadout: () => ({
      hiddenDeclarations: catalog()
        .filter((t: any) => !visible.has(t.name))
        .map((t: any) => t.name),
    }),
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
      const count = input.limit ?? Math.min(8, limit);
      if (!Number.isInteger(count) || count < 1 || count > limit)
        throw new Error(`Search limit must be between 1 and ${limit}`);
      if (signal?.aborted) throw new Error("Tool discovery cancelled");
      const candidates = catalog().filter(
        (t: any) => !input.namespace || namespaces(t).includes(input.namespace),
      );
      const exact = exactMatches(input.query, candidates);
      let selected = exact.tools;
      let mode = "exact";
      if (exact.ambiguous.length && !selected.length)
        return {
          content: [
            {
              type: "text",
              text:
                "Tool name is ambiguous; qualify the source: " +
                exact.ambiguous.slice(0, count).join(", "),
            },
          ],
          details: {
            loaded: [],
            discovery: "ambiguous",
            candidates: exact.ambiguous.slice(0, count),
          },
        };
      if (!exact.explicit) {
        if (input.namespace && !input.query.trim()) {
          selected = candidates.slice(0, count);
          mode = "namespace";
        } else {
          const scoped = nativeSearch({
            ...pi,
            getAllTools: () =>
              candidates.map((t: any) => ({ ...t, exposure: "deferred" })),
            getActiveTools: () => [],
            setActiveTools: select,
          });
          const result = await scoped.execute(
            id,
            { query: input.query, limit: count },
            signal,
            update,
            ctx,
          );
          selected = candidates.filter((t: any) =>
            result.details?.loaded?.includes(t.name),
          );
          mode = "metadata";
        }
      }
      if (selected.length > count)
        throw new Error("Too many exact tools; use a focused query");
      const names = selected.map((t: any) => t.name);
      select(names);
      if (!names.length) mode = "unavailable";
      const sources = [
        ...new Set(
          catalog().map(
            (t: any) => t.originalServer || t.name.slice(5).split("__")[0],
          ),
        ),
      ]
        .sort()
        .slice(0, count);
      const text = names.length
        ? "Definitions selected for your next model request:\n" +
          selected
            .map(
              (t: any) =>
                `- ${t.name}: ${t.description.trim().split(/\r?\n/)[0]}`,
            )
            .join("\n") +
          (exact.ambiguous.length
            ? "\nOther requested names are ambiguous; qualify the source: " +
              exact.ambiguous.slice(0, count).join(", ")
            : "")
        : "No match in the current ready, authorized catalog. This is not proof that a connecting service has no tools. Use an exact name or browse a namespace: " +
          sources.join(", ");
      ctx.ui.notify(
        "__assistant_discovery__:" + JSON.stringify({ mode, tools: names }),
        "info",
      );
      return {
        content: [{ type: "text", text }],
        details: {
          loaded: names,
          discovery: mode,
          namespaces: sources,
          ambiguous: exact.ambiguous.slice(0, count),
        },
      };
    },
  });
  pi.on("session_start", () => {
    visible.clear();
    used.clear();
    tick = 0;
    prepare = stableExecution;
    sync();
  });
  pi.on("before_agent_start", sync);
  pi.on("tool_result", (event: any) => {
    if (event.toolName.startsWith("mcp__")) select([event.toolName]);
  });
  pi.on("tool_execution_end", (event: any) => {
    if (event.isError && catalog().some((t: any) => t.name === event.toolName))
      select([event.toolName]);
  });
  pi.on("context_with_system", (event: any, ctx: any) => {
    const available = catalog();
    const byName = new Map<string, any>(available.map((t: any) => [t.name, t]));
    if (prepare) {
      prepare = false;
      const explicit = (allowed || []).filter((n) => n.startsWith("mcp__"));
      if (
        explicit.length > 0 &&
        explicit.length <= Math.min(4, limit) &&
        explicit.every((n) => byName.has(n)) &&
        Buffer.byteLength(
          JSON.stringify(explicit.map((n) => declaration(byName.get(n)))),
        ) <= 16384
      )
        select(explicit);
    }
    const ranked = [...visible]
      .filter((n) => byName.has(n))
      .sort((a, b) => (used.get(b) || 0) - (used.get(a) || 0));
    const keep: string[] = [];
    let bytes = 2;
    for (const name of ranked) {
      const size =
        Buffer.byteLength(JSON.stringify(declaration(byName.get(name)))) + 1;
      if (keep.length >= limit || (keep.length > 0 && bytes + size > byteLimit))
        continue;
      keep.push(name);
      bytes += size;
      if (bytes > byteLimit) break; // One oversized schema is shown intact, alone.
    }
    const evicted = [...visible].filter((n) => !keep.includes(n));
    visible.clear();
    keep.forEach((n) => visible.add(n));
    for (const name of used.keys()) if (!visible.has(name)) used.delete(name);
    sync();
    const definitions = pi
      .getAllTools()
      .filter(
        (t: any) =>
          pi.getActiveTools().includes(t.name) &&
          (!t.name.startsWith("mcp__") || visible.has(t.name)),
      )
      .sort((a: any, b: any) => a.name.localeCompare(b.name))
      .map(declaration);
    ctx.ui.notify(
      "__assistant_tools__:" +
        JSON.stringify({
          active: definitions.map((t: any) => t.name),
          callable: available.length,
          evicted,
          catalog: pi.getAllTools().length,
          schema_chars: JSON.stringify(definitions).length,
          schema_bytes: bytes,
          oversized: bytes > byteLimit,
        }),
      "info",
    );
    // Replace historical declarations with the current registry version; retain all business evidence.
    let declared = false;
    const messages = event.messages.map((message: any) => {
      if (message.role !== "system") return message;
      const { toolsAdded, toolsRemoved, ...rest } = message;
      if (stableExecution && rest.sections?.cwd)
        rest.sections = { ...rest.sections, cwd: "<cwd>\n.\n</cwd>" };
      if (declared) return rest;
      declared = true;
      return { ...rest, toolsAdded: definitions };
    });
    return { messages };
  });
}
