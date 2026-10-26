"use strict";

// Task-owned schema and shared draft/transaction editor. No separate model registry.
function taskRecommendations() {
  return valueAt(["tasks", "model_policy"], {}).recommendations || {};
}
function taskChild(parent, path, schema, value) {
  return {
    ...parent,
    path,
    schema,
    value,
    interface: {},
    explicit: value !== undefined,
  };
}
function taskEffortSchema(rule, model) {
  const entry = valueAt(["model_api", "entries"], {})[model];
  return {
    ...rule,
    enum: ["auto", ...(entry?.reasoning_efforts || [])],
    "x-labels": { auto: "自动 · 发起任务时选择" },
  };
}
function renderTaskDefaults(field) {
  const copy = structuredClone(field),
    main = valueAt(["main_model"], "unconfigured"),
    current = currentValue(field) || {},
    model = current.model && current.model !== "auto" ? current.model : main;
  copy.schema.properties.model.enum = [
    "auto",
    ...Object.keys(valueAt(["model_api", "entries"], {})),
  ];
  delete copy.schema.properties.model["x-reference"];
  copy.schema.properties.model["x-labels"] = { auto: "跟随主对话模型" };
  copy.schema.properties.reasoning_effort = taskEffortSchema(
    copy.schema.properties.reasoning_effort,
    model,
  );
  copy.schema.properties.reasoning_effort["x-labels"].auto =
    "跟随主对话 · 自动时由发起模型选择";
  const root = node("div", undefined, "task-model-defaults");
  root.append(
    node(
      "p",
      `主对话：${main} · ${valueAt(["main_reasoning_effort"], "auto")}。未配置任务模型时使用这一绑定；已保存任务的具体选型保持不变。`,
      "inline-note",
    ),
    drawField(copy),
  );
  return root;
}
function renderTaskPolicy(field) {
  const root = node("section", undefined, "task-model-policy"),
    path = field.path,
    policy = currentValue(field) || {},
    props = field.schema.properties,
    header = node("div", undefined, "recommendation-toolbar");
  header.append(
    node("h3", "模型推荐项"),
    button("添加推荐", () => openTaskRecommendation()),
  );
  root.append(
    header,
    node(
      "p",
      "可不添加。发起任务时，主模型参考推荐理由与适用场景选择；推荐项没有固定档位或优先顺序。",
      "hint",
    ),
  );
  const rows = node("div", undefined, "recommendation-list");
  for (const [id, item] of Object.entries(taskRecommendations())) {
    const row = node("section", undefined, "recommendation-row"),
      heading = node("div", undefined, "recommendation-heading"),
      copy = node("div", undefined, "recommendation-copy"),
      actions = node("div", undefined, "entity-actions");
    row.dataset.recommendation = id;
    heading.append(
      node("h4", item.name || "未命名推荐"),
      node(
        "span",
        `${item.model || "未选择模型"} · ${item.reasoning_effort || "auto"}`,
        "badge",
      ),
    );
    copy.append(
      heading,
      node("p", item.reason || "尚未填写推荐理由"),
      node("p", item.description || "尚未填写适用场景", "hint"),
    );
    actions.append(
      button("编辑", () => openTaskRecommendation(id)),
      button(
        "移除",
        () => {
          if (
            !confirm(
              `移除推荐“${item.name || id}”？已保存任务的实际选型不会改变。`,
            )
          )
            return;
          changed([...path, "recommendations", id], "default");
          render();
        },
        "text-button",
      ),
    );
    row.append(copy, actions);
    rows.append(row);
  }
  if (!rows.childElementCount)
    rows.append(node("p", "暂无推荐，后台任务默认跟随主对话。", "empty-note"));
  root.append(rows);
  const advanced = advancedGroup("task-model-policy");
  for (const name of ["review_mode", "max_upgrades"])
    advanced.append(
      drawField(taskChild(field, [...path, name], props[name], policy[name])),
    );
  root.append(advanced);
  return root;
}
function openTaskRecommendation(id) {
  const parent = fieldAt(["tasks", "model_policy"]);
  if (!parent) return;
  const checkpoint = draftCheckpoint(),
    schema = parent.schema.properties.recommendations.additionalProperties;
  if (!id) {
    id = "rec_" + configurationRequestId().replaceAll("-", "");
    const value = seedValue(schema);
    changed([...parent.path, "recommendations", id], "set", value);
  }
  const value = taskRecommendations()[id];
  activeEditor = {
    kind: "recommendations",
    name: id,
    checkpoint,
    field: taskChild(
      parent,
      [...parent.path, "recommendations", id],
      schema,
      value,
    ),
  };
  renderEntityEditor();
  showDialog("entry-dialog");
  $("entry-body").querySelector("input,select")?.focus();
}
function renderTaskRecommendation(field) {
  const root = $("entry-body"),
    props = structuredClone(field.schema.properties),
    value = currentValue(field) || {},
    model = value.model;
  props.reasoning_effort = taskEffortSchema(props.reasoning_effort, model);
  root.replaceChildren(
    node("p", "推荐供发起模型参考；不会在调用失败时自动切换。", "inline-note"),
  );
  for (const name of [
    "name",
    "model",
    "reasoning_effort",
    "reason",
    "description",
  ])
    root.append(
      drawField(
        taskChild(field, [...field.path, name], props[name], value[name]),
      ),
    );
  root.addEventListener(
    "input",
    (event) => {
      const row = event.target.closest("[data-field-path]");
      if (row?.dataset.fieldPath !== key([...field.path, "model"])) return;
      const supported =
        valueAt(["model_api", "entries"], {})[event.target.value]
          ?.reasoning_efforts || [];
      const effort = (currentValue(field) || {}).reasoning_effort;
      if (effort && effort !== "auto" && !supported.includes(effort)) {
        drafts.delete(key([...field.path, "reasoning_effort"]));
        changed([...field.path, "reasoning_effort"], "set", "auto");
      }
    },
    { capture: true, signal: editorInputController.signal },
  );
  activeEditor.validate = () => {
    const item = currentValue(field) || {};
    for (const name of field.schema.required || []) {
      if (typeof item[name] !== "string" || !item[name].trim()) {
        showNotice(`请填写${props[name].title}。`);
        document.getElementById(key([...field.path, name]))?.focus();
        return false;
      }
    }
    if (
      Object.entries(taskRecommendations()).some(
        ([id, other]) =>
          id !== activeEditor.name && other.name?.trim() === item.name.trim(),
      )
    ) {
      showNotice("推荐名称已存在，请使用不同名称。");
      return false;
    }
    const levels =
      valueAt(["model_api", "entries"], {})[item.model]?.reasoning_efforts ||
      [];
    if (
      item.reasoning_effort &&
      item.reasoning_effort !== "auto" &&
      !levels.includes(item.reasoning_effort)
    ) {
      showNotice("思考深度不受此模型支持，请重新选择。");
      return false;
    }
    return true;
  };
}
