"use strict";

// Operator presentation only. Integrations owns policy, merge rules and verification.
let mcpInventory = null,
  mcpSavedNames = new Set(),
  mcpLoad = 0;
const mcpPolicyLabels = {
  approve: "自动允许",
  reject: "禁止调用",
};
const mcpVerificationLabels = {
  verified: "连接已验证",
  auth_required: "需要配置服务鉴权",
  unavailable: "连接不可用",
};
const mcpReasons = {
  allowed: "允许",
  disabled: "服务未启用",
  scope_excluded: "范围未授权",
  execution_excluded: "执行范围外",
  rejected: "服务禁止调用",
  denied: "工具被禁止",
  not_selected: "未选入允许范围",
};
function mcpPatch() {
  return resourceEdits.get("mcp")?.value;
}
function mergeMcpChanges(previous, changes) {
  const value = { ...previous, ...changes };
  if (
    previous?.transport &&
    changes.transport &&
    previous.transport.type === changes.transport.type
  )
    value.transport = { ...previous.transport, ...changes.transport };
  return value;
}
function stageMcp(name, op, changes) {
  const patch = structuredClone(
    mcpPatch() || { revision: mcpInventory.revision, changes: [] },
  );
  const index = patch.changes.findIndex((item) => item.name === name);
  if (op === "remove" && !mcpSavedNames.has(name)) {
    if (index >= 0) patch.changes.splice(index, 1);
  } else {
    const item = {
      name,
      op,
      ...(op === "configure"
        ? { changes: mergeMcpChanges(patch.changes[index]?.changes, changes) }
        : {}),
    };
    if (index >= 0) patch.changes[index] = item;
    else patch.changes.push(item);
  }
  if (patch.changes.length)
    resourceEdits.set("mcp", { id: "mcp", op: "patch", value: patch });
  else resourceEdits.delete("mcp");
  updateSaveBar();
}
function mcpField(parent, label, value = "", choices, type = "text") {
  const row = node("label", undefined, "mcp-field"),
    title = node("span", label);
  const input = node(
    choices ? "select" : type === "textarea" ? "textarea" : "input",
  );
  input.setAttribute("aria-label", label);
  if (choices)
    for (const [key, text] of choices) {
      const option = node("option", text);
      option.value = key;
      input.append(option);
    }
  else if (type !== "textarea") input.type = type;
  if (type === "checkbox") input.checked = Boolean(value);
  else input.value = value ?? "";
  if (type === "password") input.autocomplete = "new-password";
  row.append(title, input);
  parent.append(row);
  return input;
}
function mcpOpaque(parent, title, configured, kind, required = false) {
  const box = node("section", undefined, "mcp-private-field");
  parent.append(box);
  const options = configured
    ? [
        ["keep", "保留已保存值"],
        ["replace", "替换"],
      ]
    : [["replace", "填写"]];
  if (!required) options.push(["clear", "清除"]);
  const mode = mcpField(box, title + "操作", options[0][0], options);
  const input = mcpField(
    box,
    title,
    "",
    null,
    kind === "url" ? "password" : "textarea",
  );
  input.placeholder =
    kind === "array"
      ? '["--flag", "value"]'
      : kind === "object"
        ? '{"NAME": "value"}'
        : "填写完整 HTTP(S) 地址";
  box.append(
    node(
      "small",
      configured
        ? "已保存内容不回显；保留不会覆盖它。"
        : "输入仅保留在草稿，保存后写入私有配置。",
      "hint",
    ),
  );
  function refresh() {
    input.closest("label").hidden = mode.value !== "replace";
  }
  mode.addEventListener("change", refresh);
  refresh();
  return {
    read() {
      if (mode.value === "keep") return undefined;
      if (mode.value === "clear") return kind === "array" ? [] : {};
      if (kind === "url") {
        const value = input.value.trim();
        if (!value) throw Error(title + "不能为空");
        return value;
      }
      const value = JSON.parse(input.value || (kind === "array" ? "[]" : "{}"));
      if (
        kind === "array"
          ? !Array.isArray(value) || value.some((x) => typeof x !== "string")
          : !value ||
            Array.isArray(value) ||
            typeof value !== "object" ||
            Object.values(value).some((x) => typeof x !== "string")
      )
        throw Error(title + "格式不正确");
      return value;
    },
  };
}
function renderPermissions() {
  const panel = surface(
    "第三方 MCP",
    "配置连接和长期授权。新增默认允许全部工具，包括服务未来新增的工具。",
    "mcp-panel",
  );
  const add = button("添加 MCP", () => openMcpEditor(), "primary");
  add.disabled = true;
  panel.querySelector(".card-heading").append(add);
  const content = node("div", undefined, "mcp-services");
  content.append(node("p", "读取配置…", "mcp-loading hint"));
  panel.append(content);
  $("documents").append(panel);
  if (!valueAt(["plugins"], []).includes("integrations"))
    panel.append(
      node(
        "p",
        "外部工具插件尚未启用；可以准备配置，验证和运行需要先启用插件。",
        "inline-note",
      ),
    );
  const advanced = advancedGroup("mcp-runtime-settings");
  advanced.firstChild.textContent = "高级管理设置";
  for (const f of snapshot.fields.filter((f) => f.path[0] === "external_mcp"))
    advanced.append(drawField(prettyField(f)));
  $("documents").append(advanced);
  const requestId = ++mcpLoad,
    version = snapshot.version,
    draft = JSON.stringify(mcpPatch());
  action(async () => {
    try {
      const base = await api(
        "/api/permissions/mcp?version=" + encodeURIComponent(version),
      );
      const data = mcpPatch()
        ? await api("/api/permissions/mcp", { version, patch: mcpPatch() })
        : base;
      if (
        requestId !== mcpLoad ||
        selected !== "permissions" ||
        version !== snapshot.version ||
        draft !== JSON.stringify(mcpPatch())
      )
        return;
      mcpInventory = data;
      mcpSavedNames = new Set(base.servers.map((s) => s.name));
      add.disabled = !data.writable;
      content.replaceChildren();
      const grid = node("tbody");
      if (data.servers.length) {
        const table = node("table", undefined, "mcp-table");
        table.setAttribute("aria-label", "第三方 MCP 服务");
        const columns = node("colgroup");
        for (const column of [
          "identity",
          "enabled",
          "policy",
          "scopes",
          "state",
          "actions",
        ])
          columns.append(node("col", undefined, "mcp-col-" + column));
        const head = node("thead"),
          headings = node("tr");
        for (const label of [
          "服务",
          "启停",
          "调用权限",
          "使用范围",
          "状态",
          "操作",
        ]) {
          const heading = node("th", label);
          heading.scope = "col";
          headings.append(heading);
        }
        head.append(headings);
        table.append(columns, head, grid);
        content.append(table);
      }
      if (!data.writable)
        content.append(
          node("p", "注册表位于实例外，只能在本机维护。", "inline-note"),
        );
      if (!data.servers.length)
        content.append(
          emptyState(
            "尚未配置第三方 MCP",
            "支持本地 stdio 和远程 HTTP；软件安装由操作者完成。",
          ),
        );
      for (const entry of data.servers) {
        const spec = entry.definition,
          t = spec.transport,
          row = node("tr", undefined, "mcp-service");
        row.dataset.name = entry.name;
        const identity = node("div", undefined, "mcp-identity"),
          connection = t.type + " · " + (t.origin || t.command),
          name = node("strong", entry.name),
          summary = node("small", connection, "hint");
        name.title = entry.name;
        summary.title = connection;
        identity.append(name, summary);
        const enabled = button(
          spec.enabled ? "已启用" : "已停用",
          () => {
            stageMcp(entry.name, "configure", { enabled: !spec.enabled });
            render();
          },
          "subtle mcp-enabled",
        );
        enabled.setAttribute("aria-pressed", String(spec.enabled));
        enabled.disabled = !data.writable;
        const approval = node("span", mcpPolicyLabels[spec.approval], "badge");
        const scopes = node("div", undefined, "mcp-scopes");
        for (const scope of spec.contexts)
          scopes.append(
            node("span", scope === "foreground" ? "主对话" : "后台任务"),
          );
        if (!spec.contexts.length) scopes.append(node("span", "无授权范围"));
        const status = node("div", undefined, "mcp-state");
        status.append(
          node(
            "span",
            mcpPatch()?.changes.some((c) => c.name === entry.name)
              ? "未保存"
              : !data.running
                ? "业务未运行"
                : entry.application === "applied"
                  ? "已应用"
                  : "待应用",
            "badge",
          ),
        );
        for (const [context, check] of Object.entries(entry.verification || {}))
          status.append(
            node(
              "small",
              `${context === "foreground" ? "主对话" : "后台"}验证：${check.current ? (check.state === "verified" ? "连接已验证" : "不可用") : "结果已过期"}`,
              "hint",
            ),
          );
        const actions = node("div", undefined, "mcp-row-actions");
        const edit = button("编辑", () => openMcpEditor(entry));
        edit.disabled = !data.writable;
        actions.append(
          edit,
          button("验证／工具", () => openMcpEditor(entry, "tools")),
        );
        const remove = button(
          "移除",
          () => {
            if (
              confirm(`移除 ${entry.name} 的登记？不会卸载软件或删除业务数据。`)
            ) {
              stageMcp(entry.name, "remove");
              render();
            }
          },
          "text-button",
        );
        remove.disabled = !data.writable;
        actions.append(remove);
        for (const cell of [
          identity,
          enabled,
          approval,
          scopes,
          status,
          actions,
        ]) {
          const column = node("td");
          column.append(cell);
          row.append(column);
        }
        grid.append(row);
      }
      if (data.servers.length) {
        const [filter, empty] = collectionFilter(
          grid,
          "搜索 MCP 名称或连接",
          "mcp-services",
        );
        panel.insertBefore(filter, content);
        panel.append(empty);
      }
      panel.append(node("p", data.snapshot_notice, "mcp-snapshot-note hint"));
    } catch (error) {
      if (requestId === mcpLoad && content.isConnected)
        content.replaceChildren(
          node("p", error.message, "inline-note"),
          button("重新读取", () => render()),
        );
    }
  });
}
function openMcpEditor(entry, firstTab = "connection") {
  if (!mcpInventory) return;
  const checkpoint = draftCheckpoint();
  const initial = entry?.definition || {
    enabled: true,
    contexts: ["foreground", "background"],
    approval: "approve",
    allow_tools: null,
    deny_tools: [],
    tool_approvals: {},
    startup_timeout_seconds: 60,
    tool_timeout_seconds: 600,
    transport: { type: "stdio" },
  };
  const spec = structuredClone(initial),
    transport = initial.transport,
    body = $("entry-body");
  $("entry-title").textContent = entry
    ? "编辑 MCP · " + entry.name
    : "添加 MCP";
  $("entry-description").textContent =
    "修改先保留为草稿，统一保存并生效；不会发起模型或设备操作。";
  body.replaceChildren();
  const tabs = node("div", undefined, "page-tabs"),
    sections = {};
  body.append(tabs);
  for (const [id, label] of [
    ["connection", "连接设置"],
    ["policy", "调用权限"],
    ["tools", "工具与验证"],
  ]) {
    const section = node("section", undefined, "mcp-editor-section");
    sections[id] = section;
    body.append(section);
    const tab = button(label, () => choose(id));
    tab.setAttribute("role", "tab");
    tab.dataset.tab = id;
    tabs.append(tab);
  }
  function choose(id) {
    for (const [name, s] of Object.entries(sections)) s.hidden = name !== id;
    for (const tab of tabs.children) {
      tab.classList.toggle("active", tab.dataset.tab === id);
      tab.setAttribute("aria-selected", String(tab.dataset.tab === id));
    }
  }
  const name = mcpField(sections.connection, "服务名称", entry?.name || "");
  name.readOnly = Boolean(entry);
  const type = mcpField(sections.connection, "连接类型", transport.type, [
    ["stdio", "本地 stdio"],
    ["http", "远程 HTTP"],
  ]);
  const stdio = node("fieldset"),
    http = node("fieldset");
  sections.connection.append(stdio, http);
  const command = mcpField(stdio, "可执行文件", transport.command || "");
  const cwd = mcpField(stdio, "工作目录（可选）", transport.cwd || "");
  const args = mcpOpaque(
    stdio,
    "命令参数 JSON 数组",
    Boolean(entry && transport.type === "stdio"),
    "array",
  );
  const env = mcpOpaque(
    stdio,
    "环境值 JSON 对象",
    Boolean(entry && transport.type === "stdio"),
    "object",
  );
  const envRefs = mcpField(
    stdio,
    "环境变量引用（每行一个）",
    (transport.env_vars || []).join("\n"),
    null,
    "textarea",
  );
  const url = mcpOpaque(
    http,
    "服务地址",
    Boolean(entry && transport.type === "http"),
    "url",
    true,
  );
  if (transport.origin)
    http.append(node("p", "已保存地址来源：" + transport.origin, "hint"));
  const headers = mcpOpaque(
    http,
    "请求头值 JSON 对象",
    Boolean(entry && transport.type === "http"),
    "object",
  );
  const headerRefs = mcpField(
    http,
    "请求头凭据引用 JSON 对象",
    JSON.stringify(transport.env_headers || {}, null, 2),
    null,
    "textarea",
  );
  const bearer = mcpField(
    http,
    "Bearer 凭据名称（可选）",
    transport.bearer_token_env || "",
  );
  const credential = node("details", undefined, "connection-auth-options");
  credential.append(node("summary", "私有凭据（可选）"));
  sections.connection.append(credential);
  credential.append(
    node(
      "p",
      "填入名称与新值将随本次保存写入私有凭据；留空保持。替换同名凭据会影响其他引用者。新凭据需保存后才能验证。",
      "hint",
    ),
  );
  const keyName = mcpField(
      credential,
      "凭据变量名称",
      transport.bearer_token_env || "",
    ),
    keyValue = mcpField(credential, "新凭据值", "", null, "password");
  const advanced = node("details");
  advanced.append(node("summary", "高级连接设置"));
  sections.connection.append(advanced);
  const startup = mcpField(
    advanced,
    "启动超时（秒）",
    spec.startup_timeout_seconds,
    null,
    "number",
  );
  startup.min = 1;
  startup.max = 3600;
  const timeout = mcpField(
    advanced,
    "工具超时（秒）",
    spec.tool_timeout_seconds,
    null,
    "number",
  );
  timeout.min = 1;
  timeout.max = 3600;
  function transportKind() {
    stdio.hidden = type.value !== "stdio";
    stdio.disabled = stdio.hidden;
    http.hidden = type.value !== "http";
    http.disabled = http.hidden;
  }
  type.addEventListener("change", transportKind);
  transportKind();
  const enabled = mcpField(
    sections.policy,
    "启用此服务",
    spec.enabled,
    null,
    "checkbox",
  );
  const scopeBox = node("div", undefined, "mcp-inline");
  sections.policy.append(scopeBox);
  const foreground = mcpField(
    scopeBox,
    "主对话可用",
    spec.contexts.includes("foreground"),
    null,
    "checkbox",
  );
  const background = mcpField(
    scopeBox,
    "后台任务可用",
    spec.contexts.includes("background"),
    null,
    "checkbox",
  );
  const policies = [
    ["approve", "自动允许"],
    ["reject", "禁止调用"],
  ];
  const approval = mcpField(
    sections.policy,
    "服务调用策略",
    spec.approval,
    policies,
  );
  const range = mcpField(
    sections.policy,
    "工具范围",
    spec.allow_tools === null ? "all" : "selected",
    [
      ["all", "全部工具（包括未来新增）"],
      ["selected", "仅指定工具（新增不自动授权）"],
    ],
  );
  const allowed = mcpField(
    sections.policy,
    "指定工具（每行一个）",
    (spec.allow_tools || []).join("\n"),
    null,
    "textarea",
  );
  sections.policy.append(
    node(
      "p",
      "全部工具模式包含服务未来新增的工具。禁止规则优先；实际调用仍受服务启停、使用范围及执行工具范围限制。",
      "hint",
    ),
  );
  function rangeMode() {
    allowed.closest("label").hidden = range.value !== "selected";
  }
  range.addEventListener("change", rangeMode);
  rangeMode();
  const context = mcpField(
    sections.tools,
    "验证使用范围",
    foreground.checked ? "foreground" : "background",
    [
      ["foreground", "主对话"],
      ["background", "后台任务"],
    ],
  );
  const verify = button("验证连接并发现工具", () => runVerification());
  sections.tools.append(verify);
  const result = node(
    "p",
    "验证只连接并发现工具，不调用模型、设备或业务工具。",
    "hint",
  );
  sections.tools.append(result);
  const tools = node("div", undefined, "mcp-tool-list");
  sections.tools.append(tools);
  const checks = structuredClone(entry?.verification || {});
  let discovered = [],
    verifyState = null,
    generation = 0;
  const overrides = new Map();
  const lines = (text) => [
    ...new Set(
      text
        .split("\n")
        .map((x) => x.trim())
        .filter(Boolean),
    ),
  ];
  function collect() {
    const ident = name.value.trim();
    if (!/^[a-z][a-z0-9_]{0,47}$/.test(ident))
      throw Error(
        "服务名称须为小写字母开头，使用小写字母、数字和下划线，最多 48 字符。",
      );
    if (
      !entry &&
      mcpInventory.servers.length >=
        valueAt(["external_mcp", "max_servers"], 32)
    )
      throw Error("已达到 MCP 服务数量上限，请先在高级管理设置中调整。 ");
    if (!entry && mcpInventory.servers.some((s) => s.name === ident))
      throw Error("服务名称已存在，请编辑已有服务。");
    const value = {
      ...spec,
      enabled: enabled.checked,
      approval: approval.value,
      contexts: [
        ...(foreground.checked ? ["foreground"] : []),
        ...(background.checked ? ["background"] : []),
      ],
      allow_tools: range.value === "all" ? null : lines(allowed.value),
      startup_timeout_seconds: Number(startup.value),
      tool_timeout_seconds: Number(timeout.value),
    };
    for (const [tool, policy] of overrides) {
      if (policy === "inherit") {
        delete value.tool_approvals[tool];
        value.deny_tools = value.deny_tools.filter((t) => t !== tool);
      } else if (policy === "approve") {
        value.tool_approvals[tool] = "approve";
        value.deny_tools = value.deny_tools.filter((t) => t !== tool);
        if (value.allow_tools !== null && !value.allow_tools.includes(tool))
          value.allow_tools.push(tool);
      } else value.tool_approvals[tool] = "reject";
    }
    const changed = {};
    for (const key of [
      "enabled",
      "approval",
      "contexts",
      "allow_tools",
      "deny_tools",
      "tool_approvals",
      "startup_timeout_seconds",
      "tool_timeout_seconds",
    ])
      if (!entry || JSON.stringify(value[key]) !== JSON.stringify(initial[key]))
        changed[key] = value[key];
    let t = { type: type.value };
    if (type.value === "stdio") {
      if (!command.value.trim()) throw Error("请填写可执行文件");
      t.command = command.value.trim();
      t.cwd = cwd.value.trim() || null;
      t.env_vars = lines(envRefs.value);
      for (const [key, control] of [
        ["args", args],
        ["env", env],
      ]) {
        const value = control.read();
        if (value !== undefined) t[key] = value;
      }
    } else {
      const address = url.read();
      if (address !== undefined) t.url = address;
      const values = headers.read();
      if (values !== undefined) t.headers = values;
      t.env_headers = JSON.parse(headerRefs.value || "{}");
      t.bearer_token_env = bearer.value.trim() || null;
    }
    if (entry && type.value === transport.type) {
      for (const key of Object.keys(t))
        if (
          key !== "type" &&
          JSON.stringify(t[key]) === JSON.stringify(transport[key] ?? null)
        )
          delete t[key];
      if (Object.keys(t).length > 1) changed.transport = t;
    } else changed.transport = t;
    if (
      ![value.startup_timeout_seconds, value.tool_timeout_seconds].every(
        (n) => Number.isInteger(n) && n >= 1 && n <= 3600,
      )
    )
      throw Error("超时应为 1–3600 秒的整数。");
    if (
      keyValue.value &&
      !/^[A-Za-z_][A-Za-z0-9_]*$/.test(keyName.value.trim())
    )
      throw Error("请填写有效的凭据变量名称。");
    return { ident, changes: changed };
  }
  function previewPatch() {
    const current = collect(),
      patch = structuredClone(
        mcpPatch() || { revision: mcpInventory.revision, changes: [] },
      );
    const old = patch.changes.findIndex((c) => c.name === current.ident);
    const operation = {
      name: current.ident,
      op: "configure",
      changes: mergeMcpChanges(patch.changes[old]?.changes, current.changes),
    };
    if (old >= 0) patch.changes[old] = operation;
    else patch.changes.push(operation);
    return { current, patch };
  }
  function showTools(check) {
    tools.replaceChildren();
    if (!check)
      result.textContent = "所选范围尚未验证；验证只发现工具，不执行操作。";
    if (check)
      result.textContent = `${check.current === false ? "历史结果已过期 · " : ""}${mcpVerificationLabels[check.state] || "验证未完成"} · ${new Date(check.checked_at * 1000).toLocaleString()} · 配置 ${check.configuration_version?.slice(0, 12) || "未记录"} / 服务版本 ${check.server_revision ?? "未记录"} · 来源：Pi 初始化与工具发现 · 发现 ${check.tools_count} 个工具。未验证业务操作。${check.reason ? " " + check.reason : ""}${check.truncated ? " 工具列表已截断。" : ""}`;
    const names = new Set([
      ...discovered.map((t) => t.name),
      ...(spec.allow_tools || []),
      ...spec.deny_tools,
      ...Object.keys(spec.tool_approvals),
    ]);
    for (const tool of names) {
      const metadata = discovered.find((t) => t.name === tool),
        row = node("section", undefined, "mcp-tool-row");
      row.dataset.tool = tool;
      const text = node("div");
      text.append(
        node("strong", tool),
        node(
          "small",
          metadata?.description || "未在当前验证中发现；保留已配置规则。",
          "hint",
        ),
      );
      if (metadata?.annotations && Object.keys(metadata.annotations).length)
        text.append(
          node(
            "small",
            Object.entries(metadata.annotations)
              .map(
                ([k, v]) =>
                  ({
                    readOnlyHint: v ? "服务声明只读" : "可能写入",
                    destructiveHint: v ? "可能有破坏性" : "服务声明非破坏性",
                    idempotentHint: v ? "服务声明幂等" : "未声明幂等",
                    openWorldHint: v ? "可能访问外部" : "服务声明无外部访问",
                  })[k],
              )
              .join(" · "),
            "hint",
          ),
        );
      const status = node(
        "span",
        mcpReasons[metadata?.permission] || "规则待验证",
        "hint",
      );
      const policy = mcpField(
        row,
        "工具规则 · " + tool,
        overrides.get(tool) ||
          (spec.deny_tools.includes(tool)
            ? "reject"
            : spec.tool_approvals[tool] || "inherit"),
        [
          ["inherit", "跟随服务与范围"],
          ["approve", "允许并加入范围"],
          ["reject", "禁止"],
        ],
      );
      policy.disabled = approval.value === "reject";
      policy.addEventListener("change", () => {
        overrides.set(tool, policy.value);
        status.textContent =
          "待保存 · " +
          (policy.value === "approve"
            ? "允许"
            : policy.value === "reject"
              ? "禁止"
              : "跟随规则");
      });
      row.prepend(text, status);
      tools.append(row);
    }
    if (!names.size)
      tools.append(
        node(
          "p",
          "验证连接后展示真实工具，也可在指定工具范围中填写工具名。",
          "hint",
        ),
      );
  }
  function cached() {
    const check = checks[context.value];
    discovered = check?.tools || [];
    verifyState = check || null;
    showTools(check);
  }
  context.addEventListener("change", cached);
  approval.addEventListener("change", () => showTools(verifyState));
  cached();
  async function runVerification() {
    let requested;
    try {
      requested = previewPatch();
      const target = { ...transport, ...requested.current.changes.transport };
      const refs =
        target.type === "stdio"
          ? target.env_vars || []
          : [
              ...Object.values(target.env_headers || {}),
              target.bearer_token_env,
            ].filter(Boolean);
      if (refs.some((ref) => resourceEdits.has("credentials:" + ref)))
        throw Error("该服务引用的凭据有待保存修改，请保存后再验证。");
      if (keyValue.value)
        throw Error("请先保留修改并保存新凭据，再验证连接；验证不会先行保存。");
    } catch (e) {
      showNotice(e.message);
      return;
    }
    verify.disabled = true;
    const ticket = ++generation,
      requestedContext = context.value,
      fingerprint = JSON.stringify([
        requested.patch,
        keyName.value,
        keyValue.value,
      ]);
    result.textContent = "正在建立隔离连接并发现工具…";
    try {
      const check = await api("/api/permissions/mcp/verify", {
        version: snapshot.version,
        patch: requested.patch,
        name: requested.current.ident,
        context: requestedContext,
      });
      if (
        ticket !== generation ||
        !result.isConnected ||
        !activeEditor ||
        activeEditor.kind !== "mcp"
      )
        return;
      if (
        JSON.stringify([
          previewPatch().patch,
          keyName.value,
          keyValue.value,
        ]) !== fingerprint
      ) {
        result.textContent = "设置已修改，本次结果属于旧草稿，请重新验证。";
        return;
      }
      if (check.reason && !check.checked_at) {
        result.textContent = check.reason;
        discovered = [];
        tools.replaceChildren();
        return;
      }
      checks[requestedContext] = check;
      if (context.value !== requestedContext) return;
      discovered = check.tools || [];
      verifyState = check;
      showTools(check);
    } catch (e) {
      result.textContent = e.message;
    } finally {
      verify.disabled = false;
    }
  }
  let accepted = null,
    checking = false;
  const editor = {
    kind: "mcp",
    name: entry?.name,
    checkpoint,
    field: { path: ["mcp", entry?.name || "new"] },
    validate() {
      let request, fingerprint;
      try {
        request = previewPatch();
        fingerprint = JSON.stringify([
          request.patch,
          keyName.value,
          keyValue.value,
        ]);
      } catch (e) {
        showNotice(e.message);
        return false;
      }
      if (accepted === fingerprint) return true;
      if (checking) return false;
      checking = true;
      $("entry-done").disabled = true;
      api("/api/permissions/mcp", {
        version: snapshot.version,
        patch: request.patch,
      })
        .then(() => {
          if (activeEditor !== editor) return;
          if (
            JSON.stringify([
              previewPatch().patch,
              keyName.value,
              keyValue.value,
            ]) !== fingerprint
          ) {
            showNotice("设置已修改，请再次保留修改以校验当前输入。");
            return;
          }
          accepted = fingerprint;
          finishEntity();
        })
        .catch((error) => {
          if (activeEditor === editor) showNotice(error.message);
        })
        .finally(() => {
          checking = false;
          $("entry-done").disabled = false;
        });
      return false;
    },
    commit() {
      const current = collect();
      if (Object.keys(current.changes).length)
        stageMcp(current.ident, "configure", current.changes);
      if (keyValue.value)
        resourceEdits.set("credentials:" + keyName.value.trim(), {
          id: "credentials",
          key: keyName.value.trim(),
          op: "replace_secret",
          value: keyValue.value,
        });
    },
  };
  activeEditor = editor;
  const invalidate = (event) => {
    if (event.target === context) return;
    result.textContent =
      "设置已修改，已有验证结果仅供参考；请重新验证当前草稿。";
  };
  body.addEventListener("input", invalidate);
  activeEditor.dispose = () => {
    body.removeEventListener("input", invalidate);
    body
      .querySelectorAll('input[type="password"], .mcp-private-field textarea')
      .forEach((input) => {
        input.value = "";
      });
  };
  choose(firstTab);
  showDialog("entry-dialog");
  name.focus();
}
