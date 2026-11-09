"use strict";
const $ = (id) => document.getElementById(id);
const groups = {
  access: "基础与访问",
  models: "模型与账号",
  execution: "执行与用量",
  conversation: "对话与任务",
  notifications: "通知与事件",
  resources: "记忆与文件",
  connections: "外部连接",
  permissions: "权限管理",
  plugins: "插件管理",
  extensions: "扩展插件",
};
const descriptions = {
  access: "管理实例的基础设置与访问入口。",
  models: "配置服务供应商、模型别名与用途绑定。",
  execution: "调整执行资源、调用预算与用量保护。",
  conversation: "管理对话体验与后台任务策略。",
  notifications: "设置结果通知与事件处理。",
  resources: "管理记忆、工作文件与设备资源。",
  connections: "管理设备及其连接。",
  permissions: "管理第三方 MCP 连接与工具授权。",
  plugins: "选择当前实例启用的插件，保存后重启生效。",
  extensions: "按插件管理配置，字段与默认值由插件声明。",
};
const navPaths = {
  access:
    "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M12 3v2 M12 19v2 M3 12h2 M19 12h2 M5.6 5.6l1.4 1.4 M17 17l1.4 1.4 M5.6 18.4l1.4-1.4 M17 7l1.4-1.4",
  models: "M12 3l9 5-9 5-9-5 9-5 M3 12l9 5 9-5 M3 16l9 5 9-5",
  execution: "M13 2L4 14h7l-1 8 10-12h-7l1-8",
  conversation: "M4 4h16v12H9l-5 4V4",
  notifications: "M6 8a6 6 0 0 1 12 0v6l2 3H4l2-3V8 M10 21h4",
  resources: "M3 6h7l2 2h9v12H3V6",
  connections:
    "M9 15l6-6 M7 17l-2 2a4 4 0 0 1-5-5l5-5 M17 7l2-2a4 4 0 0 1 5 5l-5 5",
  permissions: "M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3 M8 12l3 3 5-6",
  plugins: "M4 4h6v6H4V4 M14 4h6v6h-6V4 M4 14h6v6H4v-6 M14 14h6v6h-6v-6",
  extensions: "M8 3h8v5h5v8h-5v5H8v-5H3V8h5V3",
};
let snapshot,
  inventory,
  resourceInventory = [],
  selected = "overview",
  saving = false;
let configurationBusy = false,
  needsRetry = false,
  pendingRestart = null;
let showInactive = false;
let passportDefaults = false, passportDevice = null;
const openEntries = new Set();
const resourceEdits = new Map();
const dirty = () =>
  edits.size + resourceEdits.size + (pendingRestart?.opaque_count || 0);
const edits = new Map();
function hasLocalChanges() {
  const retained = new Map(
    (pendingRestart?.patches || []).map((p) => [key(p.path), p]),
  );
  return (
    invalid.size ||
    resourceEdits.size ||
    edits.size !== retained.size ||
    [...edits].some(
      ([id, patch]) =>
        JSON.stringify(patch) !== JSON.stringify(retained.get(id)),
    )
  );
}
const invalid = new Map();
const drafts = new Map();
const hiddenValue = (value) =>
  value === "••••••••" ||
  (value &&
    typeof value === "object" &&
    ((Object.keys(value).length === 1 && Object.hasOwn(value, "configured")) ||
      Object.values(value).some(hiddenValue)));
const key = (path) => JSON.stringify(path);
const node = (tag, text, cls) => {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (cls) e.className = cls;
  return e;
};
function signedOut() {
  pluginPages.dispose();
  activeEditor?.dispose?.();
  activeEditor = null;
  pendingRestart = null;
  needsRetry = false;
  passportDefaults = false;
  passportDevice = null;
  for (const map of [edits, invalid, drafts, resourceEdits]) map.clear();
  $("login-screen").hidden = false;
  $("login").hidden = false;
  $("editor").hidden = true;
  $("password-open").hidden = true;
  $("password-change").hidden = true;
  $("repair").hidden = true;
  document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
  document.querySelectorAll('input[type="password"]').forEach((input) => {
    input.value = "";
  });
  clearInterval(applicationTimer);
  applicationTimer = undefined;
  clearTimeout(accountTimer);
  accountTimer = undefined;
}
async function api(path, body, signal) {
  const response = await fetch(
    path,
    body === undefined
      ? { signal }
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal,
        },
  );
  if (response.status === 401) {
    signedOut();
    throw Object.assign(Error("请先登录"), { status: 401 });
  }
  if (!response.headers.get("Content-Type")?.includes("application/json"))
    throw Error(`请求失败（${response.status}）`);
  const data = await response.json();
  if (!response.ok)
    throw Object.assign(Error(data.error || "请求失败"), {
      issues: data.issues || [],
      code: data.code || data.issues?.[0]?.code,
    });
  return data;
}
let noticeTimer;
function showNotice(message, duration = 0) {
  clearTimeout(noticeTimer);
  if (duration)
    noticeTimer = setTimeout(() => {
      if ($("notice").textContent === message) $("notice").textContent = "";
    }, duration);
  $("notice").textContent = message;
  const dialog = document.querySelector("dialog[open]");
  if (dialog) {
    let note = dialog.querySelector(".dialog-notice");
    if (!note) {
      note = node("p", undefined, "dialog-notice");
      note.setAttribute("role", "alert");
      dialog.insertBefore(note, dialog.children[1]);
    }
    note.textContent = message;
  }
}
function showDialog(id) {
  const dialog = $(id);
  $("notice").textContent = "";
  dialog.querySelector(".dialog-notice")?.remove();
  if (!dialog.open) {
    dialog.showModal();
    if (id === "entry-dialog") $("entry-body").scrollTop = 0;
  }
}
for (const button of document.querySelectorAll("[data-close]"))
  button.addEventListener("click", () => $(button.dataset.close).close());
