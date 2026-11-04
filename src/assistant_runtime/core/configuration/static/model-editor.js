"use strict";

// Capability facts are route-bound snapshots. Explicit fields are operator overrides.
async function describeModel(providerName, model, reference = "", observed) {
  const provider = valueAt(["model_api", "providers"], {})[providerName];
  const account = provider?.account
    ? valueAt(["model_api", "accounts"], {})[provider.account]
    : undefined;
  const identity = JSON.stringify([provider, account]);
  const result = await api("/api/model-capabilities", {
    provider,
    account,
    model,
    reference,
    observed,
    version: snapshot.version,
  });
  const current = valueAt(["model_api", "providers"], {})[providerName];
  if (
    identity !==
    JSON.stringify([
      current,
      current?.account
        ? valueAt(["model_api", "accounts"], {})[current.account]
        : undefined,
    ])
  )
    throw Error("连接设置已修改，请重新识别模型能力。");
  return result.capabilities;
}

// Import fills ordinary settings; the snapshot records provenance, not another form.
function modelCapabilityValues(capabilities) {
  const values = {};
  for (const name of [
    "images",
    "structured",
    "context_window",
    "output_limit",
    "reasoning_efforts",
  ])
    if (Object.hasOwn(capabilities, name))
      values[name] = structuredClone(capabilities[name]);
  if (
    capabilities.reasoning === false &&
    !Object.hasOwn(values, "reasoning_efforts")
  )
    values.reasoning_efforts = [];
  return values;
}

