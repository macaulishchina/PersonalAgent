"use strict";

// Product views compose the shared typed editor; persistence remains in console.js.
let modelTab = "connections",
  activeEditor = null,
  pendingNavigation = null;
const collectionQueries = new Map();
const pageCopy = {
  overview: ["概览", "从一个可用模型开始，按需要扩展你的工作空间。"],
  models: ["模型与账号", "连接模型服务，管理可用模型，再决定在哪里使用。"],
  execution: ["对话与执行", "决定如何处理请求，以及一项工作可以使用多少资源。"],
  tasks: ["任务与通知", "安排后台工作，选择结果送达的方式。"],
  resources: ["文件与记忆", "管理工作内容的存放、保留和长期记忆。"],
  connections: ["连接与设备", "让助手使用外部工具与已经配置的设备。"],
  plugins: ["功能与插件", "按需要开启能力；每项能力都有自己的设置入口。"],
  access: ["实例管理", "查看运行状态，维护访问入口和实例。"],
};
const pageSections = {
  execution: [
    ["执行容量", "同时处理多少工作，以及等待队列的大小。", ["execution"]],
    [
      "用量保护",
      "额度属于每项工作；调整限制不会清零已经使用的额度。",
      ["ai_usage"],
    ],
    ["对话体验", "这些设置仅在相应能力启用时使用。", []],
  ],
  tasks: [
    ["安排任务", "设置任务并发、计划时区和执行策略。", ["tasks"]],
    ["接收结果", "选择通知方式与投递规则。", ["notifications"]],
    ["事件处理", "配置事件的接收与保留。", ["events"]],
  ],
  resources: [
    ["工作文件", "工作区与文件保留策略。", ["workspace", "work_files"]],
    [
      "长期记忆",
      "选择是否记录和整理需要长期保留的信息。",
      ["memory_enabled", "memory_auto_capture"],
    ],
    ["实例技能", "管理明确选用的技能。", ["instance_skills"]],
  ],
  connections: [
    ["外部工具", "通过 MCP 连接已经信任的工具服务。", ["external_mcp"]],
    ["设备", "普通设备独立配置；主语音绑定变更需要重启。", ["devices"]],
    ["语音连接", "主语音通道及音量策略。", []],
  ],
  access: [["实例设置", "基础路径和进程参数通常无需调整。", []]],
};
const pluginNames = {
  pi: "执行引擎",
  conversation: "通用对话",
  cli: "命令行入口",
  workfiles: "工作文件",
  models: "模型服务",
  ai_usage: "用量保护",
  integrations: "外部工具",
  assistant: "个人助手",
  devices: "设备管理",
  pipewire: "本机音频",
  v4l2: "本机摄像头",
  airplay: "AirPlay 音频",
  voice_disabled: "语音占位",
  web: "聊天网页",
};
function button(text, callback, cls = "secondary") {
  const item = node("button", text, cls);
  item.type = "button";
  item.addEventListener("click", callback);
  return item;
}
function fieldAt(path) {
  return snapshot.fields.find((f) => key(f.path) === key(path));
}
function valueAt(path, fallback) {
  const field = fieldAt(path);
  return field ? (currentValue(field) ?? fallback) : fallback;
}
function renderChatShortcut() {
  const entry = $("chat-open");
  if (!entry) return;
  entry.hidden = selected !== "plugin:web";
  entry.onclick = null;
  if (entry.hidden) return;
  const plugins = fieldAt(["plugins"])?.loaded_value,
    enabled = fieldAt(["web", "enable"])?.loaded_value;
  const address = plugins?.includes("web") && enabled &&
    ["running", "degraded"].includes($("health").dataset.state)
    ? new URL("/chat/", location.origin).href : null;
  entry.disabled = !address;
  entry.title = address || "聊天网页尚未生效或当前地址不可访问";
  if (address) entry.onclick = () => window.open(address, "_blank", "noopener,noreferrer");
}
function moveTo(page, tab) {
  if (page !== selected && hasLocalChanges()) {
    pendingNavigation = { page, tab };
    $("leave-description").textContent = invalid.size
      ? "当前页面有格式错误。可以继续修正，或放弃修改后切换。"
      : "当前页面有未保存的修改。选择保存并生效，或放弃这些修改。";
    $("leave-save").disabled = Boolean(invalid.size);
    showDialog("leave-dialog");
    $("leave-stay").focus();
    return;
  }
  navigateTo(page, tab);
}
function navigateTo(page, tab) {
  selected = page;
  if (tab) modelTab = tab;
  render();
  window.scrollTo(0, 0);
  $("page-title").focus({ preventScroll: true });
}
async function leavePage(save) {
  const target = pendingNavigation;
  if (!target || saving) return;
  $("leave-dialog")
    .querySelectorAll("button")
    .forEach((b) => (b.disabled = true));
  try {
    if (save) await saveChanges();
    else {
      for (const map of [edits, invalid, drafts, resourceEdits]) map.clear();
      for (const patch of pendingRestart?.patches || []) edits.set(key(patch.path), patch);
    }
    $("leave-dialog").close();
    pendingNavigation = null;
    navigateTo(target.page, target.tab);
  } finally {
    $("leave-dialog")
      .querySelectorAll("button")
      .forEach((b) => (b.disabled = false));
    $("leave-save").disabled = Boolean(invalid.size);
  }
}
function surface(title, detail, cls = "") {
  const panel = node("article", undefined, "settings-panel " + cls);
  panel.append(cardHeading(title, detail));
  return panel;
}
function emptyState(title, detail, label, callback) {
  const box = node("div", undefined, "empty-state");
  box.append(node("h3", title), node("p", detail, "hint"));
  if (label) box.append(button(label, callback, "primary"));
  return box;
}
function prettyField(field) {
  const ui = field.interface || {},
    schema = structuredClone(field.schema);
  if (field.path.join(".") === "ai_usage.warning_fraction") {
    schema["x-display-scale"] = 100;
    schema["x-unit"] = "%";
    schema.description = "已用额度达到此比例时提醒；不会改变工作总额度。";
  }
  if (field.path.join(".") === "ai_usage.work_limits")
    schema["x-advanced"] = true;
  return {
    ...field,
    interface: {
      ...ui,
      ...(field.path.join(".") === "ai_usage.work_limits"
        ? { advanced: true }
        : {}),
    },
    schema: {
      ...schema,
      ...(ui.widget === "secret" ? { writeOnly: true } : {}),
      ...(ui.widget === "status" ? { readOnly: true } : {}),
      ...(ui.widget === "model" ? { "x-reference": "model_api.entries" } : {}),
      ...(ui.options_from ? { "x-reference": ui.options_from } : {}),
      title:
        { main_model: "主对话模型", main_reasoning_effort: "默认思考深度" }[
          field.path.join(".")
        ] ||
        ui.title ||
        field.schema.title,
    },
  };
}
function workspaceRender() {
  const docs = $("documents"),
    navigation = $("navigation");
  docs.replaceChildren();
  navigation.replaceChildren();
  const enabled = valueAt(["plugins"], []);
  for (const [section, pages] of [
    ["开始使用", ["overview", "models"]],
    ["工作空间", ["execution", "tasks", "resources", "connections"]],
    ["管理", ["plugins", "access"]],
  ]) {
    navigation.append(node("p", section, "nav-label"));
    for (const page of pages) {
      const item = button(
        "",
        () => moveTo(page),
        selected === page ? "active" : "",
      );
      item.append(
        icon(navPaths[page] || navPaths.access),
        node("span", pageCopy[page][0]),
      );
      item.setAttribute("aria-current", page === selected ? "page" : "false");
      navigation.append(item);
    }
  }
  const plugins = inventory.plugins.filter(
    (p) =>
      p.page?.startsWith("plugin:") &&
      (enabled.includes(p.id) || selected === p.page),
  );
  if (plugins.length) navigation.append(node("p", "插件设置", "nav-label"));
  for (const plugin of plugins) {
    const item = button(
      plugin.configuration?.title || plugin.id,
      () => moveTo(plugin.page),
      selected === plugin.page ? "active" : "",
    );
    item.setAttribute(
      "aria-current",
      selected === plugin.page ? "page" : "false",
    );
    navigation.append(item);
  }
  const copy = pageCopy[selected] || [
    groups[selected] || selected,
    descriptions[selected] || "配置此插件的能力。",
  ];
  $("page-title").textContent = copy[0];
  $("page-description").textContent = copy[1];
  $("breadcrumb-current").textContent = copy[0];
  renderChatShortcut();
  if (selected === "overview") renderOverview();
  else if (selected === "models") renderModels();
  else if (selected === "plugins") renderPlugins();
  else renderSettingsPage();
  if (selected !== "models" || modelTab !== "connections") {
    clearTimeout(accountTimer);
    accountTimer = undefined;
  }
}
function renderOverview() {
  const providers = valueAt(["model_api", "providers"], {}),
    models = valueAt(["model_api", "entries"], {});
  const primary = valueAt(["main_model"], ""),
    enabled = valueAt(["model_api", "enabled"], false);
  const connected = Object.keys(providers).length > 0,
    chosen = Boolean(models[primary]);
  const hero = node("article", undefined, "welcome-panel");
  hero.append(
    node("span", "PERSONAL AGENT / WORKSPACE", "eyebrow"),
    node("h2", enabled && chosen ? "你的工作空间已配置" : "先接通一个模型"),
    node(
      "p",
      enabled && chosen
        ? "在这里管理模型、工作方式和扩展能力。实际调用结果请在对话中确认。"
        : "连接一个模型服务，选择主对话模型，就可以开始使用。其他能力可以以后再设置。",
    ),
  );
  hero.append(
    button(
      enabled && chosen ? "管理模型" : "开始配置",
      () =>
        moveTo(
          "models",
          !connected ? "connections" : !chosen ? "library" : "usage",
        ),
      "primary",
    ),
  );
  $("documents").append(hero);
  const steps = node("div", undefined, "setup-steps");
  for (const [index, title, detail, done, tab] of [
    ["01", "连接服务", "使用 API Key 或已有账号", connected, "connections"],
    [
      "02",
      "添加模型",
      "填写实际模型 ID 和能力",
      Object.keys(models).length > 0,
      "library",
    ],
    ["03", "开始使用", "指定主对话模型并启用", chosen && enabled, "usage"],
  ]) {
    const step = button("", () => moveTo("models", tab), "setup-step");
    step.append(
      node(
        "span",
        done ? "✓" : index,
        done ? "step-number done" : "step-number",
      ),
      node("strong", title),
      node("small", detail),
    );
    steps.append(step);
  }
  $("documents").append(steps);
  const shortcuts = node("div", undefined, "quick-grid");
  for (const [page, title, detail] of [
    ["execution", "控制工作量", "并发、队列与调用额度"],
    ["connections", "连接工具", "MCP 服务与设备"],
    ["plugins", "扩展能力", "任务、记忆与更多插件"],
    ["access", "维护实例", "访问入口、备份与恢复"],
  ]) {
    const link = button("", () => moveTo(page), "quick-card");
    link.append(
      icon(navPaths[page] || navPaths.access),
      node("strong", title),
      node("small", detail),
      node("span", "↗", "card-arrow"),
    );
    shortcuts.append(link);
  }
  $("documents").append(
    node("h2", "接下来，按需要调整", "section-title"),
    shortcuts,
  );
}
function renderModels() {
  const tabs = node("div", undefined, "page-tabs");
  tabs.setAttribute("role", "tablist");
  tabs.setAttribute("aria-label", "模型设置");
  for (const [id, label] of [
    ["connections", "连接"],
    ["library", "模型库"],
    ["usage", "用途"],
    ["advanced", "高级"],
  ]) {
    const item = button(
      label,
      () => {
        modelTab = id;
        render();
      },
      modelTab === id ? "active" : "",
    );
    item.setAttribute("role", "tab");
    item.setAttribute("aria-selected", String(modelTab === id));
    item.id = "model-tab-" + id;
    item.tabIndex = modelTab === id ? 0 : -1;
    item.setAttribute("aria-controls", "model-panel");
    item.addEventListener("keydown", (event) => {
      const keys = ["ArrowLeft", "ArrowRight", "Home", "End"];
      if (!keys.includes(event.key)) return;
      event.preventDefault();
      const items = [...tabs.children],
        index = items.indexOf(item);
      const next =
        event.key === "Home"
          ? 0
          : event.key === "End"
            ? items.length - 1
            : (index + (event.key === "ArrowRight" ? 1 : -1) + items.length) %
              items.length;
      const nextId = items[next].id;
      items[next].click();
      $(nextId)?.focus();
    });
    tabs.append(item);
  }
  $("documents").append(tabs);
  if (modelTab === "connections") {
    const panel = surface(
      "模型连接",
      "一个连接可以供多个模型使用。凭据只在这里维护。",
      "connection-panel",
    );
    panel
      .querySelector(".card-heading")
      .append(button("添加连接", openConnectionWizard, "primary"));
    renderModelCollection(panel, "providers");
    $("documents").append(panel);
    const secrets = advancedGroup("model-secrets");
    secrets.firstChild.textContent = "维护 · 实例凭据清单";
    drawResources(secrets, (r) => r.id === "credentials");
    $("documents").append(secrets);
    action(drawAccounts);
  } else if (modelTab === "library") {
    const panel = surface(
      "模型库",
      "保留稳定的模型名称，方便在对话、任务和记忆之间复用。",
    );
    panel
      .querySelector(".card-heading")
      .append(button("添加模型", () => openAddEntity("entries"), "primary"));
    renderModelCollection(panel, "entries");
    $("documents").append(panel);
  } else if (modelTab === "usage") {
    const panel = surface(
      "主对话",
      "选择聊天使用的模型。切换模型会保留会话和历史。",
    );
    for (const path of [
      ["main_model"],
      ["main_reasoning_effort"],
      ["model_api", "enabled"],
    ]) {
      const field = fieldAt(path);
      if (field) {
        const display = prettyField(field);
        if (path[0] === "main_reasoning_effort") {
          const model = valueAt(["model_api", "entries"], {})[
            valueAt(["main_model"], "")
          ];
          const levels =
            model?.reasoning_efforts ??
            model?.capabilities?.reasoning_efforts ??
            [];
          display.schema = {
            ...display.schema,
            enum: [...new Set(["auto", ...levels, valueAt(path, "auto")])],
          };
        }
        const control = drawField(display);
        if (path[0] === "main_model")
          control.addEventListener("change", () => render());
        panel.append(control);
      }
    }
    $("documents").append(panel);
    if (valueAt(["plugins"], []).includes("memory")) {
      const memory = surface("记忆整理", "可以使用不同于主对话的模型。");
      const field = fieldAt(["model_api", "memory"]);
      if (field) memory.append(drawField(prettyField(field)));
      $("documents").append(memory);
    }
    if (valueAt(["plugins"], []).includes("tasks"))
      $("documents").append(
        button("管理任务使用的模型 →", () => moveTo("tasks"), "subtle"),
      );
  } else {
    const panel = surface(
      "模型运行策略",
      "按需调整执行容量；超时、输出限制和部署参数默认收起。",
    );
    const limits = advancedGroup("model-runtime-limits");
    limits.firstChild.textContent = "超时、上下文与输出限制";
    for (const field of snapshot.fields.filter(
      (f) =>
        fieldGroup(f) === "models" &&
        !["accounts", "providers", "entries", "enabled", "memory"].includes(
          f.path[1],
        ) &&
        f.path.length > 1,
    )) {
      const displayed = prettyField(field);
      if (["concurrency", "queue_size"].includes(field.path[1]))
        panel.append(drawField(displayed));
      else limits.append(drawField(displayed));
    }
    panel.append(limits);
    $("documents").append(panel);
  }
  const body = node("div", undefined, "model-panel");
  body.id = "model-panel";
  body.setAttribute("role", "tabpanel");
  body.setAttribute("aria-labelledby", "model-tab-" + modelTab);
  for (const child of [...$("documents").children])
    if (child !== tabs) body.append(child);
  $("documents").append(body);
  if (modelTab === "connections" || modelTab === "library") {
    const next = node("div", undefined, "workflow-next");
    const connections = modelTab === "connections";
    next.append(
      node(
        "p",
        connections
          ? "连接负责认证；接下来为连接添加可用模型。"
          : "模型可以复用；接下来选择主对话使用哪一个。",
        "hint",
      ),
      button(
        connections ? "下一步：添加模型 →" : "下一步：指定用途 →",
        () => {
          modelTab = connections ? "library" : "usage";
          render();
          $("model-tab-" + modelTab)?.focus();
        },
        "secondary",
      ),
    );
    body.append(next);
  }
}
function collectionFilter(grid, label, identity) {
  const toolbar = node("div", undefined, "collection-filter"),
    input = node("input"),
    count = node("span", "", "hint");
  input.type = "search";
  input.placeholder = label;
  input.setAttribute("aria-label", label);
  input.value = collectionQueries.get(identity) || "";
  const empty = node("p", "没有匹配的结果，请调整搜索内容。", "search-empty");
  const apply = () => {
    const query = input.value.trim().toLocaleLowerCase();
    let visible = 0;
    for (const row of grid.children) {
      row.hidden = !(row.dataset.search || row.textContent)
        .toLocaleLowerCase()
        .includes(query);
      if (!row.hidden) visible++;
    }
    count.textContent = query
      ? `${visible} / ${grid.children.length} 项`
      : `${visible} 项`;
    empty.hidden = visible > 0 || !grid.children.length;
    collectionQueries.set(identity, input.value);
  };
  input.addEventListener("input", apply);
  toolbar.append(input, count);
  apply();
  return [toolbar, empty];
}
function renderModelCollection(panel, kind) {
  const entries = valueAt(["model_api", kind], {}),
    grid = node("div", undefined, "entity-grid");
  grid.dataset.kind = kind;
  if (!Object.keys(entries).length)
    grid.append(
      emptyState(
        kind === "providers" ? "还没有模型连接" : "还没有模型",
        kind === "providers"
          ? "添加 API Key 连接，或通过 OAuth 登录。"
          : "先有连接，再填写供应商提供的模型 ID。",
        kind === "providers" ? "添加第一个连接" : "添加第一个模型",
        kind === "providers"
          ? openConnectionWizard
          : () => openAddEntity("entries"),
      ),
    );
  for (const [name, value] of Object.entries(entries)) {
    const card = node("section", undefined, "entity-card");
    card.dataset.entity = name;
    card.dataset.search = [
      name,
      value.model,
      value.provider,
      value.account,
      value.preset,
      ...(kind === "entries"
        ? Object.values(taskRecommendations())
            .filter((item) => item.model === name)
            .map((item) => item.name)
        : []),
    ]
      .filter(Boolean)
      .join(" ");
    const head = node("div", undefined, "entity-heading");
    head.append(node("span", name.slice(0, 2).toUpperCase(), "entity-avatar"));
    if (kind === "entries") {
      const identity = node("div", undefined, "entity-name-and-usage");
      identity.append(node("h3", name));
      if (valueAt(["main_model"], "") === name)
        identity.append(node("span", "主对话", "badge configured"));
      for (const item of Object.values(taskRecommendations())) {
        if (item.model !== name) continue;
        const badge = node("span", item.name, "badge recommendation-badge");
        badge.title = `${item.reasoning_effort || "auto"} · ${item.reason}
${item.description}`;
        identity.append(badge);
      }
      head.append(identity);
    } else head.append(node("h3", name));
    if (kind === "providers")
      head.append(
        node(
          "span",
          value.enabled ? "已启用" : "未启用",
          value.enabled ? "badge configured" : "badge",
        ),
      );

    card.append(
      head,
      node(
        "p",
        kind === "providers"
          ? value.account
            ? (providerAuthentication(value) === "oauth" ? "OAuth 登录 · " : "API Key · ") + value.account
            : "API 连接 · " + (value.preset || value.protocol || "待配置")
          : value.model || "尚未填写模型 ID",
        "entity-description",
      ),
    );
    if (kind === "entries") {
      const chips = node("div", undefined, "entity-tags");
      chips.append(node("span", value.provider || "未选择连接", "badge"));
      if (value.images) chips.append(node("span", "图片", "badge"));
      card.append(chips);
    }
    const actions = node("div", undefined, "entity-actions");
    let probeStatus;
    if (kind === "entries") {
      const probe = modelTest(name);
      actions.append(probe.button);
      probeStatus = probe.status;
    }
    actions.append(button("编辑", () => openEntity(kind, name)));
    if (kind === "providers") {
      const probe = providerTest(name);
      actions.append(probe.button);
      probeStatus = probe.status;
    }
    actions.append(
      button(
        "移除",
        () => {
          if (
            !confirm(
              "移除 “" +
                name +
                "”？引用此项的模型或用途需要一并调整，保存时会校验。",
            )
          )
            return;
          changed(["model_api", kind, name], "default");
          render();
        },
        "text-button",
      ),
    );
    card.append(actions);
    if (probeStatus) card.append(probeStatus);
    grid.append(card);
  }
  if (Object.keys(entries).length) {
    const [filter, empty] = collectionFilter(
      grid,
      kind === "providers" ? "搜索连接或账号" : "搜索名称、模型 ID 或连接",
      kind,
    );
    panel.append(filter, grid, empty);
  } else panel.append(grid);
}
function renderSettingsPage() {
  const enabled = valueAt(["plugins"], []),
    all = snapshot.fields.filter(
      (f) => fieldGroup(f) === selected && interfaceVisible(f),
    );
  const fields = all.filter(
    (f) =>
      showInactive ||
      selected.startsWith("plugin:") ||
      !f.owner_id ||
      enabled.includes(f.owner_id),
  );
  const used = new Set();
  if (selected.startsWith("plugin:")) {
    const plugin = inventory.plugins.find((p) => p.id === selected.slice(7));
    if (!enabled.includes(plugin?.id))
      $("documents").append(
        node(
          "p",
          "此插件未启用。可以先准备设置，再到“功能与插件”检查认证和依赖。",
          "inline-note",
        ),
      );
  }
  for (const [title, detail, prefixes] of pageSections[selected] || [
    [groups[selected], descriptions[selected], []],
  ]) {
    const items = fields.filter(
      (f) =>
        !used.has(key(f.path)) &&
        (!prefixes.length || prefixes.includes(f.path[0])),
    );
    if (!items.length) continue;
    const panel = surface(title, detail),
      advanced = advancedGroup("page:" + selected + ":" + title);
    for (const field of items) {
      used.add(key(field.path));
      const displayed = prettyField(field);
      const isAdvanced =
        displayed.interface?.advanced ?? displayed.schema["x-advanced"];
      const editor =
        key(field.path) === key(["tasks", "model_policy"])
          ? renderTaskPolicy(displayed)
          : key(field.path) === key(["tasks", "execution"])
            ? renderTaskDefaults(displayed)
            : drawField(displayed);
      if (items.length === 1 && editor.classList.contains("object-field"))
        editor.classList.add("panel-fields");
      (isAdvanced ? advanced : panel).append(editor);
    }
    if (advanced.children.length > 1) panel.append(advanced);
    $("documents").append(panel);
  }
  const rest = fields.filter((f) => !used.has(key(f.path)));
  if (rest.length) {
    const panel = surface("其他设置", "");
    for (const field of rest) panel.append(drawField(prettyField(field)));
    $("documents").append(panel);
  }
  if (!fields.length)
    $("documents").append(
      emptyState(
        "此能力尚未启用",
        "启用相应功能后，这里会显示它的设置。已有配置会保留。",
        "选择功能",
        () => moveTo("plugins"),
      ),
    );
  if (
    all.some((f) => f.owner_id && !enabled.includes(f.owner_id)) &&
    !selected.startsWith("plugin:")
  )
    $("documents").append(
      button(
        showInactive ? "收起未启用功能的配置" : "查看未启用功能的已有配置",
        () => {
          showInactive = !showInactive;
          render();
        },
        "subtle",
      ),
    );
  const owner = inventory.plugins.find(
    (p) => p.page === selected && selected.startsWith("plugin:"),
  );
  if (owner) {
    const panel = surface("管理操作", "即时执行，独立于配置保存。");
    drawActions(owner, panel);
    if (panel.querySelector("select")) $("documents").append(panel);
  }
  drawResources();
  if (selected === "resources" || selected === "plugin:memory")
    drawMemoryDebug($("documents"), enabled.includes("memory"));
}
function drawMemoryDebug(parent, enabled) {
  const box = node("details", undefined, "memory-debug card"),
    summary = node("summary", "调试 · 查询已保存记忆"),
    body = node("div", undefined, "memory-debug-body"),
    form = node("form", undefined, "memory-debug-toolbar"),
    query = node("input"),
    scope = node("select"),
    status = node("p", "", "hint"),
    results = node("div", undefined, "memory-debug-results");
  query.type = "search";
  query.maxLength = 200;
  query.placeholder = "关键词，匹配标题或正文";
  query.setAttribute("aria-label", "记忆关键词");
  scope.setAttribute("aria-label", "记忆范围");
  for (const [value, title] of [
    ["all", "全部范围"],
    ["user", "个人记忆"],
    ["project", "当前项目"],
  ]) {
    const option = node("option", title);
    option.value = value;
    scope.append(option);
  }
  const submit = node("button", "查询", "primary"),
    recent = button("最近保存", () => {
      query.value = "";
      load();
    });
  recent.type = "button";
  form.append(query, scope, submit, recent);
  status.setAttribute("role", "status");
  body.append(
    node(
      "p",
      "只读调试：留空查看最近 20 条；关键词按标题和正文匹配，不触发模型调用。",
      "hint",
    ),
    form,
    status,
    results,
  );
  box.append(summary, body);
  parent.append(box);
  let loaded = false;
  async function load() {
    if (submit.disabled) return;
    loaded = true;
    submit.disabled = recent.disabled = true;
    query.disabled = scope.disabled = true;
    status.textContent = "查询中…";
    results.replaceChildren();
    try {
      const reply = await api("/api/configuration-action", {
        owner: "memory",
        action: "query",
        arguments: { query: query.value, scope: scope.value, limit: 20 },
      });
      status.textContent = reply.total
        ? `共 ${reply.total} 条，显示最近 ${reply.records.length} 条`
        : query.value.trim()
          ? "没有匹配的记忆。"
          : "尚无已保存的记忆。";
      for (const record of reply.records) {
        const item = node("article", undefined, "memory-debug-record"),
          heading = node("div", undefined, "memory-debug-heading"),
          date = new Date(record.updated * 1000),
          detail = node("details"),
          metadata = {
            id: record.id,
            revision: record.revision,
            project: record.project,
            source: record.source,
          };
        heading.append(
          node("strong", record.title || "未命名记忆"),
          node("span", record.scope === "project" ? "项目" : "个人", "tag"),
        );
        item.append(
          heading,
          node("p", record.text),
          node(
            "p",
            Number.isNaN(date.getTime())
              ? "保存时间未知"
              : date.toLocaleString(),
            "hint",
          ),
        );
        detail.append(
          node("summary", "ID 与来源"),
          node("pre", JSON.stringify(metadata, null, 2)),
        );
        item.append(detail);
        results.append(item);
      }
    } catch (error) {
      status.textContent = "查询失败：" + error.message;
    } finally {
      submit.disabled = recent.disabled = false;
      query.disabled = scope.disabled = false;
    }
  }
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    load();
  });
  if (!enabled) {
    status.textContent = "记忆插件未启用，请先启用并应用配置。";
    submit.disabled = recent.disabled = query.disabled = scope.disabled = true;
  }
  box.addEventListener("toggle", () => {
    if (box.open && enabled && !loaded) load();
  });
}
function renderPlugins() {
  const release = inventory.core_release;
  if (release) {
    const panel = surface(
      "内核 " + release.version,
      "核心组件随内核统一发布；扩展声明自己的版本与兼容范围。代码升级需要重启。",
    );
    const last = inventory.instance_release?.last_successful;
    panel.append(
      node(
        "p",
        last
          ? `上次成功运行：内核 ${last.core_version}`
          : "尚无成功运行的版本记录",
        "hint",
      ),
    );
    if (inventory.instance_release_error)
      panel.append(
        node("p", inventory.instance_release_error, "compatibility-note"),
      );
    $("documents").append(panel);
  }
  const field = fieldAt(["plugins"]),
    enabled = currentValue(field);
  const section = surface(
    "可选能力",
    "按需要启用；平台认证和依赖会在保存时一起检查。",
  );
  const grid = node("div", undefined, "capability-grid"),
    core = advancedGroup("core-assembly");
  core.firstChild.textContent = "核心组件与装配设置";
  const essential = new Set([
    "models",
    "pi",
    "ai_usage",
    "conversation",
    "cli",
    "execution",
    "workfiles",
    "integrations",
  ]);
  const plugins = new Map(inventory.plugins.map((p) => [p.id, p]));
  for (const id of enabled)
    if (!plugins.has(id)) plugins.set(id, { id, kind: "unavailable" });
  for (const plugin of [...plugins.values()].sort(
    (a, b) =>
      Number(enabled.includes(b.id)) - Number(enabled.includes(a.id)) ||
      Number(b.platform_supported === true) -
        Number(a.platform_supported === true),
  )) {
    const row = node("section", undefined, "plugin-option capability-card"),
      head = node("div", undefined, "capability-heading"),
      title = node("div");
    title.append(
      node(
        "h3",
        pluginNames[plugin.id] || plugin.configuration?.title || plugin.id,
      ),
    );
    const check = node("input");
    check.type = "checkbox";
    check.value = plugin.id;
    check.checked = enabled.includes(plugin.id);
    check.setAttribute("role", "switch");
    check.setAttribute(
      "aria-label",
      "启用" +
        (pluginNames[plugin.id] || plugin.configuration?.title || plugin.id),
    );
    const blocked =
      plugin.platform_supported === false ||
      plugin.version_compatible === false ||
      plugin.kind === "unavailable";
    check.disabled = blocked && !check.checked;
    check.addEventListener("change", () => {
      const values = new Set(valueAt(["plugins"], []));
      if (check.checked) values.add(plugin.id);
      else values.delete(plugin.id);
      changed(["plugins"], "set", [...values]);
      render();
    });
    head.append(title, check);
    row.append(head);
    row.append(
      node(
        "p",
        plugin.configuration?.description ||
          (essential.has(plugin.id)
            ? "此组件参与基本对话与执行流程。"
            : "为实例提供" + (pluginNames[plugin.id] || plugin.id) + "能力。"),
        "hint capability-description",
      ),
    );
    if (plugin.requires?.length)
      row.append(
        node(
          "p",
          "依赖 " +
            plugin.requires
              .map(
                (id) =>
                  pluginNames[id] ||
                  inventory.plugins.find((p) => p.id === id)?.configuration
                    ?.title ||
                  id,
              )
              .join("、"),
          "hint",
        ),
      );
    const foot = node("div", undefined, "capability-footer");
    foot.append(
      node(
        "span",
        plugin.version ? "版本 " + plugin.version : "版本未声明",
        "badge",
      ),
    );
    if (plugin.requires_core && plugin.kind === "extension")
      row.append(node("p", "兼容内核 " + plugin.requires_core, "hint"));
    if (plugin.version_error)
      row.append(node("p", plugin.version_error, "compatibility-note"));
    if (plugin.kind === "extension")
      for (const [id, name] of [
        ["linux", "Linux"],
        ["windows", "Windows"],
      ])
        foot.append(
          node(
            "span",
            name +
              ((plugin.supported_platforms || []).includes(id)
                ? " 已认证"
                : " 未认证"),
            "badge",
          ),
        );
    if (
      plugin.platform_error ||
      plugin.platform_supported === false ||
      plugin.kind === "unavailable"
    )
      row.append(
        node(
          "p",
          plugin.kind === "unavailable"
            ? "未安装，保留的配置可以移除。"
            : "当前平台未获认证，不能启用。",
          "compatibility-note",
        ),
      );
    if (plugin.page && (plugin.configuration || !essential.has(plugin.id)))
      foot.append(button("配置", () => moveTo(plugin.page), "text-button"));
    row.append(foot);
    (essential.has(plugin.id) ? core : grid).append(row);
  }
  const [filter, empty] = collectionFilter(grid, "搜索可选能力", "plugins");
  section.append(filter, grid, empty);
  $("documents").append(section);
  renderBackendDefaults();
  $("documents").append(core);
  for (const f of snapshot.fields.filter(
    (f) =>
      fieldGroup(f) === "plugins" &&
      !["plugins", "plugin_backends"].includes(f.path[0]),
  ))
    core.append(drawField(prettyField(f)));
}