$("password-dialog").addEventListener("close", () => {
  $("password-change").reset();
  $("password-change").hidden = true;
});
async function action(fn) {
  try {
    await fn();
  } catch (error) {
    showNotice(error.message);
    pluginPages.errors(error.issues || []);
    for (const issue of error.issues || []) {
      const input = document.getElementById(key(issue.path));
      if (input) {
        input.setAttribute("aria-invalid", "true");
        const field = input.closest(".field"),
          local = field?.querySelector(".field-error");
        if (local) {
          local.textContent = issue.message;
          local.hidden = false;
        } else field?.append(node("p", issue.message, "field-error"));
        for (
          let parent = input.parentElement;
          parent;
          parent = parent.parentElement
        )
          if (parent.tagName === "DETAILS") parent.open = true;
        input.focus();
      }
    }
  }
}
function changed(path, op, value) {
  invalid.delete(key(path));
  if (op === "default" || op === "clear_secret") drafts.delete(key(path));
  const parent = [...edits.values()].find(
    (e) =>
      e.op === "set" &&
      e.value &&
      typeof e.value === "object" &&
      e.path.length < path.length &&
      e.path.every((p, i) => p === path[i]),
  );
  if (parent) {
    let target = parent.value;
    for (const part of path.slice(parent.path.length, -1))
      target = target[part] ??= {};
    if (op === "default" || op === "clear_secret") delete target[path.at(-1)];
    else target[path.at(-1)] = value;
  } else {
    for (const [id, e] of edits)
      if (path.length < e.path.length && path.every((p, i) => p === e.path[i]))
        edits.delete(id);
    edits.set(key(path), {
      path,
      op,
      ...(value === undefined ? {} : { value }),
    });
  }
  updateSaveBar();
}
function icon(path) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.6");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  const drawing = document.createElementNS(svg.namespaceURI, "path");
  drawing.setAttribute("d", path);
  svg.append(drawing);
  return svg;
}
function fieldGroup(field) {
  return field.page || field.group;
}
function interfaceVisible(field) {
  const when = field.interface?.visible_when;
  if (!when) return true;
  const source = snapshot.fields.find((f) => key(f.path) === key(when.path));
  return (
    source &&
    JSON.stringify(currentValue(source)) === JSON.stringify(when.equals)
  );
}
function drawActions(plugin, section) {
  for (const [id, operation] of Object.entries(
    plugin?.configuration?.interface?.actions || {},
  )) {
    const form = node("form", undefined, "owner-action"),
      inputs = new Map(),
      result = node("p", "", "hint");
    form.append(node("h3", operation.title || id));
    for (const [name, shape] of Object.entries(
      operation.input?.properties || {},
    )) {
      const label = node("label", shape.title || name),
        input = node(shape.enum ? "select" : "input");
      if (shape.enum)
        for (const value of shape.enum) {
          const option = node("option", String(value));
          option.value = value;
          input.append(option);
        }
      else
        input.type =
          shape.type === "boolean"
            ? "checkbox"
            : ["number", "integer"].includes(shape.type)
              ? "number"
              : shape.writeOnly
                ? "password"
                : "text";
      label.append(input);
      form.append(label);
      inputs.set(name, { input, shape });
    }
    const button = node("button", operation.title || "执行", "secondary");
    form.append(button, result);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      action(async () => {
        if (dirty()) throw Error("请先保存设置；此操作使用已生效的配置。");
        const confirmed =
          operation.read_only ||
          confirm(
            operation.confirmation ||
              "此操作立即执行，不随配置恢复撤销。是否继续？",
          );
        if (!confirmed) return;
        const args = {};
        for (const [name, { input, shape }] of inputs)
          args[name] =
            shape.type === "boolean"
              ? input.checked
              : ["number", "integer"].includes(shape.type)
                ? Number(input.value)
                : input.value;
        button.disabled = true;
        try {
          const reply = await api("/api/configuration-action", {
            owner: plugin.id,
            action: id,
            arguments: args,
            confirmed,
          });
          result.textContent = reply.message || JSON.stringify(reply);
        } finally {
          button.disabled = false;
        }
      });
    });
    section.append(form);
  }
}
function updateSaveBar() {
  pluginPages.notify();
  $("count").textContent = invalid.size
    ? `${invalid.size} 项格式需要修正`
    : dirty()
      ? `${dirty()} 项未保存`
      : "没有未保存的修改";
  $("save").disabled =
    saving ||
    configurationBusy ||
    (!dirty() && !needsRetry) ||
    Boolean(invalid.size);
  $("save").textContent =
    saving || configurationBusy ? "保存并生效中…" : "保存并生效";
  $("discard").disabled = saving || (!dirty() && !invalid.size);
  for (const id of ["refresh", "history", "password-open"])
    $(id).disabled = saving;
  for (const button of $("navigation").querySelectorAll("button"))
    button.disabled = saving;
  $("documents").inert = saving;
  for (const row of document.querySelectorAll("[data-field-path]")) {
    const pending = edits.has(row.dataset.fieldPath);
    row.classList.toggle("is-dirty", pending);
    const badge = row.querySelector(
      ":scope > .field-control .field-badge, :scope > .object-heading .field-badge",
    );
    if (badge) {
      badge.textContent = pending
        ? "待保存"
        : row.dataset.explicit === "true"
          ? "已配置"
          : "默认";
      badge.classList.toggle("modified", pending);
    }
  }
}
function currentValue(field) {
  const path = field.path,
    edit = edits.get(key(path));
  let value =
    edit?.op === "clear_secret"
      ? ""
      : edit?.op === "default"
      ? field.schema.default
      : edit && Object.hasOwn(edit, "value")
        ? edit.value
        : field.value;
  const ancestor = [...edits.values()]
    .filter(
      (e) =>
        e.op === "set" &&
        e.path.length < path.length &&
        e.path.every((p, i) => p === path[i]),
    )
    .sort((a, b) => b.path.length - a.path.length)[0];
  if (ancestor) {
    value = ancestor.value;
    for (const part of path.slice(ancestor.path.length)) value = value?.[part];
  }
  if (value === undefined) value = field.schema.default;
  value = structuredClone(value);
  if (
    value === undefined &&
    [...edits.values()].some(
      (operation) =>
        operation.path.length > path.length &&
        path.every((p, i) => operation.path[i] === p) &&
        Object.hasOwn(operation, "value"),
    )
  )
    value = {};
  if (value && typeof value === "object")
    for (const operation of edits.values()) {
      if (
        operation.path.length <= path.length ||
        !path.every((p, i) => operation.path[i] === p)
      )
        continue;
      let target = value;
      for (const part of operation.path.slice(path.length, -1))
        target = target[part] ??= {};
      if (operation.op === "default" || operation.op === "clear_secret")
        delete target[operation.path.at(-1)];
      else target[operation.path.at(-1)] = operation.value;
    }
  return value;
}
function displayValue(rule, value) {
  return typeof value === "number" && rule["x-display-scale"]
    ? Number((value * rule["x-display-scale"]).toPrecision(12)) +
        (rule["x-unit"] || "")
    : JSON.stringify(value);
}
function fieldSource(field) {
  const details = node("details", undefined, "field-source");
  details.append(
    node("summary", "说明"),
    node("code", field.path.join(".")),
    node(
      "p",
      field.files.length
        ? `文件：${field.files.join("、")}`
        : `保存至 ${snapshot.entry}`,
      "hint",
    ),
  );
  if (
    field.schema.default !== undefined &&
    !field.schema.writeOnly &&
    !field.schema["x-sensitive"]
  )
    details.append(
      node(
        "p",
        "默认值：" + displayValue(field.schema, field.schema.default),
        "hint",
      ),
    );
  return details;
}
function resetControl(field, secret = false) {
  const reset = node(
    "button",
    field.schema["x-removable"] ? "移除条目" : secret ? "清除凭据" : "恢复默认",
    "text-button",
  );
  reset.type = "button";
  reset.addEventListener("click", () => {
    if (secret && !confirm("清除此凭据？保存后将尝试生效。")) return;
    if (
      field.schema["x-removable"] &&
      !confirm("移除此配置条目？保存后将尝试生效。")
    )
      return;
    changed(field.path, secret ? "clear_secret" : "default");
    render();
  });
  return reset;
}
function seedValue(rule) {
  return structuredClone(
    rule.default ??
      (rule.type === "object" || rule.properties
        ? {}
        : rule.type === "array"
          ? []
          : rule.type === "boolean"
            ? false
            : rule.type === "number" || rule.type === "integer"
              ? 0
              : ""),
  );
}
function optionLabel(rule, value, path) {
  if (rule["x-labels"]?.[value]) return rule["x-labels"][value];
  if (!path.at(-1)?.includes("reasoning")) return String(value);
  const labels = {
    auto: "自动 · 遵循模型默认策略",
    none: "关闭思考",
    minimal: "最低",
    low: "低",
    medium: "中",
    high: "高",
    xhigh: "更高",
    max: "最高",
    ultra: "超高",
  };
  return labels[value] ? `${labels[value]} · ${value}` : String(value);
}
function drawField(field) {
  if (typeof field.schema !== "object")
    field = {
      ...field,
      schema:
        field.schema === false
          ? { readOnly: true, description: "插件规范不允许此字段" }
          : {},
    };
  const rule = field.schema,
    path = field.path,
    id = key(path),
    secret = rule.writeOnly || rule["x-sensitive"],
    value = currentValue(field);
  const row = node("section", undefined, "field");
  row.dataset.fieldPath = id;
  row.dataset.explicit = String(Boolean(field.explicit));
  const copy = node("div", undefined, "field-copy"),
    label = node("label", rule.title || path.at(-1)),
    badge = node(
      "span",
      field.explicit ? "已配置" : "默认",
      "badge field-badge",
    );
  label.htmlFor = id;
  label.id = id + "-label";
  copy.append(label);
  if (rule.description) {
    const description = node("p", rule.description, "hint");
    description.id = id + "-description";
    copy.append(description);
  }
  if (rule["x-readonly-reason"])
    copy.append(node("p", rule["x-readonly-reason"], "hint"));
  const control = node("div", undefined, "field-control"),
    bottom = node("div", undefined, "field-bottom");
  bottom.append(fieldSource(field));
  if (!rule.readOnly) bottom.append(resetControl(field, secret));
  else bottom.append(node("span", "仅本机维护", "hint"));
  if (
    JSON.stringify(path) ===
    JSON.stringify(["work_files", "result_retention_days"])
  ) {
    const mode = node("select"),
      days = node("select");
    mode.id = id;
    mode.setAttribute("aria-label", "结果保留方式");
    days.setAttribute("aria-label", "自动删除时间");
    for (const [v, label] of [
      ["0", "永久保留"],
      ["auto", "自动删除"],
    ]) {
      const option = node("option", label);
      option.value = v;
      mode.append(option);
    }
    for (const [v, label] of [
      [7, "7 天"],
      [30, "30 天"],
      [365, "1 年"],
    ]) {
      const option = node("option", label);
      option.value = String(v);
      days.append(option);
    }
    mode.value = Number(value || 0) === 0 ? "0" : "auto";
    days.value = String(value || 30);
    days.hidden = mode.value === "0";
    mode.onchange = () => {
      days.hidden = mode.value === "0";
      changed(path, "set", mode.value === "0" ? 0 : Number(days.value));
    };
    days.onchange = () => changed(path, "set", Number(days.value));
    control.append(mode, days);
    row.append(copy, control, bottom);
    return row;
  }
  const objectForm =
    rule.type === "object" &&
    (rule.properties || Object.keys(rule.additionalProperties || {}).length) &&
    !secret;
  if (objectForm) {
    row.classList.add("object-field");
    const heading = node("div", undefined, "object-heading"),
      titles = node("div"),
      actions = node("div", undefined, "heading-actions");
    titles.append(node("strong", rule.title || path.at(-1)));
    if (rule.description) titles.append(node("p", rule.description, "hint"));
    actions.append(badge);
    if (!rule.readOnly && rule["x-removable"])
      actions.append(resetControl(field));
    heading.append(titles, actions);
    row.append(heading);
    const current = value || {},
      properties = displayProperties(rule, current),
      named = Boolean(
        rule.additionalProperties &&
        Object.keys(rule.additionalProperties).length,
      ),
      children = node(
        "div",
        undefined,
        named ? "named-collection" : "object-children",
      );
    for (const name of Object.keys(current))
      if (!Object.hasOwn(properties, name))
        properties[name] = rule.additionalProperties || {};
    const fixed = new Set(Object.keys(displayProperties(rule, current)));
    for (const [name, child] of Object.entries(properties)) {
      const definition = {
        ...field,
        path: [...path, name],
        schema: {
          ...child,
          readOnly: rule.readOnly || child.readOnly,
          "x-removable": !fixed.has(name),
        },
        value: current[name],
        loaded_value: field.loaded_value?.[name],
        explicit: field.explicit && Object.hasOwn(current, name),
      };
      if (named && !fixed.has(name)) {
        const entry = node("details", undefined, "named-entry"),
          entryId = key([...path, name]),
          summary = node("summary");
        entry.open = openEntries.has(entryId);
        summary.append(
          node("strong", name),
          node(
            "span",
            Object.keys(current[name] || {}).length + " 项设置",
            "badge",
          ),
        );
        entry.append(summary);
        const populate = () => {
          if (!entry.querySelector(":scope > .field"))
            entry.append(drawField(definition));
        };
        if (entry.open) populate();
        entry.addEventListener("toggle", () => {
          if (entry.open) {
            openEntries.add(entryId);
            populate();
            updateSaveBar();
          } else openEntries.delete(entryId);
        });
        children.append(entry);
      } else if (child["x-advanced"]) {
        let advanced = children.querySelector(":scope > .advanced-fields");
        if (!advanced) {
          advanced = advancedGroup("advanced:" + key(path));
          children.append(advanced);
        }
        advanced.append(drawField(definition));
      } else children.append(drawField(definition));
    }
    const advancedFields = children.querySelector(":scope > .advanced-fields");
    if (advancedFields) children.append(advancedFields);
    if (!children.childElementCount)
      children.append(node("p", "尚未添加条目", "empty-state"));
    if (rule.additionalProperties && !rule.readOnly) {
      const toolbar = node("div", undefined, "collection-toolbar"),
        name = node("input"),
        add = node("button", "添加条目", "secondary");
      name.placeholder = rule["x-entry-placeholder"] || "条目名称，例如 primary";
      add.textContent = rule["x-entry-add"] || "添加条目";
      name.setAttribute(
        "aria-label",
        (rule.title || path.at(-1)) + " · 新条目名称",
      );
      add.type = "button";
      add.addEventListener("click", () => {
        const text = name.value.trim();
        if (
          !(rule.propertyNames?.pattern
            ? new RegExp(rule.propertyNames.pattern).test(text)
            : /^[A-Za-z_][A-Za-z0-9_-]{0,63}$/.test(text)) ||
          Object.hasOwn(current, text)
        ) {
          $("notice").textContent =
            "名称需以字母或下划线开头，且不能与现有条目重复。";
          name.focus();
          return;
        }
        const child = [...path, text];
        openEntries.add(key(child));
        const seed = seedValue(rule.additionalProperties);
        changed(child, rule.additionalProperties.type === "object" && !Object.keys(seed).length ? "create" : "set", seed);
        render();
        document
          .getElementById(
            key([
              ...child,
              Object.keys(rule.additionalProperties.properties || {})[0],
            ]),
          )
          ?.focus();
      });
      toolbar.append(name, add);
      children.append(toolbar);
    }
    row.append(children);
    return row;
  }
  let input, read, element;
  if (rule.readOnly || (!secret && hiddenValue(value))) {
    const output = node(
      "output",
      hiddenValue(value)
        ? "含敏感配置，请通过对应凭据入口维护"
        : value === undefined
          ? "未配置"
          : typeof value === "string"
            ? value
            : JSON.stringify(value),
      "readonly-value",
    );
    output.id = id;
    control.append(output, bottom);
    row.append(copy, control);
    return row;
  }
  if (secret) {
    input = node("input");
    input.type = "password";
    input.autocomplete = "new-password";
    input.placeholder = value?.configured ? "已配置，留空保持" : "输入新凭据";
    read = () => input.value;
  } else if (rule["x-reference"]) {
    input = node("select");
    for (const v of new Set([
      "",
      ...referenceValues(rule["x-reference"]),
      ...(value ? [value] : []),
    ])) {
      const option = node("option", String(v) || "未指定");
      option.value = String(v);
      input.append(option);
    }
    input.value = String(value ?? "");
    read = () => input.value;
    if (rule["x-reference"] === "model_api.entries") {
      if (rule.enum) {
        input.replaceChildren();
        for (const v of new Set([...rule.enum, ...(value ? [value] : [])])) {
          const option = node("option", optionLabel(rule, v, path));
          option.value = String(v);
          input.append(option);
        }
        input.value = String(value ?? rule.enum[0] ?? "");
      }
      element = modelReferencePicker(input, {
        auto: Boolean(rule.enum?.includes("auto") && rule["x-labels"]?.auto),
      });
    }
  } else if (rule.enum) {
    input = node("select");
    if (value === undefined) {
      const placeholder = node("option", "请选择");
      placeholder.value = "";
      placeholder.disabled = true;
      input.append(placeholder);
    }
    for (const v of rule.enum) {
      const option = node("option", optionLabel(rule, v, path));
      option.value = String(v);
      input.append(option);
    }
    input.value = String(value ?? "");
    read = () => rule.enum.find((v) => String(v) === input.value);
  } else if (rule.type === "boolean") {
    input = node("input");
    input.type = "checkbox";
    input.setAttribute("role", "switch");
    input.checked = Boolean(value);
    read = () => input.checked;
    element = node("div", undefined, "switch-label");
    const state = node("span", input.checked ? "已开启" : "已关闭");
    state.setAttribute("aria-hidden", "true");
    element.append(input, state);
    input.addEventListener("change", () => {
      state.textContent = input.checked ? "已开启" : "已关闭";
    });
  } else if (rule.type === "number" || rule.type === "integer") {
    input = node("input");
    input.type = "number";
    input.step = rule.type === "integer" ? "1" : "any";
    const scale = rule["x-display-scale"] || 1;
    if (rule.minimum !== undefined) input.min = rule.minimum * scale;
    if (rule.maximum !== undefined) input.max = rule.maximum * scale;
    input.value =
      value === undefined || value === null
        ? ""
        : Number((value * scale).toPrecision(12));
    const unit =
      rule["x-unit"] ||
      (/(?:_seconds|_s)$/.test(path.at(-1))
        ? "秒"
        : /_ms$/.test(path.at(-1))
          ? "毫秒"
          : /_bytes$/.test(path.at(-1))
            ? "字节"
            : /_days$/.test(path.at(-1))
              ? "天"
              : "");
    if (unit) {
      element = node("div", undefined, "number-with-unit");
      element.append(input, node("span", unit));
    }
    read = () => {
      if (!input.value) throw Error("请输入数值。");
      if (!input.checkValidity())
        throw Error(
          input.validity.rangeUnderflow
            ? `数值不能小于 ${input.min}。`
            : input.validity.rangeOverflow
              ? `数值不能大于 ${input.max}。`
              : "请填写符合范围的数值。",
        );
      const number = Number(input.value) / scale;
      if (
        (rule.exclusiveMinimum !== undefined &&
          number <= rule.exclusiveMinimum) ||
        (rule.exclusiveMaximum !== undefined && number >= rule.exclusiveMaximum)
      )
        throw Error(
          "数值需" +
            [
              rule.exclusiveMinimum !== undefined
                ? `大于 ${rule.exclusiveMinimum * scale}`
                : "",
              rule.exclusiveMaximum !== undefined
                ? `小于 ${rule.exclusiveMaximum * scale}`
                : "",
            ]
              .filter(Boolean)
              .join("、") +
            "。",
        );
      return number;
    };
  } else if (rule.type === "array" && Array.isArray(rule.items?.enum)) {
    input = node("div", undefined, "choice-list");
    for (const choice of rule.items.enum) {
      const line = node("label"),
        check = node("input");
      check.type = "checkbox";
      check.value = String(choice);
      check.checked = (value || []).includes(choice);
      line.append(check, node("span", optionLabel(rule, choice, path)));
      input.append(line);
    }
    read = () =>
      [...input.querySelectorAll("input:checked")].map((i) =>
        rule.items.enum.find((v) => String(v) === i.value),
      );
  } else if (
    rule.type === "array" &&
    (rule.items?.type === "string" ||
      (Array.isArray(value) &&
        value.length &&
        value.every((v) => typeof v === "string")))
  ) {
    input = node("div", undefined, "list-editor");
    let current = Array.isArray(value) ? value : [];
    const draw = () => {
      input.replaceChildren();
      current.forEach((v, index) => {
        const line = node("div", undefined, "list-item"),
          item = node(rule.items?.["x-reference"] ? "select" : "input"),
          remove = node("button", "×", "icon-button");
        if (item.tagName === "SELECT")
          for (const value of new Set([
            "",
            ...referenceValues(rule.items["x-reference"]),
            v,
          ])) {
            const option = node("option", value || "请选择");
            option.value = value;
            item.append(option);
          }
        item.value = v;
        item.setAttribute(
          "aria-label",
          (rule.title || path.at(-1)) + " · 第 " + (index + 1) + " 项",
        );
        item.addEventListener("input", () => {
          current[index] = item.value;
        });
        remove.type = "button";
        remove.setAttribute("aria-label", "移除第 " + (index + 1) + " 项");
        remove.addEventListener("click", () => {
          current.splice(index, 1);
          changed(path, "set", [...current]);
          draw();
        });
        const itemControl =
          rule.items?.["x-reference"] === "model_api.entries"
            ? modelReferencePicker(item)
            : item;
        line.append(itemControl, remove);
        input.append(line);
      });
      const add = node("button", "添加一项", "secondary");
      add.type = "button";
      add.addEventListener("click", () => {
        current.push("");
        changed(path, "set", [...current]);
        draw();
        input.querySelector(".list-item:last-of-type input")?.focus();
      });
      input.append(add);
    };
    draw();
    read = () => [...current];
  } else if (!rule.type || rule.type === "object" || rule.type === "array") {
    input = node("textarea");
    input.spellcheck = false;
    input.value = JSON.stringify(
      value ?? (rule.type === "array" ? [] : {}),
      null,
      2,
    );
    read = () => JSON.parse(input.value);
    element = node("details", undefined, "structured-editor");
    element.open = invalid.has(id);
    element.append(
      node(
        "summary",
        `结构化编辑 · ${rule.type === "array" ? "列表" : "JSON"}`,
      ),
      input,
    );
  } else {
    input = node("input");
    input.type = "text";
    input.value = value ?? "";
    if (rule.maxLength !== undefined) input.maxLength = rule.maxLength;
    read = () => input.value;
  }
  if (drafts.has(id) && input.tagName !== "DIV" && input.type !== "checkbox")
    input.value = drafts.get(id);
  input.id = id;
  if (element?.classList.contains("model-reference")) {
    element
      .querySelector("[role=searchbox]")
      .setAttribute(
        "aria-label",
        (rule.title || path.at(-1)) + " · 搜索模型或 Provider",
      );
  }
  if (input.tagName === "DIV") {
    input.setAttribute("role", "group");
    input.setAttribute("aria-labelledby", label.id);
  }
  const error = node(
    "p",
    invalid.has(id) ? "请检查输入格式或允许的范围。" : "",
    "field-error",
  );
  error.id = id + "-error";
  error.hidden = !invalid.has(id);
  error.setAttribute("role", "alert");
  input.setAttribute(
    "aria-describedby",
    [rule.description ? id + "-description" : "", error.id]
      .filter(Boolean)
      .join(" "),
  );
  input.setAttribute("aria-invalid", String(invalid.has(id)));
  input.addEventListener("input", () => {
    if (input.tagName !== "DIV") drafts.set(id, input.value);
    try {
      changed(path, secret ? "replace_secret" : "set", read());
      input.setAttribute("aria-invalid", "false");
      error.hidden = true;
      error.textContent = "";
      if (
        input.tagName === "SELECT" ||
        input.type === "checkbox" ||
        path[0] === "plugins" ||
        (rule.type === "array" && input.classList.contains("choice-list"))
      ) {
        render();
        document.getElementById(id)?.focus();
      }
    } catch (reason) {
      invalid.set(id, path.join("."));
      input.setAttribute("aria-invalid", "true");
      updateSaveBar();
      error.hidden = false;
      error.textContent = ["number", "integer"].includes(rule.type)
        ? reason.message
        : "请检查输入格式或允许的范围。";
    }
  });
  control.append(element || input);
  const limits = [
    rule.minimum !== undefined
      ? "最小 " + displayValue(rule, rule.minimum)
      : null,
    rule.exclusiveMinimum !== undefined
      ? "大于 " + displayValue(rule, rule.exclusiveMinimum)
      : null,
    rule.maximum !== undefined
      ? "最大 " + displayValue(rule, rule.maximum)
      : null,
    rule.exclusiveMaximum !== undefined
      ? "小于 " + displayValue(rule, rule.exclusiveMaximum)
      : null,
  ].filter(Boolean);
  input.title = [
    ...(rule["x-reference"] === "model_api.entries"
      ? [input.selectedOptions?.[0]?.label]
      : []),
    ...limits,
  ]
    .filter(Boolean)
    .join(" · ");
  const information = fieldSource(field);
  if (limits.length) information.append(node("p", limits.join(" · "), "hint"));
  if (
    Object.hasOwn(field, "loaded_value") &&
    JSON.stringify(field.loaded_value) !== JSON.stringify(field.value) &&
    !secret &&
    field.loaded_value !== undefined
  )
    information.append(
      node(
        "p",
        "当前已加载：" + displayValue(rule, field.loaded_value),
        "hint",
      ),
    );
  if (!rule.readOnly) information.append(resetControl(field, secret));
  copy.append(information);
  bottom.replaceChildren(badge);
  control.append(error, bottom);
  row.append(copy, control);
  return row;
}
function cardHeading(title, description) {
  const heading = node("div", undefined, "card-heading");
  heading.append(node("h2", title));
  if (description) heading.append(node("p", description));
  return heading;
}
function advancedGroup(identity) {
  const group = node("details", undefined, "advanced-fields");
  group.open = openEntries.has(identity);
  group.append(node("summary", "高级设置"));
  group.addEventListener("toggle", () => {
    if (!group.isConnected) return;
    if (group.open) openEntries.add(identity);
    else openEntries.delete(identity);
  });
  return group;
}
function render() {
  workspaceRender();
  renderEntityEditor();
  updateSaveBar();
}
async function load({ discardLocal = false } = {}) {
  if (
    !discardLocal &&
    hasLocalChanges() &&
    !confirm("重新加载会放弃尚未保存的编辑，是否继续？")
  )
    return;
  const status = await api("/api/status");
  $("login-screen").hidden = true;
  $("login").hidden = true;
  $("editor").hidden = false;
  $("password-open").hidden = false;
  const [config, catalogInfo] = await Promise.all([
    api("/api/configuration"),
    api("/api/catalog"),
  ]);
  resourceInventory = config.resources;
  resourceEdits.clear();
  snapshot = config;
  inventory = catalogInfo;
  pluginPages.errors([]);
  for (const name of Object.keys(groups)) delete groups[name];
  for (const page of inventory.pages || []) {
    groups[page.id] = page.title;
    descriptions[page.id] = page.description;
  }
  edits.clear();
  pendingRestart = config.pending_restart;
  for (const patch of pendingRestart?.patches || [])
    edits.set(key(patch.path), patch);
  invalid.clear();
  drafts.clear();
  $("diagnostics").replaceChildren(
    ...(inventory.issues || []).map((item) =>
      node("p", `${item.id}：配置描述不可用（${item.error}）`, "hint"),
    ),
  );
  $("login-screen").hidden = true;
  $("login").hidden = true;
  $("editor").hidden = false;
  $("password-open").hidden = false;
  $("state").textContent =
    status.loaded_version === config.version
      ? "已应用 · 配置与运行版本一致"
      : "已保存 · 等待确认生效";
  $("state-version").textContent = "配置修订 " + config.version.slice(0, 10);
  $("health").textContent =
    {
      running: "业务运行中",
      stopped: "业务已停止",
      failed: "启动失败",
      degraded: "部分功能不可用",
      starting: "业务启动中",
    }[status.state] || status.state;
  $("health").dataset.state = status.state;
  renderPluginHealth(status);
  render();
  await applicationStatus();
}
$("login").addEventListener("submit", (e) => {
  e.preventDefault();
  action(async () => {
    await api("/api/login", { token: $("token").value });
    $("token").value = "";
    $("password-open").hidden = false;
    await load();
    $("notice").textContent = "";
  });
});
$("password-open").addEventListener("click", () =>
  action(async () => {
    const policy = await api("/api/admin-password");
    for (const id of ["new-password", "confirm-password"]) {
      $(id).minLength = policy.min_length;
      $(id).maxLength = policy.max_length;
    }
    $("password-policy").textContent =
      `新密码须为 ${policy.min_length}–${policy.max_length} 个字符，首尾不能有空白。`;
    $("password-change").reset();
    $("password-change").hidden = false;
    showDialog("password-dialog");
    $("current-password").focus();
  }),
);
$("password-cancel").addEventListener("click", () =>
  $("password-dialog").close(),
);
$("password-change").addEventListener("submit", (e) => {
  e.preventDefault();
  action(async () => {
    if ($("new-password").value !== $("confirm-password").value)
      throw Error("两次输入的新密码不一致。");
    if (
      (dirty() || invalid.size) &&
      !confirm("修改密码后需要重新登录，未保存的编辑会丢失。是否继续？")
    )
      return;
    $("password-save").disabled = true;
    try {
      await api("/api/admin-password", {
        current_password: $("current-password").value,
        new_password: $("new-password").value,
        confirmation: $("confirm-password").value,
      });
      edits.clear();
      resourceEdits.clear();
      invalid.clear();
      drafts.clear();
      signedOut();
      $("notice").textContent =
        "管理员密码已修改，所有设备需使用新密码重新登录。无需重启业务服务。";
      $("token").focus();
    } finally {
      $("password-change").reset();
      $("password-save").disabled = false;
    }
  });
});
$("refresh").addEventListener("click", () => action(load));
$("discard").addEventListener("click", () =>
  action(async () => {
    if (pendingRestart)
      await api("/api/configuration/discard", {
        pending_id: pendingRestart.id,
      });
    await load({ discardLocal: true });
    showNotice("已放弃修改。", 4000);
  }),
);
function askRestart() {
  if (!pendingRestart) return;
  $("restart-summary").textContent =
    `还有 ${pendingRestart.count} 项修改需要重启；无需重启的设置已处理完成。`;
  showDialog("restart-dialog");
}
async function waitForApplication() {
  for (;;) {
    const status = await api("/api/status"),
      application = status.application || {};
    if (
      ![
        "preparing",
        "waiting",
        "applying",
        "recovering",
        "cancelling",
        "restarting",
      ].includes(application.phase)
    ) {
      if (application.phase === "failed")
        throw Object.assign(
          Error(
            (application.issues || []).map((i) => i.message).join("；") ||
              "设置未能生效，原运行配置已保留。请重试保存；如仍失败，请检查日志。",
          ),
          { issues: application.issues || [] },
        );
      if (application.phase === "pending_restart")
        throw Error("这些修改需要一起重启才能应用，请重新保存后确认重启。");
      return application;
    }
    await new Promise((resolve) => setTimeout(resolve, 750));
  }
}
async function saveChanges() {
  if (invalid.size)
    throw Error("请先修正字段格式：" + [...invalid.values()].join("、"));
  if (!dirty() && !needsRetry) return;
  saving = true;
  $("notice").textContent = "";
  updateSaveBar();
  try {
    const saved = await api("/api/configuration", {
      version: snapshot.version,
      patches: [...edits.values()],
      resources: [...resourceEdits.values()],
      pending_id: pendingRestart?.id || null,
    });
    // The server has committed a new file revision even if a module cannot switch.
    // Keep the editable candidate for an explicit retry; never resubmit an old revision.
    snapshot.version = saved.version;
    pendingRestart = saved.pending_restart;
    const applied = saved.application ? await waitForApplication() : {};
    await load({ discardLocal: true });
    if (pendingRestart) askRestart();
    else
      showNotice(
        applied.phase === "pending_start"
          ? "保存成功，下次启动时生效。"
          : applied.phase === "partially_applied"
            ? "保存成功；部分功能仍需处理，请查看实例日志。"
            : "保存成功，设置已生效。",
        6000,
      );
  } finally {
    saving = false;
    await applicationStatus().catch(() => {});
    updateSaveBar();
  }
}
$("restart-later").addEventListener("click", () => {
  $("restart-dialog").close();
  showNotice("无需重启的修改已生效，其余修改保留在变更记录中。", 6000);
});
$("restart-confirm").addEventListener("click", () =>
  action(async () => {
    if (!pendingRestart) return;
    saving = true;
    $("restart-confirm").disabled = true;
    updateSaveBar();
    try {
      const saved = await api("/api/configuration/restart", {
        version: snapshot.version,
        pending_id: pendingRestart.id,
        confirmed: true,
      });
      snapshot.version = saved.version;
      $("restart-dialog").close();
      showNotice("正在重启并应用设置…");
      await waitForApplication();
      await load({ discardLocal: true });
      showNotice("保存成功，重启后设置已生效。", 6000);
    } finally {
      saving = false;
      $("restart-confirm").disabled = false;
      await applicationStatus().catch(() => {});
      updateSaveBar();
    }
  }),
);
$("save").addEventListener("click", () => action(saveChanges));
$("history").addEventListener("click", () =>
  action(async () => {
    const history = await api("/api/history");
    $("versions").replaceChildren();
    if (dirty()) {
      const current = node("article");
      current.append(
        node("h3", "当前修改"),
        node(
          "p",
          [
            ...new Set([...edits.values()].map((p) => p.path.join("."))),
            ...new Set(
              [...resourceEdits.values()].map((p) => "resource:" + p.id),
            ),
            ...(pendingRestart?.labels || []),
          ]
            .filter((item, index, all) => all.indexOf(item) === index)
            .join("、"),
        ),
      );
      $("versions").append(current);
    }
    if (!history.versions.length)
      $("versions").append(node("p", "尚无配置变更记录", "empty-state"));
    for (const item of history.versions) {
      const article = node("article", undefined, "history-item"),
        header = node("header"),
        restore = node("button", "恢复此版本", "secondary");
      header.append(
        node("strong", new Date(item.time * 1000).toLocaleString()),
        restore,
      );
      article.append(
        header,
        node("p", item.fields.join("、") || "配置资源变更"),
      );
      restore.addEventListener("click", () =>
        action(async () => {
          if (dirty()) throw Error("请先保存或放弃当前编辑");
          if (!confirm("恢复此前的配置？业务数据不会回滚，保存后将尝试生效。"))
            return;
          await api("/api/restore", { id: item.id, version: snapshot.version });
          $("history-dialog").close();
          await load();
        }),
      );
      $("versions").append(article);
    }
    showDialog("history-dialog");
  }),
);
window.addEventListener("beforeunload", (e) => {
  if (hasLocalChanges()) {
    e.preventDefault();
    e.returnValue = "";
  }
});
$("entry-cancel").addEventListener("click", () => finishEntity(true));
$("entry-done").addEventListener("click", () => finishEntity());
$("entry-dialog").addEventListener("cancel", (event) => {
  event.preventDefault();
  finishEntity(true);
});
$("setup-dialog").addEventListener("close", () =>
  $("setup-form").replaceChildren(),
);
load().catch((error) => {
  if (error.status !== 401) showNotice(error.message);
});

