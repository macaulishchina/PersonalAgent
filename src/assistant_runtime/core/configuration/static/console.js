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
  connections: "管理外部服务与 MCP 连接。",
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
  plugins: "M4 4h6v6H4V4 M14 4h6v6h-6V4 M4 14h6v6H4v-6 M14 14h6v6h-6v-6",
  extensions: "M8 3h8v5h5v8h-5v5H8v-5H3V8h5V3",
};
let snapshot,
  inventory,
  resourceInventory = [],
  selected = "access",
  expandedOwner,
  saving = false;
const openEntries = new Set();
const resourceEdits = new Map();
const dirty = () => edits.size + resourceEdits.size;
const edits = new Map();
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
async function api(path, body) {
  const response = await fetch(
    path,
    body === undefined
      ? {}
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
  );
  if (response.status === 401) {
    signedOut();
    throw Object.assign(Error("请先登录"), { status: 401 });
  }
  if (!response.headers.get("Content-Type")?.includes("application/json"))
    throw Error(`请求失败（${response.status}）`);
  const data = await response.json();
  if (!response.ok) throw Error(data.error || "请求失败");
  return data;
}
function showNotice(message) {
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
  if (!dialog.open) dialog.showModal();
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
  return field.group;
}
function updateSaveBar() {
  $("count").textContent = invalid.size
    ? `${invalid.size} 项格式需要修正`
    : dirty()
      ? `${dirty()} 项未保存`
      : "没有未保存的修改";
  $("save").disabled = saving || !dirty() || Boolean(invalid.size);
  $("save").textContent = saving ? "保存中…" : "校验并保存";
  $("discard").disabled = saving || (!dirty() && !invalid.size);
  for (const id of ["refresh", "history", "password-open", "apply-open"])
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
    edit?.op === "default"
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
function fieldSource(field) {
  const details = node("details", undefined, "field-source");
  details.append(
    node("summary", "字段信息"),
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
      node("p", "默认值：" + JSON.stringify(field.schema.default), "hint"),
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
    if (secret && !confirm("清除此凭据？保存并重启后生效。")) return;
    if (
      field.schema["x-removable"] &&
      !confirm("移除此配置条目？保存并重启后生效。")
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
  if (rule.description) copy.append(node("p", rule.description, "hint"));
  if (rule["x-readonly-reason"])
    copy.append(node("p", rule["x-readonly-reason"], "hint"));
  const control = node("div", undefined, "field-control"),
    bottom = node("div", undefined, "field-bottom");
  bottom.append(fieldSource(field));
  if (!rule.readOnly) bottom.append(resetControl(field, secret));
  else bottom.append(node("span", "仅本机维护", "hint"));
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
    if (!rule.readOnly) actions.append(resetControl(field));
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
      } else children.append(drawField(definition));
    }
    if (!children.childElementCount)
      children.append(node("p", "尚未添加条目", "empty-state"));
    if (rule.additionalProperties && !rule.readOnly) {
      const toolbar = node("div", undefined, "collection-toolbar"),
        name = node("input"),
        add = node("button", "添加条目", "secondary");
      name.placeholder = "条目名称，例如 primary";
      name.setAttribute(
        "aria-label",
        (rule.title || path.at(-1)) + " · 新条目名称",
      );
      add.type = "button";
      add.addEventListener("click", () => {
        const text = name.value.trim();
        if (
          !/^[A-Za-z_][A-Za-z0-9_-]{0,63}$/.test(text) ||
          Object.hasOwn(current, text)
        ) {
          $("notice").textContent =
            "名称需以字母或下划线开头，且不能与现有条目重复。";
          name.focus();
          return;
        }
        const child = [...path, text];
        openEntries.add(key(child));
        changed(child, "set", seedValue(rule.additionalProperties));
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
  if (path.length === 1 && path[0] === "plugins") {
    row.classList.add("plugin-field");
    input = node("div", undefined, "plugin-picker");
    const available = new Map(
      inventory.plugins.map((plugin) => [plugin.id, plugin]),
    );
    for (const name of value || [])
      if (!available.has(name))
        available.set(name, { id: name, kind: "unavailable" });
    for (const plugin of available.values()) {
      const line = node("label", undefined, "plugin-option"),
        check = node("input"),
        title = node("span"),
        badges = node("span", undefined, "plugin-badges");
      check.type = "checkbox";
      check.value = plugin.id;
      check.checked = (value || []).includes(plugin.id);
      const blocked =
        plugin.platform_supported === false || plugin.kind === "unavailable";
      if (blocked && !check.checked) check.disabled = true;
      // An existing incompatible selection can be removed, but never re-enabled.
      check.addEventListener("change", () => {
        if (blocked && !check.checked) check.disabled = true;
      });
      title.append(
        node("strong", plugin.configuration?.title || plugin.id),
        node("code", plugin.id),
      );
      badges.append(
        node(
          "span",
          plugin.platform_error
            ? "未声明平台认证"
            : plugin.platform_supported === false
              ? "当前系统不支持"
              : plugin.kind === "unavailable"
                ? "未安装"
                : plugin.kind === "core"
                  ? "内核"
                  : "扩展",
          "badge",
        ),
      );
      line.append(check, title, badges);
      if (plugin.kind === "extension") {
        for (const [platform, label] of [
          ["linux", "Linux"],
          ["windows", "Windows"],
        ]) {
          const certified = (plugin.supported_platforms || []).includes(
            platform,
          );
          badges.append(
            node(
              "span",
              `${label} ${certified ? "已认证" : "未认证"}`,
              "badge",
            ),
          );
        }
      }
      if (plugin.platform_error) line.title = plugin.platform_error;
      input.append(line);
    }
    read = () =>
      [...input.querySelectorAll("input:checked")].map((i) => i.value);
  } else if (secret) {
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
  } else if (rule.enum) {
    input = node("select");
    if (value === undefined) {
      const placeholder = node("option", "请选择");
      placeholder.value = "";
      placeholder.disabled = true;
      input.append(placeholder);
    }
    for (const v of rule.enum) {
      const option = node("option", String(v));
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
    if (rule.minimum !== undefined) input.min = rule.minimum;
    if (rule.maximum !== undefined) input.max = rule.maximum;
    input.value = value ?? "";
    read = () => {
      if (!input.value || !input.checkValidity())
        throw Error("请填写符合范围的数值");
      return Number(input.value);
    };
  } else if (rule.type === "array" && Array.isArray(rule.items?.enum)) {
    input = node("div", undefined, "choice-list");
    for (const choice of rule.items.enum) {
      const line = node("label"),
        check = node("input");
      check.type = "checkbox";
      check.value = String(choice);
      check.checked = (value || []).includes(choice);
      line.append(check, node("span", String(choice)));
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
        line.append(item, remove);
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
  if (input.tagName === "DIV") {
    input.setAttribute("role", "group");
    input.setAttribute("aria-labelledby", label.id);
  }
  input.setAttribute("aria-invalid", String(invalid.has(id)));
  input.addEventListener("input", () => {
    if (input.tagName !== "DIV") drafts.set(id, input.value);
    try {
      changed(path, secret ? "replace_secret" : "set", read());
      input.setAttribute("aria-invalid", "false");
      if (
        input.tagName === "SELECT" ||
        input.type === "checkbox" ||
        path[0] === "plugins" ||
        (rule.type === "array" && input.classList.contains("choice-list"))
      ) {
        render();
        document.getElementById(id)?.focus();
      }
    } catch {
      invalid.set(id, path.join("."));
      input.setAttribute("aria-invalid", "true");
      updateSaveBar();
      $("notice").textContent =
        "字段格式不正确：" + (rule.title || path.join("."));
    }
  });
  control.append(element || input);
  const limits = [
    rule.minimum !== undefined ? "最小 " + rule.minimum : null,
    rule.maximum !== undefined ? "最大 " + rule.maximum : null,
  ].filter(Boolean);
  if (limits.length)
    control.append(node("p", limits.join(" · "), "hint control-hint"));
  if (
    Object.hasOwn(field, "loaded_value") &&
    JSON.stringify(field.loaded_value) !== JSON.stringify(field.value) &&
    !secret &&
    field.loaded_value !== undefined
  )
    control.append(
      node("p", "当前已加载：" + JSON.stringify(field.loaded_value), "hint"),
    );
  const sourceWrap = node("div");
  sourceWrap.append(badge, fieldSource(field));
  bottom.replaceChildren(sourceWrap, resetControl(field, secret));
  control.append(bottom);
  row.append(copy, control);
  return row;
}
function cardHeading(title, description) {
  const heading = node("div", undefined, "card-heading");
  heading.append(node("h2", title));
  if (description) heading.append(node("p", description));
  return heading;
}
function render() {
  $("documents").replaceChildren();
  $("navigation").replaceChildren();
  const present = new Set([
    ...snapshot.fields.map(fieldGroup),
    ...resourceInventory.filter((r) => r.kind !== "asset").map(resourceGroup),
  ]);
  const available = [
    ...Object.keys(groups).filter((group) => present.has(group)),
    ...present,
  ].filter((group, index, list) => list.indexOf(group) === index);
  if (!available.includes(selected)) selected = available[0];
  for (const group of available) {
    const button = node(
      "button",
      undefined,
      group === selected ? "active" : "",
    );
    button.append(
      icon(navPaths[group] || navPaths.extensions),
      node("span", groups[group] || group),
    );
    button.setAttribute("aria-current", group === selected ? "page" : "false");
    button.addEventListener("click", () => {
      selected = group;
      render();
      window.scrollTo(0, 0);
    });
    $("navigation").append(button);
  }
  $("page-title").textContent = groups[selected] || selected;
  $("page-description").textContent =
    descriptions[selected] || "管理当前模块的配置。";
  $("breadcrumb-current").textContent = groups[selected] || selected;
  const fields = snapshot.fields.filter((f) => fieldGroup(f) === selected),
    owners = [...new Set(fields.map((f) => f.owner))];
  const pluginField = snapshot.fields.find(
      (f) => key(f.path) === key(["plugins"]),
    ),
    enabled = pluginField ? currentValue(pluginField) : [];
  const extension = (owner) =>
    inventory.plugins.find((p) => p.configuration?.title === owner);
  if (!owners.includes(expandedOwner))
    expandedOwner =
      owners.find((owner) => enabled.includes(extension(owner)?.id)) ||
      owners[0];
  for (const owner of owners) {
    const plugin = extension(owner),
      collapsible = selected === "extensions",
      section = node(
        collapsible ? "details" : "article",
        undefined,
        collapsible ? "owner-panel" : "",
      ),
      advanced = node("details", undefined, "advanced-fields");
    if (collapsible) {
      section.open = owner === expandedOwner;
      const summary = node("summary", owner);
      summary.append(
        node(
          "span",
          enabled.includes(plugin?.id) ? "已启用" : "未启用",
          "badge",
        ),
      );
      section.append(summary);
      section.addEventListener("toggle", () => {
        if (section.open) {
          expandedOwner = owner;
          for (const other of $("documents").querySelectorAll(".owner-panel"))
            if (other !== section) other.open = false;
        }
      });
      if (!enabled.includes(plugin?.id))
        section.append(
          node(
            "p",
            "此插件尚未启用。请在插件管理中选择插件，保存时会校验完整依赖。",
            "hint",
          ),
        );
    } else
      section.append(
        cardHeading(
          selected === "plugins" ? "启用的插件" : owner,
          plugin?.configuration?.description,
        ),
      );
    advanced.append(node("summary", "高级设置"));
    for (const field of fields.filter((f) => f.owner === owner))
      (field.schema["x-advanced"] ? advanced : section).append(
        drawField(field),
      );
    if (advanced.childElementCount > 1) section.append(advanced);
    $("documents").append(section);
  }
  drawResources();
  if (selected === "models") action(drawAccounts);
  else {
    clearTimeout(accountTimer);
    accountTimer = undefined;
  }
  updateSaveBar();
}
async function load() {
  if (
    (dirty() || invalid.size) &&
    !confirm("重新加载会放弃尚未保存的编辑，是否继续？")
  )
    return;
  const status = await api("/api/status");
  $("login-screen").hidden = true;
  $("login").hidden = true;
  $("editor").hidden = false;
  $("password-open").hidden = false;
  const [config, descriptions, resources] = await Promise.all([
    api("/api/configuration"),
    api("/api/catalog"),
    api("/api/resources"),
  ]);
  resourceInventory = resources.resources;
  resourceEdits.clear();
  snapshot = config;
  inventory = descriptions;
  edits.clear();
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
      : "已保存 · 等待重启生效";
  $("state-version").textContent = "版本 " + config.version.slice(0, 10);
  $("health").textContent =
    {
      running: "业务运行中",
      stopped: "业务已停止",
      failed: "启动失败",
      degraded: "部分功能不可用",
      starting: "业务启动中",
    }[status.state] || status.state;
  $("health").dataset.state = status.state;
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
$("discard").addEventListener("click", () => action(load));
$("apply-open").addEventListener("click", () =>
  action(async () => {
    await applicationStatus();
    showDialog("apply-dialog");
  }),
);
$("save").addEventListener("click", () =>
  action(async () => {
    if (invalid.size)
      throw Error("请先修正字段格式：" + [...invalid.values()].join("、"));
    if (!dirty()) {
      $("notice").textContent = "没有未保存的修改。";
      return;
    }
    saving = true;
    $("notice").textContent = "";
    updateSaveBar();
    try {
      const saved = await api("/api/configuration", {
        version: snapshot.version,
        patches: [...edits.values()],
        resources: [...resourceEdits.values()],
      });
      edits.clear();
      resourceEdits.clear();
      await load();
      $("notice").textContent = saved.changed
        ? "已保存，待重启生效。"
        : "没有配置变更。";
    } finally {
      saving = false;
      updateSaveBar();
    }
  }),
);
$("history").addEventListener("click", () =>
  action(async () => {
    const history = await api("/api/history");
    $("versions").replaceChildren();
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
          if (!confirm("恢复此前的配置？业务数据不会回滚，保存后仍需重启。"))
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
  if (dirty() || invalid.size) {
    e.preventDefault();
    e.returnValue = "";
  }
});
load().catch((error) => {
  if (error.status !== 401) showNotice(error.message);
});

let applicationTimer;
async function applicationStatus() {
  const status = await api("/api/status"),
    application = status.application || { phase: "idle" };
  if ($("editor").hidden) return;
  const labels = {
    idle: "尚未发起应用",
    preparing: "校验配置中",
    waiting: "正在等待工作结束",
    cancelling: "正在取消在途工作",
    restarting: "正在重启业务服务",
    applied: "已应用指定配置版本",
    partially_applied: "已应用，但部分功能或外部部署步骤尚未就绪",
    failed: "应用失败，请查看业务诊断",
    withdrawn: "已撤销等待，继续接收新工作",
  };
  $("application-state").textContent =
    labels[application.phase] || application.phase;
  const pending = snapshot && status.loaded_version !== snapshot.version,
    partial = application.phase === "partially_applied";
  if (pending && ["applied", "idle", "withdrawn"].includes(application.phase))
    $("application-state").textContent = "待应用 · 新配置将在重启后加载";
  $("application-state").parentElement.dataset.state = pending
    ? "pending"
    : application.phase;
  $("state").textContent = partial
    ? "已加载 · 仍有待处理事项"
    : pending
      ? "已保存 · 等待重启生效"
      : "已应用 · 配置与运行版本一致";
  document
    .querySelector(".configuration-state")
    .classList.toggle("pending", Boolean(pending || partial));
  $("health").textContent =
    {
      running: "业务运行中",
      stopped: "业务已停止",
      failed: "启动失败",
      degraded: "部分功能不可用",
      starting: "业务启动中",
    }[status.state] || status.state;
  $("health").dataset.state = status.state;
  const workLabels = {
    queued: "等待执行",
    dispatching: "正在启动",
    running: "执行中",
    waiting_user: "等待你回答",
    cancelling: "正在取消",
    unknown: "结果待核实",
    paused: "已暂停，需要处理",
    scheduled_immediate: "等待派发",
    finalizing: "正在保存结果",
    model_review: "正在整理执行评估",
    model_call: "模型请求尚未结束",
    memory_capture: "正在处理记忆",
    capability_call: "操作尚未结束",
    drain_not_supported: "插件尚不支持等待检查；可撤销等待或选择终止并重启",
  };
  $("blockers").replaceChildren(
    ...(application.blockers || []).map((item) => {
      const owner =
        inventory?.plugins.find((plugin) => plugin.id === item.owner)
          ?.configuration?.title ||
        item.owner ||
        "";
      const message =
        item.owner === "admission"
          ? "正在准备已接受的请求"
          : workLabels[item.state] || "操作尚未结束";
      const row = node(
        "li",
        `${owner} ${item.id ? item.id.slice(0, 12) : ""} · ${message}`,
      );
      if (item.id) row.title = item.id;
      return row;
    }),
    ...(application.required_steps || []).map((item) =>
      node("li", item.message),
    ),
  );
  for (const step of application.required_steps || [])
    if (application.phase === "partially_applied") {
      const li = node("li"),
        button = node("button", "确认已手动完成 " + step.component, "subtle");
      button.addEventListener("click", () =>
        action(async () => {
          if (
            !confirm(
              "仅在已完成并验证此外部部署步骤后确认；此按钮不会执行部署。",
            )
          )
            return;
          await api("/api/complete-step", {
            version: application.version,
            component: step.component,
            confirmed: true,
          });
          await applicationStatus();
        }),
      );
      li.append(button);
      $("blockers").append(li);
    }
  if (application.management_address) {
    const address = application.management_address;
    let host = ["0.0.0.0", "::"].includes(address.host)
      ? location.hostname
      : address.host;
    if (host.includes(":") && !host.startsWith("[")) host = `[${host}]`;
    const target =
      address.public_origin || `${location.protocol}//${host}:${address.port}`;
    if (target !== location.origin) {
      const link = node("a", "打开新的配置地址");
      link.href = target;
      $("application-state").append(" · ", link);
    }
  }
  if (application.resume_failed)
    $("application-state").append(
      "；恢复接收新工作失败，请检查插件或终止并重启。",
    );
  const active =
    status.state === "starting" ||
    ["preparing", "waiting", "cancelling", "restarting"].includes(
      application.phase,
    );
  if (status.state === "starting") {
    $("state").textContent = "正在启动 · 核对配置与业务状态";
    $("application-state").textContent = "业务启动中，正在核对配置";
  }
  $("apply-wait").disabled = active;
  $("apply-force").disabled = active;
  $("abort-apply").disabled = !["preparing", "waiting"].includes(
    application.phase,
  );
  if (active && !applicationTimer)
    applicationTimer = setInterval(() => action(applicationStatus), 1000);
  if (!active && applicationTimer) {
    clearInterval(applicationTimer);
    applicationTimer = undefined;
  }
}
async function applyConfiguration(mode) {
  if (dirty() || invalid.size) throw Error("请先保存或放弃尚未保存的编辑");
  const preview = await api("/api/application-preview");
  if (
    !confirm(
      `将应用版本 ${preview.version.slice(0, 12)}，并重启业务服务。\n变更字段：${preview.fields.join("、") || "无文件变化"}\n${(preview.required_steps || []).map((step) => step.message).join("\n")}\n${mode === "wait" ? "等待已接受的工作完成；等待期间可撤销。" : "当前工作将被终止，外部操作可能已经发生。"}`,
    )
  )
    return;
  if (
    mode === "force" &&
    !confirm("再次确认：取消在途工作并终止业务进程？未知结果不会自动重做。")
  )
    return;
  await api("/api/apply", { version: preview.version, mode, confirmed: true });
  await applicationStatus();
}
$("apply-wait").addEventListener("click", () =>
  action(() => applyConfiguration("wait")),
);
$("apply-force").addEventListener("click", () =>
  action(() => applyConfiguration("force")),
);
$("abort-apply").addEventListener("click", () =>
  action(async () => {
    await api("/api/abort-apply", {});
    await applicationStatus();
  }),
);

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
      mcp: "connections",
      management: "access",
      web_access: "access",
      gateway_key: "models",
    }[resource.id] || (resource.kind === "skill" ? "resources" : "extensions")
  );
}
function drawResources() {
  const resources = resourceInventory.filter(
    (r) => r.kind !== "asset" && resourceGroup(r) === selected,
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
          if (confirm("清除此文件凭据？保存及重启后生效。")) {
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
      if (resource.id === "management")
        content.append(
          node(
            "p",
            "管理地址变更可能中断当前连接；应用前请记录新地址。绑定失败会保留旧入口。",
            "hint",
          ),
        );
      if (resource.id === "mcp")
        content.append(
          node(
            "p",
            "提交完整 MCP 定义 JSON；已有内容不回显，版本由所属模块递增。凭据请使用环境变量引用。",
            "hint",
          ),
        );
    }
    row.append(content);
    section.append(row);
  }
  $("documents").append(section);
}
let accountTimer;
async function drawAccounts() {
  const data = await api("/api/accounts");
  if (selected !== "models" || $("editor").hidden) return;
  let section = $("accounts-panel");
  if (!section) {
    section = node("article");
    section.id = "accounts-panel";
    $("documents").append(section);
  }
  section.replaceChildren(
    cardHeading("原生账号授权", "登录与退出即时生效，账号授权独立于配置保存。"),
  );
  if (!data.accounts.length)
    section.append(node("p", "尚未配置原生账号", "empty-state"));
  for (const account of data.accounts) {
    const row = node("section", undefined, "account-row"),
      title = node("div"),
      actions = node("div", undefined, "account-actions");
    title.append(
      node("strong", account.name),
      node("span", account.provider + " · " + account.auth, "hint"),
    );
    row.append(title);
    for (const [name, label] of [
      ["status", "检查授权"],
      ["catalog", "可用模型"],
      ["login", "登录"],
      ["logout", "退出授权"],
    ]) {
      if (account.environment_key && ["login", "logout"].includes(name))
        continue;
      const button = node(
        "button",
        label,
        name === "login" ? "secondary" : "subtle",
      );
      button.addEventListener("click", () =>
        action(async () => {
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
  if (selected !== "models" || $("editor").hidden) return;
  const data = await api("/api/accounts");
  if (selected !== "models" || $("editor").hidden) return;
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