function renderBackendDefaults() {
  const plugins = inventory.plugins.filter((p) => p.backends?.implementations),
    platform = inventory.platform;
  if (!plugins.length || !platform) return;
  const panel = surface(
    "平台默认实现",
    `当前系统：${platform === "windows" ? "Windows / Windows Server" : "Linux"}。插件自动选择当前平台的实现，通常无需修改。`,
  );
  const settings = valueAt(["plugin_backends"], {});
  const enabled = new Set(valueAt(["plugins"], []));
  for (const plugin of plugins) {
    const choices = plugin.backends.implementations[platform] || [];
    const configured = settings[plugin.id]?.[platform];
    const invalidChoice = configured && !choices.includes(configured);
    if (!invalidChoice && (!enabled.has(plugin.id) || choices.length <= 1))
      continue;
    const row = node("section", undefined, "field"),
      copy = node("div", undefined, "field-copy"),
      label = node("label", pluginNames[plugin.id] || plugin.id),
      control = node("div", undefined, "field-control"),
      input = node("select");
    const id = "backend-" + plugin.id;
    label.htmlFor = id;
    input.id = id;
    input.setAttribute(
      "aria-label",
      `${pluginNames[plugin.id] || plugin.id}默认实现`,
    );
    const automatic =
      plugin.backends.defaults[platform] ||
      (choices.length === 1 ? choices[0] : null);
    const option = node(
      "option",
      automatic ? `使用默认（${automatic}）` : "请选择默认实现",
    );
    option.value = "";
    input.append(option);
    for (const name of new Set([
      ...choices,
      ...(configured ? [configured] : []),
    ])) {
      const item = node(
        "option",
        choices.includes(name) ? name : name + "（不可用）",
      );
      item.value = name;
      input.append(item);
    }
    input.value = configured || "";
    input.addEventListener("change", () => {
      changed(
        ["plugin_backends", plugin.id, platform],
        input.value ? "set" : "default",
        input.value || undefined,
      );
      updateSaveBar();
    });
    copy.append(
      label,
      node(
        "p",
        choices.length === 1
          ? "当前平台只有一个实现，已自动选择。"
          : "同一平台有多个实现时，使用这里指定的默认值。",
        "hint",
      ),
    );
    control.append(input);
    row.append(copy, control);
    panel.append(row);
  }
  if (panel.querySelector("select")) $("documents").append(panel);
}