function renderPluginHealth(status) {
  const panel = $("plugin-health"),
    issues = status.plugin_issues || [];
  const signature = JSON.stringify(issues);
  if (panel.dataset.signature === signature) return;
  panel.dataset.signature = signature;
  panel.hidden = !issues.length;
  panel.replaceChildren();
  if (!issues.length) return;
  panel.append(node("strong", `${issues.length} 项功能需要处理`));
  const list = node("ul");
  for (const issue of issues) {
    const row = node("li");
    row.append(
      node("strong", issue.id + "："),
      document.createTextNode(issue.error),
    );
    list.append(row);
  }
  panel.append(
    list,
    node(
      "p",
      "故障功能可能不可用；其余功能可继续使用。状态会自动刷新，恢复后提示消失。",
      "hint",
    ),
  );
}
let applicationTimer;
let statusPolling = false;

async function applicationStatus() {
  const status = await api("/api/status"),
    application = status.application || { phase: "idle" };
  if ($("editor").hidden) return;
  configurationBusy = [
    "preparing",
    "waiting",
    "applying",
    "recovering",
    "cancelling",
    "restarting",
  ].includes(application.phase);
  updateSaveBar();
  needsRetry = Boolean(
    !configurationBusy &&
    snapshot &&
    status.loaded_version &&
    status.loaded_version !== snapshot.version &&
    application.phase !== "pending_start",
  );
  $("state").textContent = configurationBusy
    ? "正在保存并生效…"
    : needsRetry
      ? "有保存的修改尚未生效，请重试保存。"
      : "";
  document.querySelector(".configuration-state").hidden =
    !configurationBusy && !needsRetry;
  updateSaveBar();
  $("health").textContent =
    {
      running: "业务运行中",
      stopped: "业务已停止",
      failed: "启动失败",
      degraded: "部分功能不可用",
      starting: "业务启动中",
    }[status.state] || status.state;
  $("health").dataset.state = status.state;
  renderPluginHealth(status);
  renderChatShortcut();
  if (!applicationTimer)
    applicationTimer = setInterval(async () => {
      if (statusPolling || $("editor").hidden) return;
      statusPolling = true;
      try {
        await applicationStatus();
      } catch {
        if (!$("editor").hidden) {
          $("health").textContent = "状态连接中断";
          $("health").dataset.state = "degraded";
          renderPluginHealth({
            plugin_issues: [
              {
                id: "连接",
                error: "暂时无法获取运行状态，正在重试；现有编辑不会丢失。",
              },
            ],
          });
        }
      } finally {
        statusPolling = false;
      }
    }, 5000);
}
function matches(value, condition) {
  if (!condition) return true;
  if (condition.required?.some((name) => !Object.hasOwn(value || {}, name)))
    return false;
  if (
    condition.const !== undefined &&
    JSON.stringify(condition.const) !== JSON.stringify(value)
  )
    return false;
  if (
    condition.enum &&
    !condition.enum.some(
      (item) => JSON.stringify(item) === JSON.stringify(value),
    )
  )
    return false;
  if (
    condition.properties &&
    Object.entries(condition.properties).some(
      ([name, rule]) =>
        Object.hasOwn(value || {}, name) && !matches(value[name], rule),
    )
  )
    return false;
  return (
    (!condition.allOf || condition.allOf.every((c) => matches(value, c))) &&
    (!condition.anyOf || condition.anyOf.some((c) => matches(value, c)))
  );
}
function referenceValues(path) {
  if (typeof path !== "string") return [];
  const parts = path.split(".");
  const field = snapshot.fields.find((field) => key(field.path) === key(parts));
  const current = field ? currentValue(field) : undefined;
  return current && typeof current === "object" ? Object.keys(current) : [];
}
function resourceGroup(resource) {
  return (
    {
      credentials: "models",
      mcp: "permissions",
      management: "access",
      web_access: "plugin:web",
    }[resource.id] ||
    (resource.kind === "skill"
      ? "resources"
      : resource.id.includes(":")
        ? "plugin:" + resource.id.split(":")[0]
        : "plugins")
  );
}
function drawResources(target = $("documents"), filter) {
  const resources = resourceInventory.filter(
    (r) =>
      r.kind !== "asset" &&
      r.id !== "mcp" &&
      (filter ? filter(r) : resourceGroup(r) === selected),
  );
  if (!resources.length) return;
  const section = node("article");
  section.append(
    cardHeading(
      "配置文件与凭据",
      "凭据不回显；留空保持当前值。保存后随配置一并应用。",
    ),
  );
  for (const resource of resources) {
    const row = node("details", undefined, "resource-entry"),
      summary = node("summary", resource.title),
      content = node("div", undefined, "resource-content");
    summary.append(
      node(
        "span",
        resource.configured ? "已配置" : "未配置",
        resource.configured ? "badge configured" : "badge",
      ),
    );
    row.append(summary);
    content.append(node("p", resource.file, "hint"));
    if (resource.read_only) {
      content.append(node("p", "此路径在实例之外，需要本机维护。", "hint"));
      row.append(content);
      section.append(row);
      continue;
    }
    if (resource.kind === "dotenv") {
      const names = new Set((resource.keys || []).map((k) => k.name));
      for (const entry of resource.keys || []) {
        const line = node("div", undefined, "credential-row"),
          label = node("label", entry.name),
          input = node("input"),
          id = resource.id + ":" + entry.name;
        label.htmlFor = "resource-" + id;
        label.append(
          node(
            "span",
            entry.source === "environment"
              ? entry.environment_configured
                ? "由环境变量覆盖"
                : "环境变量为空"
              : entry.configured
                ? "文件中已配置"
                : "未配置",
            "hint",
          ),
        );
        input.id = "resource-" + id;
        input.type = "password";
        input.autocomplete = "new-password";
        input.placeholder = "输入新值替换，留空保持";
        input.disabled = entry.source === "environment";
        input.value = resourceEdits.get(id)?.value ?? "";
        input.addEventListener("input", () => {
          if (input.value)
            resourceEdits.set(id, {
              id: resource.id,
              key: entry.name,
              op: "replace_secret",
              value: input.value,
            });
          else resourceEdits.delete(id);
          updateSaveBar();
        });
        const clear = node("button", "清除", "subtle");
        clear.disabled = input.disabled;
        clear.addEventListener("click", () => {
          if (confirm("清除此文件凭据？保存后将尝试生效。")) {
            resourceEdits.set(id, {
              id: resource.id,
              key: entry.name,
              op: "clear_secret",
            });
            input.value = "";
            updateSaveBar();
          }
        });
        line.append(label, input, clear);
        content.append(line);
      }
      const toolbar = node("div", undefined, "collection-toolbar"),
        name = node("input"),
        add = node("button", "添加私有凭据", "secondary");
      name.placeholder = "例如 DEEPSEEK_API_KEY";
      name.setAttribute("aria-label", "新凭据环境变量名称");
      add.addEventListener("click", () => {
        const text = name.value.trim();
        if (!/^[A-Z_][A-Z0-9_]*$/.test(text) || names.has(text)) {
          showNotice(
            "请输入不重复的环境变量名称，仅支持大写字母、数字和下划线。",
          );
          return;
        }
        (resource.keys ??= []).push({
          name: text,
          configured: false,
          source: "file",
        });
        render();
      });
      toolbar.append(name, add);
      content.append(toolbar);
    } else {
      const input = node(resource.sensitive ? "input" : "textarea");
      input.id = "resource-" + resource.id;
      input.setAttribute("aria-label", resource.title);
      if (resource.sensitive) {
        input.type = "password";
        input.autocomplete = "new-password";
        input.placeholder = "输入新值替换，留空保持";
        input.value = resourceEdits.get(resource.id)?.value ?? "";
      } else {
        input.spellcheck = false;
        input.value =
          resourceEdits.get(resource.id)?.value ?? resource.value ?? "";
      }
      input.addEventListener("input", () => {
        if (resource.sensitive && !input.value)
          resourceEdits.delete(resource.id);
        else
          resourceEdits.set(resource.id, {
            id: resource.id,
            op: resource.sensitive ? "replace_secret" : "set",
            value: input.value,
          });
        updateSaveBar();
      });
      content.append(input);
      if (resource.id === "web_access")
        content.append(
          node(
            "p",
            "至少 6 个字符，无需大小写或数字组合；留空保持当前密码。",
            "hint",
          ),
        );
      if (resource.id === "management")
        content.append(
          node(
            "p",
            "管理地址变更可能中断当前连接；应用前请记录新地址。绑定失败会保留旧入口。",
            "hint",
          ),
        );
    }
    row.append(content);
    section.append(row);
  }
  target.append(section);
}
let accountTimer;
async function drawAccounts() {
  const data = await api("/api/accounts");
  if (selected !== "models" || modelTab !== "connections" || $("editor").hidden)
    return;
  let section = $("accounts-panel");
  if (!section) {
    section = node("article");
    section.id = "accounts-panel";
    ($("model-panel") || $("documents")).append(section);
  }
  section.replaceChildren(
    cardHeading(
      "供应商认证",
      "API Key 无需登录；OAuth 登录与退出即时生效，独立于配置保存。",
    ),
  );
  if (!data.accounts.length) {
    section.remove();
    return;
  }
  for (const account of data.accounts) {
    const row = node("section", undefined, "account-row"),
      title = node("div"),
      actions = node("div", undefined, "account-actions");
    title.append(
      node("strong", account.name),
      node(
        "span",
        account.provider +
          " · " +
          (account.auth === "oauth" ? "OAuth 登录" : "API Key"),
        "hint",
      ),
    );
    row.append(title);
    actions.append(button("编辑", () => openEntity("accounts", account.name)));
    for (const [name, label] of [
      ["status", "检查登录"],
      ["catalog", "可用模型"],
      ["login", "登录"],
      ["logout", "退出授权"],
    ]) {
      if (account.auth !== "oauth") continue;
      const button = node(
        "button",
        label,
        name === "login" ? "secondary" : "subtle",
      );
      button.addEventListener("click", () =>
        action(async () => {
          if (dirty() || invalid.size)
            throw Error("请先保存连接设置，再执行账号操作。");
          if (
            ["login", "logout"].includes(name) &&
            !confirm(
              `${label}会立即改变账号授权，影响正在使用此账号的模型。是否继续？`,
            )
          )
            return;
          const result = await api("/api/account", {
            name: account.name,
            action: name,
            confirmed: true,
          });
          row.querySelector(".account-result")?.remove();
          const output = node("details", undefined, "account-result");
          output.append(
            node("summary", label + "结果"),
            node("pre", JSON.stringify(result, null, 2)),
          );
          row.append(output);
          await pollAccounts();
        }),
      );
      actions.append(button);
    }
    row.append(actions);
    section.append(row);
  }
  let flows = $("account-flows");
  if (!flows) {
    flows = node("article");
    flows.id = "account-flows";
    $("documents").append(flows);
  }
  flows.replaceChildren();
  drawFlows(data.flows, flows);
}
function drawFlows(flows, section) {
  for (const flow of flows) {
    const row = node("section", undefined, "flow-card");
    row.append(node("p", `${flow.name} · ${flow.state}`));
    if (flow.message) row.append(node("p", flow.message));
    if (flow.authorization_url) {
      const link = node("a", "打开官方授权页面");
      link.href = flow.authorization_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      row.append(
        link,
        node(
          "p",
          "回调若为 localhost，请将授权端口转发至运行服务的机器。",
          "hint",
        ),
      );
    }
    if (flow.prompt) {
      const label = node("label", flow.prompt.message),
        input = node("input");
      input.type = "password";
      input.autocomplete = "off";
      const answer = node("button", "提交授权信息");
      answer.addEventListener("click", () =>
        action(async () => {
          await api("/api/account-answer", {
            id: flow.id,
            prompt_id: flow.prompt.id,
            value: input.value,
          });
          input.value = "";
          await pollAccounts();
        }),
      );
      label.append(input, answer);
      row.append(label);
    }
    if (flow.state === "pending") {
      const cancel = node("button", "取消授权", "subtle");
      cancel.addEventListener("click", () =>
        action(async () => {
          await api("/api/account-cancel", { id: flow.id });
          await pollAccounts();
        }),
      );
      row.append(cancel);
    }
    section.append(row);
  }
}
async function pollAccounts() {
  clearTimeout(accountTimer);
  accountTimer = undefined;
  if (selected !== "models" || modelTab !== "connections" || $("editor").hidden)
    return;
  const data = await api("/api/accounts");
  if (selected !== "models" || modelTab !== "connections" || $("editor").hidden)
    return;
  let section = $("account-flows");
  if (!section) {
    section = node("article");
    section.id = "account-flows";
    $("documents").append(section);
  }
  section.replaceChildren();
  drawFlows(data.flows, section);
  if (data.flows.some((f) => f.state === "pending"))
    accountTimer = setTimeout(() => action(pollAccounts), 1500);
}
$("repair-open").addEventListener("click", () =>
  action(async () => {
    const data = await api("/api/repair");
    const panel = $("repair");
    panel.hidden = false;
    panel.replaceChildren(
      node(
        "p",
        "只替换下面列出的文件。损坏内容可能含有密钥，因此不会回显；请使用本机内容或备份填写完整 TOML。保存前仍校验整个配置。",
        "hint",
      ),
    );
    const select = node("select");
    for (const name of data.files) {
      const option = node("option", name);
      option.value = name;
      select.append(option);
    }
    const text = node("textarea"),
      save = node("button", "校验并修复");
    text.spellcheck = false;
    save.addEventListener("click", () =>
      action(async () => {
        if (!confirm("替换此配置文件？不会重启业务或回滚业务数据。")) return;
        await api("/api/repair", {
          version: data.version,
          files: { [select.value]: text.value },
          confirmed: true,
        });
        panel.hidden = true;
        $("repair-dialog").close();
        await load();
        $("notice").textContent = "已修复，待重启生效。";
      }),
    );
    select.setAttribute("aria-label", "需要修复的配置文件");
    text.setAttribute("aria-label", "完整 TOML 内容");
    save.className = "primary";
    panel.append(select, text, save);
    showDialog("repair-dialog");
  }),
);