function renderModelEditor(field) {
  const root = $("entry-body"),
    path = field.path;
  const current = () =>
    valueAt(["model_api", "entries"], {})[path.at(-1)] || {};
  function assign(name, value) {
    // Programmatic fills must replace the raw input cache as well as the draft.
    drafts.delete(key([...path, name]));
    changed([...path, name], "set", value);
  }
  const initial = current(),
    cap = initial.capabilities || {};
  const props = structuredClone(field.schema.properties);
  const levels = initial.reasoning_efforts ?? cap.reasoning_efforts ?? [];
  const selected = initial.default_reasoning_effort || "auto";
  props.provider.title = "使用连接";
  props.model.title = "供应商模型 ID";
  props.default_reasoning_effort.title = "默认思考模式";
  props.default_reasoning_effort.enum = [
    ...new Set(["auto", ...levels, selected]),
  ];
  props.default_reasoning_effort.default = "auto";
  props.default_reasoning_effort.description = levels.length
    ? "自动使用服务端默认策略；也可从上方启用的档位中选择默认值。"
    : "当前未设置可用档位，因此只有自动。可在上方按供应商说明启用档位，再选择默认值。";
  props.reasoning_efforts.title = "可用思考档位";
  props.reasoning_efforts.description =
    "勾选当前连接实际支持的档位，默认思考模式会同步更新。资料明确提供档位时会自动勾选；未标注时可按供应商说明设置。";
  const summary = node(
    "section",
    undefined,
    "model-parameters capability-summary",
  );
  summary.setAttribute("aria-label", "模型参数");
  summary.append(node("h3", "模型参数"));
  const source = node(
    "p",
    cap.source && cap.source !== "unknown"
      ? `最近采用：${cap.source} · ${cap.reference || initial.model}。下方参数可直接修改，再次填充会替换资料中已知的值。`
      : "可手动填写，也可搜索模型资料自动填充。没有资料也能保存。",
    "hint",
  );
  const status = node("p", "", "provider-status");
  status.setAttribute("role", "status");
  let busy = false;
  async function refresh(
    reference = current().capabilities?.source === "models.dev"
      ? current().capabilities.reference
      : "",
    observed,
  ) {
    if (busy) return;
    busy = true;
    status.textContent = "正在读取模型资料…";
    const identity = [current().provider, current().model];
    try {
      const value = await describeModel(...identity, reference, observed);
      if (
        !root.contains(summary) ||
        JSON.stringify(identity) !==
          JSON.stringify([current().provider, current().model])
      )
        return;
      const values = modelCapabilityValues(value);
      changed([...path, "capabilities"], "set", value);
      for (const [name, setting] of Object.entries(values))
        assign(name, setting);
      const mode = current().default_reasoning_effort || "auto";
      if (
        values.reasoning_efforts &&
        mode !== "auto" &&
        !values.reasoning_efforts.includes(mode)
      )
        assign("default_reasoning_effort", "auto");
      renderEntityEditor();
      updateSaveBar();
      const message = root.querySelector(".provider-status");
      message.textContent = Object.keys(values).length
        ? "已填入模型参数，未提供的字段保留原值。可以继续修改，保存后生效。"
        : "资料未提供可填充参数，现有值保持不变。可选择其他参考或手动填写。";
      root
        .querySelector(".model-parameters")
        .scrollIntoView({ block: "start" });
    } catch (error) {
      if (root.contains(summary)) {
        status.textContent = error.message;
        status.dataset.state = "error";
      }
    } finally {
      busy = false;
    }
  }
  const picker = renderCapabilityPicker(initial, refresh);
  summary.append(source, picker, status);
  const values = { ...modelCapabilityValues(cap), ...initial };
  function input(name) {
    return drawField({
      ...field,
      path: [...path, name],
      value: values[name],
      schema: props[name],
      loaded_value: field.loaded_value?.[name],
      explicit: Object.hasOwn(initial, name),
    });
  }
  const basic = node("section", undefined, "model-basic");
  for (const name of ["provider", "model"]) basic.append(input(name));
  const parameters = node("div", undefined, "model-parameter-inputs");
  for (const name of ["context_window", "output_limit", "images", "structured"])
    parameters.append(input(name));
  summary.append(parameters);
  const thinking = node("section", undefined, "model-thinking");
  thinking.append(
    node("h3", "思考设置"),
    input("reasoning_efforts"),
    input("default_reasoning_effort"),
  );
  const advanced = node("details", undefined, "advanced-fields model-budget");
  advanced.append(
    node("summary", "高级设置"),
    node("p", "可选的单次输出限制。0 表示由供应商／Pi 默认处理，与模型规格及累计用量限制独立。", "hint"),
  );
  for (const name of ["max_output_tokens", "execution_max_output_tokens"])
    advanced.append(input(name));
  root.replaceChildren(basic, renderModelPresentation(path, initial.presentation || {}), summary, thinking, advanced);
  // Generic controls commit and rebuild on input, before a later change event.
  // Reconcile dependent fields during capture so the rebuild sees one coherent draft.
  root.addEventListener(
    "input",
    (event) => {
      const row = event.target.closest("[data-field-path]");
      if (row?.dataset.fieldPath === key([...path, "reasoning_efforts"])) {
        const supported = [...row.querySelectorAll("input:checked")].map(
          (item) => item.value,
        );
        const mode = current().default_reasoning_effort || "auto";
        if (mode !== "auto" && !supported.includes(mode))
          assign("default_reasoning_effort", "auto");
        const focused = event.target.value;
        queueMicrotask(() =>
          root
            .querySelector(
              `.model-thinking input[value="${CSS.escape(focused)}"]`,
            )
            ?.focus(),
        );
      }
      if (row?.dataset.fieldPath === key([...path, "provider"])) {
        changed([...path, "capabilities"], "default");
      }
    },
    { capture: true, signal: editorInputController.signal },
  );
  root.addEventListener(
    "change",
    (event) => {
      if (
        event.target.closest("[data-field-path]")?.dataset.fieldPath ===
        key([...path, "model"])
      ) {
        changed([...path, "capabilities"], "default");
        renderEntityEditor();
        root.querySelector(".provider-status").textContent =
          "模型已更换，请核对参数或重新填充。";
      }
    },
    { signal: editorInputController.signal },
  );
  activeEditor.validate = () => {
    const value = current(),
      supported =
        value.reasoning_efforts ?? value.capabilities?.reasoning_efforts ?? [];
    const effort = value.default_reasoning_effort || "auto";
    if (effort !== "auto" && !supported.includes(effort)) {
      status.textContent = "请启用对应思考档位，或将默认思考模式改为自动。";
      return false;
    }
    return true;
  };
}

