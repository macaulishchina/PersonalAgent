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
function moveTo(page, tab) {
  if (page !== selected && (dirty() || invalid.size)) {
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
    else for (const map of [edits, invalid, drafts, resourceEdits]) map.clear();
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
    const accounts = valueAt(["model_api", "accounts"], {}),
      providers = valueAt(["model_api", "providers"], {});
    const unbound = Object.keys(accounts).filter(
      (name) => !Object.values(providers).some((p) => p.account === name),
    );
    if (unbound.length) {
      const spare = advancedGroup("unbound-accounts");
      spare.firstChild.textContent = "未绑定的账号";
      for (const name of unbound)
        spare.append(
          button(name + " · 编辑账号", () => openEntity("accounts", name)),
        );
      panel.append(spare);
    }
    $("documents").append(panel);
    const secrets = advancedGroup("model-secrets");
    secrets.firstChild.textContent = "凭据与环境变量";
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
      if (field) panel.append(drawField(prettyField(field)));
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
      "默认设置适合大多数情况；仅在有明确需求时调整。",
    );
    for (const field of snapshot.fields.filter(
      (f) =>
        fieldGroup(f) === "models" &&
        !["accounts", "providers", "entries", "enabled", "memory"].includes(
          f.path[1],
        ) &&
        f.path.length > 1,
    ))
      panel.append(drawField(prettyField(field)));
    drawActions(
      inventory.plugins.find((p) => p.id === "models"),
      panel,
    );
    $("documents").append(panel);
    drawResources(undefined, (r) => r.id === "gateway_key");
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
          ? "添加一个连接，账号和 API Key 都可以。"
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
    ]
      .filter(Boolean)
      .join(" ");
    const head = node("div", undefined, "entity-heading");
    head.append(
      node("span", name.slice(0, 2).toUpperCase(), "entity-avatar"),
      node("h3", name),
    );
    if (kind === "providers")
      head.append(
        node(
          "span",
          value.enabled ? "已启用" : "未启用",
          value.enabled ? "badge configured" : "badge",
        ),
      );
    else if (valueAt(["main_model"], "") === name)
      head.append(node("span", "主对话", "badge configured"));
    card.append(
      head,
      node(
        "p",
        kind === "providers"
          ? value.account
            ? "原生账号 · " + value.account
            : "API 网关 · " + (value.preset || value.protocol || "待配置")
          : value.model || "尚未填写模型 ID",
        "entity-description",
      ),
    );
    if (kind === "entries") {
      const chips = node("div", undefined, "entity-tags");
      chips.append(node("span", value.provider || "未选择连接", "badge"));
      if (value.images) chips.append(node("span", "图片", "badge"));
      chips.append(
        node(
          "span",
          value.execution_validated ? "执行已验收" : "执行待验收",
          "badge",
        ),
      );
      card.append(chips);
    }
    const actions = node("div", undefined, "entity-actions");
    actions.append(button("编辑", () => openEntity(kind, name)));
    if (kind === "providers" && value.account)
      actions.append(
        button(
          "账号设置",
          () => openEntity("accounts", value.account),
          "subtle",
        ),
      );
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
      const editor = drawField(displayed);
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
function renderEntityEditor() {
  if (!activeEditor) return;
  const { kind, name, field } = activeEditor;
  $("entry-title").textContent =
    { providers: "编辑连接", entries: "编辑模型", accounts: "编辑账号" }[kind] +
    " · " +
    name;
  $("entry-description").textContent =
    "修改先保留在本页，完成后统一保存并生效。";
  const copy = structuredClone(field),
    props = copy.schema.properties;
  if (kind === "entries") {
    props.provider.title = "使用连接";
    props.model.title = "供应商模型 ID";
    for (const [name, definition] of Object.entries(props))
      definition["x-advanced"] = ![
        "provider",
        "model",
        "default_reasoning_effort",
      ].includes(name);
    if (props.execution_validated)
      props.execution_validated.description =
        "只有实际验证过工具执行能力后才开启，添加模型不代表已经通过验收。";
  }
  if (kind === "providers")
    for (const name of ["key_env", "auth_scheme", "session_header"])
      if (props[name]) props[name]["x-advanced"] = true;
  $("entry-body").replaceChildren(drawField(copy));
  if (kind === "providers") {
    const value = currentValue(field),
      name = value.key_env;
    if (name) $("entry-body").append(credentialInput(name));
  } else if (kind === "accounts") {
    const value = currentValue(field);
    if (value.key_env) $("entry-body").append(credentialInput(value.key_env));
  }
}
function finishEntity(discard = false) {
  if (!activeEditor) return;
  if (
    !discard &&
    [...invalid.keys()].some(
      (id) =>
        JSON.parse(id).slice(0, 3).join(".") ===
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
function credentialInput(name) {
  const container = node("div", undefined, "inline-credential"),
    input = node("input"),
    label = node("label", "API Key");
  const info = resourceInventory
      .find((r) => r.id === "credentials")
      ?.keys?.find((k) => k.name === name),
    id = "credentials:" + name;
  input.type = "password";
  input.id = "connection-key";
  input.autocomplete = "new-password";
  label.htmlFor = input.id;
  input.placeholder = info?.configured ? "已保存，留空保持" : "输入 API Key";
  input.disabled = info?.source === "environment";
  input.value = resourceEdits.get(id)?.value || "";
  input.addEventListener("input", () => {
    if (input.value)
      resourceEdits.set(id, {
        id: "credentials",
        key: name,
        op: "replace_secret",
        value: input.value,
      });
    else resourceEdits.delete(id);
    updateSaveBar();
  });
  container.append(
    label,
    input,
    node(
      "p",
      info?.source === "environment"
        ? "此凭据由服务器环境变量提供。"
        : "单独保存为私有凭据，不写入普通配置。",
      "hint",
    ),
  );
  return container;
}
function wizard(title, detail) {
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
    choices: providers.map((p) => [p, p]),
  });
  const model = formInput(form, "model", "供应商模型 ID", {
    required: true,
    placeholder: "填写供应商提供的模型 ID",
  });
  const name = formInput(form, "name", "在 PA 中的名称", {
    required: true,
    pattern: "[a-z][a-z0-9_-]{0,63}",
    placeholder: "例如 main 或 fast",
    hint: "保持名称稳定，用途绑定会引用它。",
  });
  wizardFooter(form, "添加并完善设置");
  form.onsubmit = (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const id = name.value.trim();
    if (Object.hasOwn(valueAt(["model_api", kind], {}), id)) {
      showNotice("此名称已存在。");
      return;
    }
    const checkpoint = draftCheckpoint();
    changed(["model_api", kind, id], "set", {
      provider: provider.value,
      model: model.value.trim(),
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
  const route = formInput(form, "route", "接入方式", {
    cards: true,
    choices: [
      ["native", "直接连接账号", "OAuth / API Key，无需部署网关"],
      ["gateway", "通过 API 网关", "预设或自定义接口，需要单独部署网关"],
    ],
  });
  const name = formInput(form, "name", "连接名称", {
    required: true,
    pattern: "[a-z][a-z0-9_-]{0,63}",
    placeholder: "例如 openai 或 team",
    hint: "用于识别连接，不是模型 ID。",
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
  const auth = formInput(native, "auth", "认证方式", {
    choices: [
      ["api_key", "API Key"],
      ["oauth", "账号登录（OAuth）"],
    ],
  });
  const credential = formInput(native, "credential", "API Key", {
    type: "password",
    placeholder: "粘贴 API Key，或保存后通过账号授权录入",
  });
  const gateway = node("fieldset", undefined, "wizard-route");
  form.append(gateway);
  const presets = fieldAt(["model_api", "providers"]).schema
    .additionalProperties.properties.preset.enum;
  const preset = formInput(gateway, "preset", "接口类型", {
    choices: [...presets.map((p) => [p, p]), ["custom", "自定义接口"]],
  });
  const protocol = formInput(gateway, "protocol", "协议", {
    choices: fieldAt([
      "model_api",
      "providers",
    ]).schema.additionalProperties.properties.protocol.enum.map((p) => [p, p]),
  });
  const url = formInput(gateway, "url", "接口地址", {
    type: "url",
    placeholder: "https://…",
  });
  const gatewayKey = formInput(gateway, "gateway-key", "API Key", {
    type: "password",
    placeholder: "可选，保存为私有凭据",
  });
  const note = node("p", "", "inline-note");
  form.append(note);
  function refresh() {
    const useNative = route.value === "native";
    native.hidden = !useNative;
    native.disabled = !useNative;
    gateway.hidden = useNative;
    gateway.disabled = useNative;
    credential.closest("label").hidden = auth.value === "oauth";
    credential.disabled = auth.value === "oauth";
    const custom = preset.value === "custom";
    for (const input of [protocol, url]) {
      input.closest("label").hidden = !custom;
      input.disabled = !custom;
    }
    url.required = !useNative && custom;
    note.textContent = useNative
      ? "账号和连接将一起添加。OAuth 授权需要先保存，再在账号授权区域登录；不会自动发起模型请求。"
      : "此路线需要单独部署模型网关。配置保存后，按生效状态中的提示完成外部部署。";
  }
  for (const input of [route, auth, preset])
    input.addEventListener("change", refresh);
  refresh();
  wizardFooter(form, "添加连接");
  form.onsubmit = (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const id = name.value.trim(),
      accounts = valueAt(["model_api", "accounts"], {});
    if (
      Object.hasOwn(valueAt(["model_api", "providers"], {}), id) ||
      (route.value === "native" && Object.hasOwn(accounts, id))
    ) {
      showNotice("此连接名称或账号名称已存在，请使用其他名称。");
      return;
    }
    if (
      route.value === "native" &&
      !/^[a-z][a-z0-9_-]{0,63}$/.test(provider.value.trim())
    ) {
      showNotice("请填写有效的供应商标识。");
      return;
    }
    const env = "PA_" + id.toUpperCase().replaceAll("-", "_") + "_API_KEY",
      secret =
        route.value === "native"
          ? auth.value === "api_key"
            ? credential.value
            : ""
          : gatewayKey.value;
    const existingKey = resourceInventory
      .find((r) => r.id === "credentials")
      ?.keys?.find((k) => k.name === env);
    if (secret && (existingKey || resourceEdits.has("credentials:" + env))) {
      showNotice(
        "此名称对应的凭据已经存在，请更换连接名称，避免覆盖其他连接的凭据。",
      );
      return;
    }
    if (route.value === "native") {
      const account = { provider: provider.value.trim(), auth: auth.value };
      if (auth.value === "oauth" && account.provider === "openai")
        account.output_limit = "model";
      if (secret) account.key_env = env;
      changed(["model_api", "accounts", id], "set", account);
      changed(["model_api", "providers", id], "set", {
        account: id,
        enabled: true,
      });
    } else
      changed(
        ["model_api", "providers", id],
        "set",
        preset.value === "custom"
          ? {
              protocol: protocol.value,
              base_url: url.value.trim(),
              key_env: env,
              enabled: true,
            }
          : { preset: preset.value, key_env: env, enabled: true },
      );
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
    showNotice("连接已加入草稿。保存后可授权账号，并继续添加模型。");
  };
}