function draftCheckpoint() {
  return [edits, invalid, drafts, resourceEdits].map((map) =>
    structuredClone([...map]),
  );
}
function restoreCheckpoint(saved) {
  [edits, invalid, drafts, resourceEdits].forEach((map, index) => {
    map.clear();
    for (const [k, v] of saved[index]) map.set(k, v);
  });
}
function openEntity(kind, name, checkpoint = draftCheckpoint()) {
  const parent = fieldAt(["model_api", kind]);
  if (!parent) return;
  const current = valueAt(parent.path, {}),
    definition = parent.schema.additionalProperties;
  if (!Object.hasOwn(current, name)) {
    showNotice("此条目不存在，请重新加载。");
    return;
  }
  activeEditor = {
    kind,
    name,
    checkpoint,
    field: {
      ...parent,
      path: [...parent.path, name],
      interface: {},
      schema: { ...definition, title: name },
      value: current[name],
      explicit: parent.explicit,
      loaded_value: parent.loaded_value?.[name],
    },
  };
  renderEntityEditor();
  showDialog("entry-dialog");
  $("entry-body").querySelector("input,select")?.focus();
}
let editorInputController = new AbortController();
function renderEntityEditor() {
  editorInputController.abort();
  editorInputController = new AbortController();
  if (!activeEditor) return;
  const { kind, name, field } = activeEditor;
  $("entry-title").textContent =
    {
      providers: "编辑连接",
      entries: "编辑模型",
      accounts: "编辑认证配置",
      recommendations: "编辑推荐",
    }[kind] +
    " · " +
    name;
  $("entry-description").textContent =
    "修改先保留在本页，完成后统一保存并生效。";
  const copy = structuredClone(field),
    props = copy.schema.properties;
  if (kind === "recommendations") {
    renderTaskRecommendation(field);
    return;
  }
  if (kind === "entries") {
    renderModelEditor(field);
    return;
  }
  if (kind === "providers") {
    renderProviderEditor(field, name);
    return;
  }
  if (kind === "accounts") delete props.key_env;
  $("entry-body").replaceChildren(drawField(copy));
  if (kind === "accounts" && currentValue(field).auth === "api_key") {
    let previous = currentValue(field).key_env;
    const auth = connectionCredentials($("entry-body"), previous, {
      onChange() {
        const value = auth.read();
        changed([...field.path, "key_env"], "set", value.reference);
        stageConnectionSecret(previous, value.reference, value.secret);
        previous = value.reference;
        updateSaveBar();
      },
    });
  }
}
function finishEntity(discard = false) {
  if (!activeEditor) return;
  if (!discard && activeEditor.validate && !activeEditor.validate()) return;
  if (!discard) activeEditor.commit?.();
  if (
    !discard &&
    [...invalid.keys()].some(
      (id) =>
        JSON.parse(id).slice(0, activeEditor.field.path.length).join(".") ===
        activeEditor.field.path.join("."),
    )
  ) {
    showNotice("请先修正此条目的格式错误。");
    return;
  }
  if (discard) restoreCheckpoint(activeEditor.checkpoint);
  activeEditor = null;
  $("entry-dialog").close();
  render();
}
function wizard(title, detail) {
  delete $("setup-dialog").dataset.mode;
  $("setup-title").textContent = title;
  $("setup-description").textContent = detail;
  const form = $("setup-form");
  form.replaceChildren();
  form.onsubmit = null;
  showDialog("setup-dialog");
  return form;
}
function formInput(form, name, title, options = {}) {
  if (options.cards) {
    const group = node("fieldset", undefined, "route-choices");
    group.append(node("legend", title));
    for (const [index, [value, label, detail]] of options.choices.entries()) {
      const row = node("label", undefined, "route-choice"),
        input = node("input");
      input.type = "radio";
      input.name = name;
      input.value = value;
      input.checked = index === 0;
      input.setAttribute("aria-label", label);
      const copy = node("span");
      copy.append(node("strong", label), node("small", detail, "hint"));
      row.append(input, copy);
      group.append(row);
    }
    Object.defineProperty(group, "value", {
      get() {
        return group.querySelector("input:checked")?.value;
      },
    });
    form.append(group);
    return group;
  }
  const row = node("label", title, "wizard-field"),
    input = node(options.choices ? "select" : "input");
  input.name = name;
  input.setAttribute("aria-label", title);
  input.id = "setup-" + name;
  row.htmlFor = input.id;
  if (options.choices)
    for (const [value, label] of options.choices) {
      const option = node("option", label);
      option.value = value;
      input.append(option);
    }
  else input.type = options.type || "text";
  input.required = Boolean(options.required);
  input.placeholder = options.placeholder || "";
  if (options.pattern) input.pattern = options.pattern;
  if (options.value !== undefined) input.value = options.value;
  if (input.type === "password") input.autocomplete = "new-password";
  if (options.suggestions) {
    const list = node("datalist");
    list.id = input.id + "-options";
    for (const [value, label] of options.suggestions) {
      const option = node("option", label);
      option.value = value;
      list.append(option);
    }
    input.setAttribute("list", list.id);
    row.append(list);
  }
  row.append(input);
  if (options.hint) row.append(node("small", options.hint, "hint"));
  form.append(row);
  return input;
}
function wizardFooter(form, label) {
  const footer = node("div", undefined, "dialog-actions");
  footer.append(
    button("取消", () => {
      $("setup-dialog").close();
      form.replaceChildren();
    }),
  );
  const submit = node("button", label, "primary");
  submit.type = "submit";
  footer.append(submit);
  form.append(footer);
}
function providerKeyName(provider) {
  return (
    provider.key_env || snapshot.provider_presets?.[provider.preset]?.key_env
  );
}
function providerAuthentication(provider) {
  if (!provider?.account) return "api_key";
  return valueAt(["model_api", "accounts"], {})[provider.account]?.auth;
}
function providerDraft(name) {
  const editing =
    activeEditor?.kind === "providers" && activeEditor.name === name
      ? activeEditor.readConnection?.()
      : null;
  const provider =
    editing?.provider || valueAt(["model_api", "providers"], {})[name];
  if (!provider) throw Error("请选择要测试的连接。");
  const paths = [["model_api", "providers", name]];
  if (provider.account) paths.push(["model_api", "accounts", provider.account]);
  for (const id of invalid.keys()) {
    const path = JSON.parse(id);
    if (
      paths.some((prefix) =>
        prefix.every((part, index) => path[index] === part),
      )
    ) {
      const input = document.getElementById(id);
      const label =
        input?.closest(".field-row")?.querySelector("label")?.textContent ||
        path.at(-1);
      throw Error(`连接「${name}」的「${label}」格式无效，请修正后重试。`);
    }
  }
  const draft = { provider };
  if (provider.account)
    draft.account = valueAt(["model_api", "accounts"], {})[provider.account];
  const env = providerKeyName(provider) || draft.account?.key_env;
  const secret = resourceEdits.get("credentials:" + env);
  if (secret) draft.key = secret.op === "replace_secret" ? secret.value : "";
  if (editing?.secret) draft.key = editing.secret;
  return draft;
}
function focusProbeField(name, field) {
  if (activeEditor?.kind !== "providers" || activeEditor.name !== name) return;
  const input =
    field === "key"
      ? $("connection-key")
      : $("entry-body").querySelector(`[data-connection-field="${field}"]`) ||
        document.getElementById(key(["model_api", "providers", name, field]));
  if (!input) return;
  for (let parent = input.parentElement; parent; parent = parent.parentElement)
    if (parent.tagName === "DETAILS") parent.open = true;
  input.focus();
}
async function queryProvider(name, action) {
  const draft = providerDraft(name);
  const fingerprint = JSON.stringify(draft);
  const result = await api("/api/provider-query", {
    name,
    action,
    version: snapshot.version,
    draft,
  });
  if (JSON.stringify(providerDraft(name)) !== fingerprint)
    throw Error("连接或 API Key 已修改，请使用当前输入重新测试或查询。");
  if (!result.ok && result.field) focusProbeField(name, result.field);
  return result;
}
function providerTest(name) {
  const status = node("p", "", "provider-status");
  status.hidden = true;
  status.setAttribute("role", "status");
  const provider = valueAt(["model_api", "providers"], {})[name];
  const label = providerAuthentication(provider) === "oauth"
    ? "检查登录"
    : provider?.account ? "检查凭据" : "测试连接";
  const control = button(
    label,
    async () => {
      control.disabled = true;
      control.textContent = "检查中…";
      status.hidden = false;
      status.textContent = "正在使用当前连接设置检查，不保存修改…";
      status.dataset.state = "loading";
      try {
        const result = await queryProvider(name, "test");
        if (!control.isConnected) return;
        status.textContent = result.message;
        status.dataset.state = result.ok ? "success" : "error";
      } catch (error) {
        if (control.isConnected) {
          status.textContent = error.message;
          status.dataset.state = "error";
        }
      } finally {
        control.disabled = false;
        control.textContent = label;
      }
    },
    "secondary",
  );
  return { button: control, status };
}