// Search only suggests records. Explicit selection + apply changes the local draft.
function renderCapabilityPicker(initial, apply) {
  const root = node("details", undefined, "capability-picker");
  root.open =
    !initial.capabilities?.source || initial.capabilities.source === "unknown";
  root.append(node("summary", "从模型资料填充"));
  root.append(
    node(
      "p",
      "搜索并选择参考模型，再填入下方参数。只替换资料中已知的值，不更换连接或模型 ID。",
      "hint",
    ),
  );
  const catalogBar = node("div", undefined, "capability-catalog-bar");
  const catalogStatus = node("span", "正在读取资料库状态…", "hint");
  catalogStatus.setAttribute("role", "status");
  const check = button("检查更新", () => updateCatalog(), "secondary");
  catalogBar.append(catalogStatus, check);
  root.append(catalogBar);
  const fields = node("fieldset", undefined, "capability-picker-fields");
  const query = formInput(fields, "capability-search", "搜索模型或服务商", {
    type: "search",
    value: initial.model || "",
    placeholder: "例如 deepseek-v4.1-flash",
  });
  query.maxLength = 200;
  const select = searchableSelect(
    "匹配的参考模型",
    "过滤服务商或模型（可输入原厂）",
    "capability-candidates",
    editorInputController.signal,
  );
  fields.append(select);
  const status = node("p", "", "hint");
  status.setAttribute("role", "status");
  const preview = node("div", undefined, "capability-preview");
  preview.hidden = true;
  const actions = node("div", undefined, "capability-actions");
  const adopt = button(
    "填入参数",
    async () => {
      const candidate = candidates.find(
        (item) => item.reference === select.value,
      );
      if (!candidate) return;
      fields.disabled = true;
      try {
        await apply(candidate.applyReference ?? candidate.reference);
      } finally {
        fields.disabled = false;
      }
    },
    "primary",
  );
  adopt.disabled = true;
  actions.append(adopt);
  fields.append(status, preview, actions);
  root.append(fields);
  let candidates = [],
    sequence = 0,
    timer,
    started = false,
    pollTimer,
    catalogRevision = "";
  function clear() {
    candidates = [];
    select.setOptions([]);
    adopt.disabled = true;
    preview.hidden = true;
  }
  function showCatalog(value) {
    if (!root.isConnected || !value) return;
    const date = value.retrieved_at ? new Date(value.retrieved_at).toLocaleString("zh-CN", { hour12: false }) : "未知";
    catalogStatus.textContent = `资料库更新于 ${date}` +
      (value.updating ? " · 正在后台更新…" : value.error ? " · 更新未完成，继续使用已有资料" : "");
    catalogStatus.dataset.state = value.error ? "error" : "";
    check.disabled = value.updating;
    clearTimeout(pollTimer);
    if (value.updating) pollTimer = setTimeout(pollCatalog, 800);
  }
  async function pollCatalog() {
    if (!root.isConnected) return;
    try {
      const result = await api("/api/model-capabilities", { action: "catalog", version: snapshot.version });
      showCatalog(result.catalog);
      if (!result.catalog.updating && result.catalog.revision !== catalogRevision)
        await search(false, true);
    } catch (error) {
      if (root.isConnected) {
        check.disabled = false;
        catalogStatus.textContent = error.message;
      }
    }
  }
  async function updateCatalog() {
    check.disabled = true;
    try {
      const result = await api("/api/model-capabilities", { action: "catalog", update: true, version: snapshot.version });
      showCatalog(result.catalog);
    } catch (error) {
      if (root.isConnected) { check.disabled = false; catalogStatus.textContent = error.message; }
    }
  }
  editorInputController.signal.addEventListener("abort", () => {
    ++sequence;
    clearTimeout(timer);
    clearTimeout(pollTimer);
  }, { once: true });
  async function search(update = true, preserve = false) {
    clearTimeout(timer);
    const id = ++sequence;
    const text = query.value.trim();
    const previous = preserve ? select.value : "";
    clear();
    if (!text) {
      status.textContent = "输入模型名，也可用服务商名称缩小范围。";
      return;
    }
    status.textContent = "正在查找模型资料…";
    try {
      const result = await api("/api/model-capabilities", {
        action: "search",
        query: text,
        update,
        version: snapshot.version,
      });
      if (id !== sequence || !root.isConnected) return;
      candidates = result.candidates;
      const provider = valueAt(["model_api", "providers"], {})[initial.provider];
      if (provider?.account || provider?.protocol === "pi") candidates.unshift({
        reference: "@connection", applyReference: "", provider: initial.provider,
        model: initial.model, origin: "connection", match: "exact", canonical_model_id: "",
        capabilities: initial.capabilities?.source === "Pi 1.4.0" ? initial.capabilities : {},
      });
      catalogRevision = result.revision;
      showCatalog(result.catalog);
      const origins = {
        connection: "当前连接的 Pi 资料",
        original: "原厂目录",
        other: "其他目录",
        unknown: "归属未标注",
      };
      select.setOptions(
        candidates.map((candidate) => {
          const label = `${candidate.model} · ${candidate.provider} · ${origins[candidate.origin] || origins.unknown}${candidate.match === "exact" ? " · 同名" : " · 相关"}`;
          return {
            value: candidate.reference,
            label,
            search:
              label +
              " " +
              candidate.canonical_model_id +
              (candidate.origin === "original" ? " 官方" : ""),
          };
        }),
      );
      const selection = candidates.find((item) => item.reference === previous) ||
        candidates.find((item) => item.reference === initial.capabilities?.reference) ||
        (initial.capabilities?.source === "Pi 1.4.0" ? candidates.find((item) => item.origin === "connection") : null) ||
        (candidates.length === 1 ? candidates[0] : null);
      if (selection) { select.value = selection.reference; }
      status.textContent = candidates.length
        ? `共 ${candidates.length} 项，全部可选。原厂目录优先；下拉中输入“原厂”可筛选。${candidates.some((item) => item.origin === "original") ? "" : " 当前匹配项没有已标注的原厂记录，可缩短模型名重新搜索。"}`
        : "暂无匹配资料。后台更新完成后会再次查找；也可缩短名称或手动填写，不影响保存。";
    } catch (error) {
      if (id === sequence && root.isConnected)
        status.textContent = error.message;
    }
  }
  select.addEventListener("change", () => {
    const candidate = candidates.find(
      (item) => item.reference === select.value,
    );
    adopt.disabled = !candidate;
    preview.hidden = !candidate;
    if (!candidate) return;
    const cap = candidate.capabilities;
    const truth = (v) =>
      v === true ? "支持" : v === false ? "不支持" : "未知";
    const number = (v) => (v ? v.toLocaleString() + " tokens" : "未知");
    const facts = node("dl", undefined, "capability-facts");
    for (const [label, value] of [
      ["上下文窗口", number(cap.context_window)],
      ["最大输出", number(cap.output_limit)],
      ["图片输入", truth(cap.images)],
      ["结构化输出", truth(cap.structured)],
      ["工具调用", truth(cap.tools)],
      ["推理能力", truth(cap.reasoning)],
      [
        "资料中的思考档位",
        cap.reasoning_efforts?.length
          ? cap.reasoning_efforts
              .map((v) => optionLabel({}, v, ["reasoning"]))
              .join("、")
          : "未标注",
      ],
    ])
      facts.append(node("dt", label), node("dd", value));
    preview.replaceChildren(
      node("strong", candidate.reference),
      node(
        "p",
        candidate.origin === "connection"
          ? "点击填入时读取当前连接的 Pi 模型资料，不发起推理。"
          : candidate.origin === "original"
          ? "资料来源：原厂目录"
          : candidate.origin === "other"
            ? "资料来源：其他目录（不代表非官方）"
            : "资料来源：上游未标注模型归属",
        "hint",
      ),
      node(
        "p",
        candidate.canonical_model_id
          ? `上游原始模型：${candidate.canonical_model_id}`
          : "上游未提供原始模型归属",
        "hint",
      ),
      facts,
      node(
        "p",
        candidate.origin === "connection"
          ? "来自当前连接的模型目录。没有提供的参数仍保留原值。"
          : "来源：models.dev 公开目录。仅借用能力规格，不代表当前连接已验收；思考档位仍按当前连接的适配能力确定。",
        "hint",
      ),
    );
  });
  query.addEventListener("input", () => {
    ++sequence;
    clear();
    status.textContent = "等待搜索…";
    clearTimeout(timer);
    timer = setTimeout(() => search(), 250);
  });
  query.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      search();
    }
  });
  root.addEventListener("toggle", () => {
    if (root.open && !started) {
      started = true;
      search();
    }
  });
  queueMicrotask(() => {
    if (!root.isConnected) return;
    if (root.open && !started) { started = true; search(); }
    else if (!root.open) updateCatalog();
  });
  return root;
}