function displayProperties(rule, current) {
  let props = { ...rule.properties };
  for (const branch of rule.allOf || [])
    Object.assign(props, displayProperties(branch, current));
  if (rule.if)
    Object.assign(
      props,
      displayProperties(
        (matches(current, rule.if) ? rule.then : rule.else) || {},
        current,
      ),
    );
  return props;
}

const passportSettings = [
  ["播报", ["speech_mode", "volume", "voice", "rate"]],
  ["屏幕与缓存", ["brightness", "text_size", "show_images", "offline_cache"]],
  ["输入与休眠", ["input_mode", "keep_online", "sleep_seconds"]],
  ["连接", ["wifi_networks", "server_url", "trusted_ca", "max_connections"]],
];

function drawPassportSettings(section, fields) {
  const remaining = new Map(fields.map(field => [field.path.at(-1), field]));
  for (const [title, names] of [...passportSettings, ["其他设置", [...remaining.keys()]]]) {
    const panel = surface(title);
    const advanced = advancedGroup("passport:" + key(fields[0]?.path.slice(0, -1)) + title);
    for (const name of names) {
      const field = remaining.get(name);
      if (!field) continue;
      remaining.delete(name);
      const target = field.schema["x-advanced"] ? advanced : panel;
      target.append(drawField(field));
    }
    if (advanced.children.length > 1) panel.append(advanced);
    if (panel.children.length > 1) section.append(panel);
  }
}