const modelTestResults = new Map();
function configurationRequestId() {
  // getRandomValues also works on remote HTTP management pages.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
function modelTest(name) {
  // Completed results belong to the tested draft; a rebuilt form starts fresh.
  if (modelTestResults.has(name) && !modelTestResults.get(name).pending)
    modelTestResults.delete(name);
  const status = node("p", "", "provider-status");
  status.setAttribute("role", "status");
  const current = () => {
    const entry = valueAt(["model_api", "entries"], {})[name];
    if (!entry?.provider || !entry?.model)
      throw Error("请先填写模型 ID 和使用连接。");
    return {
      entry: structuredClone(entry),
      connection: providerDraft(entry.provider),
    };
  };
  const control = button(
    "测试模型",
    async () => {
      let draft;
      try {
        draft = current();
      } catch (error) {
        status.hidden = false;
        status.textContent = error.message;
        status.dataset.state = "error";
        return;
      }
      const state = {
        pending: true,
        message: "正在测试文本推理，最长等待 30 秒…",
        ok: null,
      };
      modelTestResults.set(name, state);
      show();
      try {
        const result = await api(
          "/api/model-test",
          {
            name,
            version: snapshot.version,
            request_id: configurationRequestId(),
            draft,
          },
          AbortSignal.timeout(45000),
        );
        if (JSON.stringify(current()) !== JSON.stringify(draft)) {
          state.ok = false;
          state.message =
            "设置已修改，本次结果属于测试开始时的设置，请重新测试。";
        } else {
          state.ok = result.ok;
          state.message = result.message;
          if (Number.isFinite(result.elapsed_ms))
            state.message += ` 耗时 ${(result.elapsed_ms / 1000).toFixed(1)} 秒。`;
          if (result.ok && result.reply)
            state.message += ` 回复：${result.reply}`;
        }
      } catch (error) {
        state.ok = false;
        state.message = ["TimeoutError", "AbortError"].includes(error.name)
          ? "未能取得测试结果，请检查服务连接；不会自动重试。"
          : error.message;
      } finally {
        state.pending = false;
        show();
        // A tab switch may have replaced this row while the request was in flight.
        for (const row of document.querySelectorAll(
          '.entity-grid[data-kind="entries"] .entity-card',
        )) {
          if (row.dataset.entity === name)
            row.dispatchEvent(new Event("model-test-result"));
        }
      }
    },
    "secondary",
  );
  function show() {
    const state = modelTestResults.get(name);
    status.hidden = !state;
    status.textContent = state?.message || "";
    status.dataset.state = state?.pending
      ? "loading"
      : state?.ok
        ? "success"
        : "error";
    control.disabled = !!state?.pending;
    control.textContent = state?.pending ? "测试中…" : "测试模型";
  }
  // Register after insertion so reconstructed rows can receive the completed result.
  queueMicrotask(() =>
    control
      .closest(".entity-card")
      ?.addEventListener("model-test-result", show),
  );
  show();
  return { button: control, status };
}
// Reuse the collection filter and model directory styles for finite local choices.
function searchableSelect(label, filterLabel, identity, signal) {
  const root = node("div", undefined, "searchable-select");
  const trigger = button(
    "请选择…",
    () => toggle(panel.hidden),
    "secondary searchable-select-trigger",
  );
  trigger.setAttribute("aria-label", label);
  trigger.setAttribute("aria-haspopup", "dialog");
  trigger.setAttribute("aria-expanded", "false");
  const panel = node("div", undefined, "searchable-select-panel");
  panel.id = "searchable-" + identity;
  panel.hidden = true;
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", label + "筛选");
  trigger.setAttribute("aria-controls", panel.id);
  const list = node("div", undefined, "model-result-list");
  list.setAttribute("role", "group");
  list.setAttribute("aria-label", label + "候选");
  root.append(node("span", label, "searchable-select-label"), trigger, panel);
  let selected = "",
    search;
  function toggle(open, focus = true) {
    panel.hidden = !open;
    if (open) {
      const anchor = root.getBoundingClientRect();
      const boundary = root
        .closest("#entry-body, dialog")
        ?.getBoundingClientRect();
      const below =
        Math.min(
          window.innerHeight - 16,
          boundary?.bottom ?? window.innerHeight,
        ) - anchor.bottom;
      const above = anchor.top - Math.max(16, boundary?.top ?? 0);
      root.classList.toggle(
        "opens-up",
        panel.offsetHeight > below && above > below,
      );
    }
    trigger.setAttribute("aria-expanded", String(open));
    if (focus) (open ? search : trigger)?.focus();
  }
  Object.defineProperty(root, "value", {
    get: () => selected,
    set: (value) => {
      const row = [...list.children].find((item) => item.dataset.value === value);
      selected = row ? value : "";
      trigger.textContent = row?.textContent || "请选择参考模型…";
      for (const item of list.children) item.setAttribute("aria-pressed", String(item === row));
      root.dispatchEvent(new Event("change", { bubbles: true }));
    },
  });
  Object.defineProperty(root, "disabled", {
    get: () => trigger.disabled,
    set: (value) => {
      trigger.disabled = value;
      if (value) toggle(false, false);
    },
  });
  root.setOptions = (options) => {
    selected = "";
    trigger.textContent = "请选择参考模型…";
    toggle(false, false);
    list.replaceChildren();
    collectionQueries.delete(identity);
    for (const option of options) {
      const row = button(
        option.label,
        () => {
          selected = option.value;
          trigger.textContent = option.label;
          for (const item of list.children)
            item.setAttribute("aria-pressed", String(item === row));
          toggle(false);
          root.dispatchEvent(new Event("change", { bubbles: true }));
        },
        "searchable-select-option",
      );
      row.dataset.value = option.value;
      row.dataset.search = option.search || option.label;
      row.setAttribute("aria-pressed", "false");
      list.append(row);
    }
    const [filter, empty] = collectionFilter(list, filterLabel, identity);
    search = filter.querySelector("input");
    panel.replaceChildren(filter, empty, list);
    search.addEventListener("input", () => {
      list.scrollTop = 0;
    });
    root.disabled = !options.length;
  };
  panel.addEventListener("keydown", (event) => {
    const rows = [...list.children].filter((row) => !row.hidden);
    const index = rows.indexOf(document.activeElement);
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      toggle(false);
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const next =
        index < 0
          ? event.key === "ArrowDown"
            ? 0
            : rows.length - 1
          : (index + (event.key === "ArrowDown" ? 1 : -1) + rows.length) %
            rows.length;
      rows[next]?.focus();
    } else if (event.key === "Enter" && event.target === search) {
      event.preventDefault();
      rows[0]?.click();
    }
  });
  root.addEventListener("focusout", (event) => {
    if (event.relatedTarget && !root.contains(event.relatedTarget))
      toggle(false, false);
  });
  document.addEventListener(
    "pointerdown",
    (event) => {
      if (!root.contains(event.target)) toggle(false, false);
    },
    { signal },
  );
  root.setOptions([]);
  return root;
}