// One model identity and grouped picker for every declared model reference.
function modelDisplayName(alias) {
  const entry = valueAt(["model_api", "entries"], {})[alias];
  return `${alias} · ${entry?.provider || "未配置 Provider"}`;
}
function modelReferencePicker(select, { auto = false } = {}) {
  const root = node("div", undefined, "model-reference"),
    popup = node("div", undefined, "model-reference-popup"),
    search = node("input", undefined, "model-reference-search"),
    tree = node("div", undefined, "model-reference-tree"),
    status = node("p", "", "model-reference-status");
  const entries = valueAt(["model_api", "entries"], {}),
    providers = valueAt(["model_api", "providers"], {}),
    groups = new Map(),
    specials = [],
    collapsed = new Set();
  const current = select.value;
  for (const option of [...select.options]) {
    const alias = option.value;
    if (!alias || (auto && alias === "auto")) {
      specials.push({ alias, label: option.textContent });
      continue;
    }
    const entry = entries[alias],
      provider = entry?.provider || "未配置 Provider";
    if (!groups.has(provider)) groups.set(provider, []);
    groups.get(provider).push({ alias, model: entry?.model || "" });
  }
  const ordered = [...groups].sort(([a], [b]) => a.localeCompare(b));
  select.replaceChildren();
  for (const item of specials) {
    const option = node("option", item.label);
    option.value = item.alias;
    select.append(option);
  }
  for (const [provider, models] of ordered) {
    const group = node("optgroup");
    group.label = provider;
    for (const item of models) {
      const option = node("option", modelDisplayName(item.alias));
      option.value = item.alias;
      group.append(option);
    }
    select.append(group);
  }
  select.value = current;
  select.title = select.selectedOptions[0]?.label || "";
  select.classList.add("model-reference-select");
  select.setAttribute("aria-haspopup", "tree");
  popup.id = "model-reference-" + configurationRequestId();
  popup.setAttribute("popover", "auto");
  tree.id = popup.id + "-tree";
  select.setAttribute("aria-controls", tree.id);
  select.setAttribute("aria-expanded", "false");
  search.type = "search";
  search.setAttribute("role", "searchbox");
  search.setAttribute(
    "aria-label",
    (select.getAttribute("aria-label") || "模型") + " · 搜索模型或 Provider",
  );
  search.placeholder = "搜索模型或 Provider";
  search.autocomplete = "off";
  tree.setAttribute("role", "tree");
  tree.setAttribute("aria-label", "按 Provider 分组的模型");
  status.setAttribute("role", "status");
  popup.append(search, tree, status);
  root.append(select, popup);
  if (!popup.showPopover) {
    popup.hidden = true;
    for (const name of ["aria-haspopup", "aria-controls", "aria-expanded"])
      select.removeAttribute(name);
    return root;
  }
  let watching;
  const opened = () => popup.matches(":popover-open");
  function close(restore = true) {
    if (opened()) popup.hidePopover();
    watching?.abort();
    if (restore && select.isConnected) select.focus();
  }
  function choose(alias) {
    close();
    if (select.value === alias) return;
    select.value = alias;
    select.title = select.selectedOptions[0]?.label || "";
    select.dispatchEvent(new Event("input", { bubbles: true }));
    select.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function leaf(alias, label, model, parent) {
    const item = node("button", undefined, "model-reference-option");
    item.type = "button";
    item.tabIndex = -1;
    item.setAttribute("role", "treeitem");
    item.setAttribute("aria-level", parent ? "2" : "1");
    item.setAttribute("aria-label", parent ? modelDisplayName(alias) : label);
    item.setAttribute("aria-selected", String(select.value === alias));
    item.dataset.value = alias;
    if (parent) item.dataset.provider = parent;
    const copy = node("span", undefined, "model-reference-option-copy");
    copy.append(node("strong", label));
    if (model && model !== alias) copy.append(node("small", model));
    item.append(
      copy,
      node("span", select.value === alias ? "✓" : "", "model-reference-check"),
    );
    item.addEventListener("click", () => choose(alias));
    return item;
  }
  function draw() {
    const query = search.value.trim().toLocaleLowerCase();
    tree.replaceChildren();
    let count = 0;
    for (const item of specials)
      if (!query || item.label.toLocaleLowerCase().includes(query))
        tree.append(leaf(item.alias, item.label));
    for (const [provider, models] of ordered) {
      const matches = models.filter((item) =>
        `${provider} ${item.alias} ${item.model}`
          .toLocaleLowerCase()
          .includes(query),
      );
      if (!matches.length) continue;
      count += matches.length;
      const group = node("div", undefined, "model-reference-group"),
        heading = node("button", undefined, "model-reference-provider"),
        children = node("div", undefined, "model-reference-children");
      heading.type = "button";
      heading.tabIndex = -1;
      heading.setAttribute("role", "treeitem");
      heading.setAttribute("aria-level", "1");
      heading.setAttribute("aria-label", provider);
      heading.setAttribute("aria-expanded", String(!collapsed.has(provider)));
      children.id =
        popup.id + "-group-" + ordered.findIndex(([name]) => name === provider);
      heading.setAttribute("aria-controls", children.id);
      heading.setAttribute("aria-owns", children.id);
      heading.dataset.provider = provider;
      heading.append(
        node(
          "span",
          collapsed.has(provider) ? "›" : "⌄",
          "model-reference-caret",
        ),
        node("strong", provider),
      );
      if (providers[provider]?.enabled === false)
        heading.append(node("small", "未启用", "model-reference-disabled"));
      heading.append(
        node("span", String(matches.length), "model-reference-count"),
      );
      children.setAttribute("role", "group");
      children.setAttribute("aria-label", provider + " 的模型");
      children.hidden = collapsed.has(provider);
      for (const item of matches)
        children.append(leaf(item.alias, item.alias, item.model, provider));
      heading.addEventListener("click", () => {
        if (collapsed.has(provider)) collapsed.delete(provider);
        else collapsed.add(provider);
        draw();
        [...tree.querySelectorAll(".model-reference-provider")]
          .find((item) => item.dataset.provider === provider)
          ?.focus();
      });
      group.append(heading, children);
      tree.append(group);
    }
    status.textContent = count
      ? `${count} 个模型 · ↑↓ 选择，Enter 确认，Esc 关闭`
      : "没有匹配的模型";
  }
  function position() {
    const rect = select.getBoundingClientRect(),
      margin = 12,
      gap = 6;
    if (rect.bottom < 0 || rect.top > window.innerHeight) {
      close(false);
      return;
    }
    const below = window.innerHeight - rect.bottom - margin,
      above = rect.top - margin,
      down = below >= Math.min(320, above),
      height = Math.max(120, Math.min(420, down ? below : above) - gap),
      width = Math.min(
        Math.max(rect.width, 320),
        window.innerWidth - margin * 2,
      );
    popup.style.width = width + "px";
    popup.style.maxHeight = height + "px";
    popup.style.left =
      Math.max(
        margin,
        Math.min(rect.left, window.innerWidth - width - margin),
      ) + "px";
    popup.style.top =
      (down
        ? rect.bottom + gap
        : Math.max(margin, rect.top - popup.offsetHeight - gap)) + "px";
  }
  function open() {
    if (opened() || select.disabled) return;
    search.value = "";
    collapsed.clear();
    draw();
    popup.showPopover();
    select.setAttribute("aria-expanded", "true");
    position();
    search.focus();
    tree
      .querySelector('[aria-selected="true"]')
      ?.scrollIntoView({ block: "nearest" });
    watching?.abort();
    watching = new AbortController();
    window.addEventListener("resize", position, { signal: watching.signal });
    document.addEventListener(
      "scroll",
      (event) => {
        if (!popup.contains(event.target)) position();
      },
      { capture: true, signal: watching.signal },
    );
  }
  let pointerOpened = false;
  select.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    pointerOpened = opened();
    event.preventDefault();
    select.focus();
  });
  select.addEventListener("click", (event) => {
    event.preventDefault();
    if (pointerOpened) close();
    else open();
    pointerOpened = false;
  });
  select.addEventListener("keydown", (event) => {
    if (["ArrowDown", "ArrowUp", "Enter", " "].includes(event.key)) {
      event.preventDefault();
      open();
    }
  });
  popup.addEventListener("toggle", () => {
    select.setAttribute("aria-expanded", String(opened()));
    if (!opened()) watching?.abort();
  });
  search.addEventListener("input", (event) => {
    // Search is local UI state, never a change to the owning configuration field.
    event.stopPropagation();
    collapsed.clear();
    draw();
    position();
  });
  search.addEventListener("change", (event) => event.stopPropagation());
  popup.addEventListener("keydown", (event) => {
    const items = [...tree.querySelectorAll("[role=treeitem]")].filter(
        (item) => item.getClientRects().length,
      ),
      index = items.indexOf(document.activeElement),
      active = document.activeElement;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      close();
    } else if (event.key === "Tab") {
      close();
    } else if (["ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault();
      items[
        (index +
          (event.key === "ArrowDown" ? 1 : index < 0 ? 0 : -1) +
          items.length) %
          items.length
      ]?.focus();
    } else if (["Home", "End"].includes(event.key) && active !== search) {
      event.preventDefault();
      items[event.key === "Home" ? 0 : items.length - 1]?.focus();
    } else if (event.key === "ArrowLeft" && active !== search) {
      event.preventDefault();
      const provider = active?.dataset.provider,
        heading = [...tree.querySelectorAll(".model-reference-provider")].find(
          (item) => item.dataset.provider === provider,
        );
      if (active === heading && active.getAttribute("aria-expanded") === "true")
        heading.click();
      else heading?.focus();
    } else if (
      event.key === "ArrowRight" &&
      active?.classList.contains("model-reference-provider")
    ) {
      event.preventDefault();
      if (active.getAttribute("aria-expanded") === "false") active.click();
      else active.nextElementSibling.querySelector("[role=treeitem]")?.focus();
    } else if (event.key === "Enter" && active === search) {
      event.preventDefault();
      const options = [
        ...tree.querySelectorAll(".model-reference-option"),
      ].filter((item) => item.getClientRects().length);
      const selected = !search.value.trim()
        ? options.find((item) => item.dataset.value === select.value)
        : null;
      (selected || options[0])?.click();
    } else if (
      active !== search &&
      event.key.length === 1 &&
      !event.ctrlKey &&
      !event.metaKey &&
      !event.altKey &&
      event.key !== " "
    ) {
      event.preventDefault();
      search.focus();
      search.value += event.key;
      search.dispatchEvent(new Event("input", { bubbles: true }));
    }
  });
  return root;
}