function drawTerminalDevices(plugin, section, fields, enabled) {
  const devices = fields.find(field => field.path.at(-1) === "devices"),
    defaults = fields.filter(field => field !== devices),
    tabs = node("div", undefined, "passport-tabs");
  tabs.setAttribute("role", "group");
  tabs.setAttribute("aria-label", "AI Passport 设置范围");
  for (const [label, isDefault] of [["设备", false], ["默认设置", true]]) {
    const tab = button(label, () => {
      passportDefaults = isDefault;
      passportDevice = null;
      render();
      document.querySelector('.passport-tabs [aria-pressed="true"]')?.focus();
    }, "text-button");
    tab.setAttribute("aria-pressed", String(passportDefaults === isDefault));
    tabs.append(tab);
  }
  section.append(tabs);
  if (!enabled)
    section.append(node("p", "AI Passport 未启用。启用后可配对和管理设备。", "inline-note"));
  if (passportDefaults) {
    section.append(node("p", "这些设置适用于所有配对设备。单台设备有专属设置时，该项优先使用专属值。", "inline-note"));
    drawPassportSettings(section, defaults);
    return;
  }

  const endpoint = "/api/entrances/" + encodeURIComponent(plugin.id);
  const operation = (action, args = {}) => api(endpoint, {action, arguments: args});
  const status = (device) => node("span", device.connected ? "在线" : "离线",
    device.connected ? "badge configured" : "badge");
  const syncLabel = (device) => {
    const configuration = Object.values(device.configuration || {});
    const pending = configuration.filter(field => field.desired !== field.applied).length;
    return pending ? `${pending} 项待设备确认`
      : configuration.length ? "配置已同步" : "尚未同步配置";
  };

  if (passportDevice && enabled) {
    const device = passportDevice,
      panel = surface(device.name, "只影响这台 PP。未覆盖的项目沿用默认设置。", "passport-panel"),
      actions = node("div", undefined, "passport-actions"),
      info = node("details", undefined, "passport-info");
    const back = button("← 返回设备", () => {
      passportDevice = null;
      render();
    }, "text-button");
    back.dataset.passportBack = "";
    const revoke = button("撤销配对", () => action(async () => {
      if (dirty() || invalid.size) throw Error("请先处理配置草稿，再撤销配对。");
      if (!confirm(`撤销「${device.name}」的配对？设备将断开，重新使用时需要再次配对。`)) return;
      revoke.disabled = true;
      try {
        await operation("revoke", {device_id: device.device_id});
        passportDevice = null;
        render();
        showNotice("已撤销配对。", 5000);
      } finally {
        revoke.disabled = false;
      }
    }), "text-button passport-revoke");
    actions.append(revoke);
    info.append(node("summary", "设备标识"),
      node("p", "设备 " + device.device_id), node("p", "会话 " + device.session_id));
    panel.append(actions, info);
    section.append(back, panel);
    const values = devices?.value?.[device.device_id] || {};
    const inherited = new Map(defaults.map(field => [field.path.at(-1), currentValue(field)]));
    const overrides = Object.entries(devices?.schema.additionalProperties?.properties || {}).map(
      ([name, rule]) => ({
        ...devices,
        path: [...devices.path, device.device_id, name],
        schema: {
          ...rule,
          ...(inherited.has(name) ? {default: inherited.get(name)} : {}),
          ...(rule.type === "object" ? {"x-removable": true} : {}),
        },
        value: values[name],
        loaded_value: devices.loaded_value?.[device.device_id]?.[name],
        explicit: Object.hasOwn(values, name),
      }),
    );
    drawPassportSettings(section, overrides);
    return;
  }

  const panel = surface("已配对设备", "选择设备查看连接状态与专属设置。", "passport-panel"),
    header = panel.firstElementChild,
    actions = node("div", undefined, "passport-actions"),
    form = node("form", undefined, "passport-pair"),
    name = node("input"),
    code = node("input"),
    submit = node("button", "确认配对", "primary"),
    list = node("div", undefined, "passport-list"),
    feedback = node("p", "", "passport-feedback");
  feedback.setAttribute("role", "status");
  const showPairing = (open) => {
    form.hidden = !open;
    pair.setAttribute("aria-expanded", String(open));
    if (open) name.focus();
    else {
      form.reset();
      pair.focus();
    }
  };
  const refresh = button("刷新", () => reload()),
    pair = button("配对新设备", () => showPairing(form.hidden), "primary"),
    cancel = button("取消", () => showPairing(false), "secondary");
  pair.setAttribute("aria-expanded", "false");
  pair.setAttribute("aria-controls", "passport-pair-form");
  form.id = "passport-pair-form";
  form.hidden = true;
  name.required = true;
  name.maxLength = 48;
  name.placeholder = "例如 客厅 PP";
  code.required = true;
  code.pattern = "[0-9]{8}";
  code.inputMode = "numeric";
  code.maxLength = 8;
  code.autocomplete = "off";
  code.placeholder = "8 位数字";
  const inputs = node("div", undefined, "passport-pair-inputs");
  for (const [label, input] of [["设备名称", name], ["配对码", code]]) {
    const wrapper = node("label", label);
    wrapper.append(input);
    inputs.append(wrapper);
  }
  const formActions = node("div", undefined, "passport-actions");
  formActions.append(submit, cancel);
  form.append(node("h3", "配对新设备"), node("p", "先在 PP 的热点页面配置 Wi-Fi 和 PA 地址，再输入屏幕上的配对码。配对码十分钟内有效。", "hint"), inputs, formActions);
  actions.append(refresh, pair);
  header.append(actions);
  panel.append(form, feedback, list);
  section.append(panel);

  async function reload() {
    refresh.disabled = true;
    if (!list.childElementCount) list.append(node("p", "正在读取设备…", "empty-state"));
    try {
      const reply = await operation("list");
      const active = reply.devices.filter(device => device.paired && !device.revoked);
      list.replaceChildren();
      for (const device of active) {
        const row = node("div", undefined, "passport-row"),
          description = node("div", undefined, "passport-device-label"),
          configure = button("设备配置", () => {
            passportDevice = device;
            render();
            document.querySelector("[data-passport-back]")?.focus();
          });
        description.append(node("strong", device.name), status(device));
        row.append(description, node("span", syncLabel(device), "hint"), configure);
        list.append(row);
      }
      if (!active.length) list.append(node("p", "还没有配对设备。请使用上方的「配对新设备」开始。", "empty-state"));
    } catch (error) {
      list.replaceChildren(node("p", "无法读取设备列表：" + error.message, "empty-state"));
    } finally {
      refresh.disabled = false;
    }
  }

  form.onsubmit = async (event) => {
    event.preventDefault();
    if (submit.disabled || refresh.disabled) return;
    if (dirty() || invalid.size) {
      feedback.textContent = "请先处理配置草稿，再配对设备。";
      return;
    }
    if (!name.value.trim()) {
      feedback.textContent = "请填写设备名称。";
      name.focus();
      return;
    }
    submit.disabled = cancel.disabled = pair.disabled = refresh.disabled = true;
    feedback.textContent = "正在配对…";
    try {
      await operation("pair", {name: name.value.trim(), code: code.value});
    } catch (error) {
      feedback.textContent = error.message;
      return;
    } finally {
      submit.disabled = cancel.disabled = pair.disabled = refresh.disabled = false;
    }
    showPairing(false);
    feedback.textContent = "配对成功，设备会自动连接。";
    await reload();
  };
  if (enabled) reload();
  else {
    refresh.disabled = pair.disabled = true;
    list.append(node("p", "启用插件后可查看设备列表。", "empty-state"));
  }
}