function modelPicker(form, provider, model, name) {
  const box = node("section", undefined, "model-picker");
  box.setAttribute("aria-label", "供应商模型查询");
  const toolbar = node("div", undefined, "model-query-toolbar");
  const status = node(
    "p",
    "查询目录后选择模型，也可以直接填写模型 ID。",
    "hint",
  );
  status.setAttribute("role", "status");
  const panel = node("div", undefined, "model-results");
  panel.id = "setup-model-results";
  panel.hidden = true;
  const search = node("input");
  search.type = "search";
  search.placeholder = "搜索模型名称或 ID";
  search.setAttribute("aria-label", "搜索查询到的模型");
  const count = node("span", "", "hint");
  const list = node("div", undefined, "model-result-list");
  list.setAttribute("role", "group");
  list.setAttribute("aria-label", "可选择的供应商模型");
  panel.append(search, count, list);
  let rows = [],
    generation = 0;
  let selectedMetadata;
  const fold = button(
    "收起列表",
    () => {
      panel.hidden = !panel.hidden;
      fold.textContent = panel.hidden ? "展开列表" : "收起列表";
      fold.setAttribute("aria-expanded", String(!panel.hidden));
    },
    "subtle",
  );
  fold.setAttribute("aria-controls", panel.id);
  fold.hidden = true;
  function draw() {
    list.replaceChildren();
    const query = search.value.trim().toLowerCase();
    const matches = rows.filter((row) =>
      (row.id + " " + row.label).toLowerCase().includes(query),
    );
    count.textContent = `${matches.length} / ${rows.length} 个模型`;
    if (!matches.length)
      list.append(
        node("p", "没有匹配的模型，可调整搜索或手工填写 ID。", "hint"),
      );
    for (const row of matches) {
      const item = node("div", undefined, "model-result");
      const text = node("div");
      text.append(node("strong", row.label));
      if (row.label !== row.id) text.append(node("code", row.id));
      const selected = model.value === row.id;
      const choose = button(
        selected ? "已选中" : "选择",
        () => {
          model.value = row.id;
          selectedMetadata = { id: row.id, capabilities: row.capabilities };
          model.dispatchEvent(new Event("input", { bubbles: true }));
          if (!name.value.trim()) {
            let alias = row.id.toLowerCase().replace(/[^a-z0-9_.-]+/g, "-");
            if (!/^[a-z]/.test(alias)) alias = "model-" + alias;
            name.value = alias.slice(0, 64);
          }
          status.textContent =
            "已选择 " + row.id + "，可确认或修改 PA 中的名称。";
          panel.hidden = true;
          fold.textContent = "展开列表";
          fold.setAttribute("aria-expanded", "false");
          name.focus();
          draw();
        },
        selected ? "secondary selected" : "secondary",
      );
      choose.setAttribute(
        "aria-label",
        (selected ? "已选中 " : "选择 ") + row.id,
      );
      choose.setAttribute("aria-pressed", String(selected));
      item.append(text, choose);
      list.append(item);
    }
  }
  const load = button("查询模型", async () => {
    const mine = ++generation,
      selected = provider.value;
    const alive = () =>
      mine === generation && box.isConnected && $("setup-dialog").open;
    load.disabled = true;
    load.textContent = "查询中…";
    box.setAttribute("aria-busy", "true");
    rows = [];
    panel.hidden = fold.hidden = true;
    status.textContent = "正在使用当前连接设置查询模型目录…";
    status.dataset.state = "loading";
    try {
      const result = await queryProvider(selected, "models");
      if (!alive()) return;
      status.textContent =
        result.message +
        (result.truncated
          ? " 当前仅显示部分目录，可手工填写其他模型 ID。"
          : "");
      status.dataset.state = result.ok ? "success" : "error";
      if (result.ok) {
        rows = result.models || [];
        search.value = "";
        panel.hidden = false;
        fold.hidden = !rows.length;
        fold.textContent = "收起列表";
        fold.setAttribute("aria-expanded", "true");
        draw();
        if (rows.length) search.focus();
      }
    } catch (error) {
      if (alive()) {
        status.textContent = error.message;
        status.dataset.state = "error";
      }
    } finally {
      if (alive()) {
        load.disabled = false;
        load.textContent = "查询模型";
        box.setAttribute("aria-busy", "false");
      }
    }
  });
  provider.addEventListener("change", () => {
    ++generation;
    rows = [];
    selectedMetadata = undefined;
    model.value = "";
    panel.hidden = fold.hidden = true;
    load.disabled = false;
    load.textContent = "查询模型";
    box.setAttribute("aria-busy", "false");
    status.dataset.state = "";
    status.textContent = "连接已切换，请重新查询或填写模型 ID。";
  });
  $("setup-dialog").addEventListener(
    "close",
    () => {
      ++generation;
    },
    { once: true },
  );
  search.addEventListener("input", draw);
  model.addEventListener("input", () => {
    if (rows.length) draw();
  });
  toolbar.append(load, fold);
  box.append(toolbar, status, panel);
  form.insertBefore(box, model.closest("label"));
  return () =>
    selectedMetadata?.id === model.value
      ? selectedMetadata.capabilities
      : undefined;
}
function openAddEntity(kind) {
  const form = wizard(
    "添加模型",
    "模型 ID 来自供应商；名称是你在这个实例里选择模型时使用的标识。",
  );
  const providers = Object.keys(valueAt(["model_api", "providers"], {}));
  if (!providers.length) {
    form.append(
      emptyState(
        "先添加一个连接",
        "模型需要通过已配置的连接访问。",
        "去连接",
        () => {
          $("setup-dialog").close();
          modelTab = "connections";
          render();
        },
      ),
    );
    return;
  }
  const provider = formInput(form, "provider", "使用连接", {
    required: true,
    choices: providers.map((p) => {
      const item = valueAt(["model_api", "providers"], {})[p];
      return [
        p,
        p +
          " · " +
          (providerAuthentication(item) === "oauth" ? "OAuth 登录" : "API Key") +
          (item.enabled ? "" : "（未启用）"),
      ];
    }),
  });
  const model = formInput(form, "model", "供应商模型 ID", {
    required: true,
    placeholder: "填写供应商提供的模型 ID",
  });
  const name = formInput(form, "name", "在 PA 中的名称", {
    required: true,
    pattern: "[A-Za-z](?:[A-Za-z0-9_.]|-){0,63}",
    placeholder: "例如 main 或 fast",
    hint: "支持点号，例如 deepseek-v4.1-flash；保持名称稳定，用途绑定会引用它。",
  });
  $("setup-dialog").dataset.mode = "model";
  const selectedMetadata = modelPicker(form, provider, model, name);
  const details = node("div", undefined, "model-details");
  details.append(model.closest("label"), name.closest("label"));
  form.append(details);
  wizardFooter(form, "添加并完善设置");
  form.onsubmit = async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const id = name.value.trim();
    if (Object.hasOwn(valueAt(["model_api", kind], {}), id)) {
      showNotice("此名称已存在。");
      return;
    }
    const submitted = [provider.value, model.value.trim()];
    const submit = form.querySelector('[type="submit"]');
    submit.disabled = true;
    let capabilities;
    try {
      capabilities = await describeModel(...submitted, "", selectedMetadata());
      if (
        !form.isConnected ||
        !$("setup-dialog").open ||
        JSON.stringify(submitted) !==
          JSON.stringify([provider.value, model.value.trim()])
      )
        return;
    } catch (error) {
      showNotice(error.message);
      return;
    } finally {
      submit.disabled = false;
    }
    const checkpoint = draftCheckpoint();
    changed(["model_api", kind, id], "set", {
      provider: provider.value,
      model: model.value.trim(),
      default_reasoning_effort: "auto",
      ...modelCapabilityValues(capabilities),
      capabilities,
    });
    $("setup-dialog").close();
    render();
    openEntity(kind, id, checkpoint);
  };
}
function openConnectionWizard() {
  const form = wizard(
    "添加模型连接",
    "先连接服务，随后可以为它添加一个或多个模型。",
  );
  const route = formInput(form, "route", "认证方式", {
    cards: true,
    choices: [
      ["api", "API Key", "官方或第三方接口，填写密钥即可，无需账号登录"],
      ["native", "OAuth 登录", "使用供应商支持的账号登录授权"],
    ],
  });
  const name = formInput(form, "name", "连接名称", {
    required: true,
    pattern: "[A-Za-z](?:[A-Za-z0-9_]|-){0,63}",
    placeholder: "例如 openai 或 team",
    hint: "可使用大小写英文字母、数字、下划线或连字符，以字母开头；连接可先单独保存。",
  });
  const native = node("fieldset", undefined, "wizard-route");
  form.append(native);
  const provider = formInput(native, "provider", "供应商", {
    required: true,
    placeholder: "选择常用供应商，或输入其他供应商标识",
    suggestions: [
      ["openai", "OpenAI"],
      ["anthropic", "Anthropic"],
      ["google", "Google"],
    ],
    hint: "可输入其他 Pi 支持的供应商；认证方式需与供应商匹配。",
  });
  const apiSection = node("fieldset", undefined, "wizard-route");
  form.append(apiSection);
  const apiFields = apiConnectionFields(
    apiSection,
    { enabled: true },
    { prefix: "setup-api", inputId: "setup-api-key" },
  );
  const advanced = node("details", undefined, "connection-auth-options");
  advanced.append(node("summary", "高级适配"));
  const nativeApi = formInput(advanced, "native-api", "使用 Pi 原生供应商适配", {
    type: "checkbox",
    hint: "仅在需要供应商专用接入时使用。仍使用 API Key，无需账号登录；模型目录来自 Pi 本地。",
  });
  form.append(advanced);
  const nativeKeyBox = node("fieldset", undefined, "wizard-route");
  form.append(nativeKeyBox);
  const nativeKey = connectionCredentials(nativeKeyBox, undefined, {
    prefix: "setup-native", inputId: "setup-native-key",
  });
  const note = node("p", "", "inline-note");
  form.append(note);
  function refresh() {
    const oauth = route.value === "native";
    const useNative = oauth || nativeApi.checked;
    native.hidden = !useNative;
    native.disabled = !useNative;
    apiSection.hidden = useNative;
    apiSection.disabled = useNative;
    advanced.hidden = oauth;
    nativeApi.disabled = oauth;
    nativeKeyBox.hidden = oauth || !nativeApi.checked;
    nativeKeyBox.disabled = nativeKeyBox.hidden;
    note.textContent = oauth
      ? "OAuth 登录需要先保存，再在供应商认证区域完成登录。不会自动发起模型请求。"
      : useNative
        ? "使用供应商 API Key，无需登录。可以直接检查草稿凭据，实际可用性请通过模型测试确认。"
        : "填写供应商接口和凭据，可以直接测试草稿。保存并生效后用于运行。";
  }
  route.addEventListener("change", refresh);
  nativeApi.addEventListener("change", refresh);
  refresh();
  wizardFooter(form, "添加连接");
  form.onsubmit = (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const oauth = route.value === "native",
      useNative = oauth || nativeApi.checked,
      id = name.value.trim(),
      accounts = valueAt(["model_api", "accounts"], {});
    if (
      Object.hasOwn(valueAt(["model_api", "providers"], {}), id) ||
      (useNative && Object.hasOwn(accounts, id))
    ) {
      showNotice("此连接名称或账号名称已存在，请使用其他名称。");
      return;
    }
    if (
      useNative &&
      !/^[a-z][a-z0-9_-]{0,63}$/.test(provider.value.trim())
    ) {
      showNotice("请填写有效的供应商标识。");
      return;
    }
    const apiValue = apiFields.read();
    const nativeValue = nativeKey.read();
    const env = useNative ? nativeValue.reference : apiValue.provider.key_env;
    const secret = oauth ? "" : useNative ? nativeValue.secret : apiValue.secret;
    if (useNative) {
      const account = { provider: provider.value.trim(), auth: oauth ? "oauth" : "api_key" };
      if (oauth && account.provider === "openai")
        account.output_limit = "model";
      if (!oauth) account.key_env = env;
      changed(["model_api", "accounts", id], "set", account);
      changed(["model_api", "providers", id], "set", {
        account: id,
        enabled: true,
      });
    } else changed(["model_api", "providers", id], "set", apiValue.provider);
    if (secret)
      resourceEdits.set("credentials:" + env, {
        id: "credentials",
        key: env,
        op: "replace_secret",
        value: secret,
      });
    $("setup-dialog").close();
    form.replaceChildren();
    render();
    showNotice(
      "连接已加入草稿，可直接测试连接或查询模型；保存并生效后用于运行。OAuth 登录需先保存，再完成授权。",
    );
  };
}
