"use strict";

// Product connection forms. Persistence and credentials still use the shared configuration service.
const providerLabels = {
  openai: "OpenAI",
  claude: "Anthropic",
  gemini: "Google Gemini",
  deepseek: "DeepSeek",
  opencode_responses: "OpenCode Console · OpenAI Responses",
  opencode_chat: "OpenCode Console · Chat Completions",
  opencode_anthropic: "OpenCode Console · Anthropic",
  opencode_gemini: "OpenCode Console · Gemini",
  opencode_go_responses: "OpenCode Go · OpenAI Responses",
  opencode_go_chat: "OpenCode Go · Chat Completions",
  opencode_go_anthropic: "OpenCode Go · Anthropic",
  opencode_go_gemini: "OpenCode Go · Gemini",
};
const protocolLabels = {
  openai_responses: "OpenAI Responses",
  openai_chat: "OpenAI Chat Completions",
  anthropic: "Anthropic Messages",
  gemini: "Google Gemini",
};
function newCredentialReference() {
  // Reference identity is internal, independent of the human-readable connection name.
  const bytes = crypto.getRandomValues(new Uint8Array(12));
  return (
    "PA_CONNECTION_" +
    [...bytes]
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("")
      .toUpperCase() +
    "_KEY"
  );
}
function credentialInfo(reference) {
  return resourceInventory
    .find((r) => r.id === "credentials")
    ?.keys?.find((k) => k.name === reference);
}
function credentialUsers(reference) {
  return (
    Object.values(valueAt(["model_api", "providers"], {})).filter(
      (p) => providerKeyName(p) === reference,
    ).length +
    Object.values(valueAt(["model_api", "accounts"], {})).filter(
      (a) => a.key_env === reference,
    ).length
  );
}
function connectionCredentials(
  container,
  reference,
  {
    onChange = () => {},
    inputId = "connection-key",
    prefix = "connection",
  } = {},
) {
  const box = node("section", undefined, "connection-auth");
  const initial = reference;
  let managed =
    reference && credentialInfo(reference)?.source !== "environment"
      ? reference
      : newCredentialReference();
  const input = formInput(box, prefix + "-key", "API Key", {
    type: "password",
  });
  input.id = inputId;
  input.closest("label").htmlFor = input.id;
  input.value = resourceEdits.get("credentials:" + reference)?.value || "";
  const state = node("p", "", "hint");
  box.append(state);
  const advanced = node("details", undefined, "connection-auth-options");
  advanced.append(node("summary", "高级认证来源"));
  const source = formInput(advanced, prefix + "-source", "密钥来源", {
    choices: [
      ["managed", "在 PA 中填写 API Key（推荐）"],
      ["reference", "引用已有凭据 / 服务器环境变量"],
    ],
  });
  source.value =
    credentialInfo(reference)?.source === "environment"
      ? "reference"
      : "managed";
  const ref = formInput(
    advanced,
    prefix + "-reference",
    "已有凭据或环境变量名称",
    {
      value: reference || "",
      pattern: "[A-Z][A-Z0-9_]{0,127}",
      hint: "仅供已有环境管理使用。填写名称，不填密钥值；服务器环境变量优先于实例凭据。",
    },
  );
  ref.dataset.connectionField = "key_env";
  box.append(advanced);
  container.append(box);
  function read() {
    return {
      reference: source.value === "reference" ? ref.value.trim() : managed,
      secret: source.value === "managed" ? input.value : "",
    };
  }
  function refresh() {
    const external = source.value === "reference";
    input.closest("label").hidden = external;
    input.disabled = external;
    ref.closest("label").hidden = !external;
    ref.disabled = !external;
    ref.required = external;
    const info = credentialInfo(external ? ref.value.trim() : managed);
    input.placeholder = info?.configured
      ? "已保存；留空保持，输入新 Key 可替换"
      : "粘贴供应商 API Key";
    state.textContent = external
      ? "使用指定来源读取密钥；不需要在此填写 API Key。"
      : input.value
        ? "已输入新密钥 · 测试不会保存，保存并生效后才写入私有凭据。"
        : info?.configured
          ? "API Key 已保存 · 无需重复填写。"
          : "尚未配置 API Key · 内部存储名称由 PA 自动管理。";
  }
  input.addEventListener("input", () => {
    // Replacing a shared key must not silently change another connection.
    if (input.value && managed === initial && credentialUsers(managed) > 1)
      managed = newCredentialReference();
    refresh();
    onChange();
  });
  source.addEventListener("change", () => {
    refresh();
    onChange();
  });
  ref.addEventListener("input", () => {
    refresh();
    onChange();
  });
  refresh();
  return {
    read,
    element: box,
    reset() {
      managed = newCredentialReference();
      input.value = "";
      ref.value = "";
      source.value = "managed";
      refresh();
    },
  };
}
function stageConnectionSecret(previous, current, secret) {
  if (previous && previous !== current && credentialUsers(previous) === 0)
    resourceEdits.delete("credentials:" + previous);
  if (secret)
    resourceEdits.set("credentials:" + current, {
      id: "credentials",
      key: current,
      op: "replace_secret",
      value: secret,
    });
  else if (current && previous === current && credentialUsers(current) <= 1)
    resourceEdits.delete("credentials:" + current);
}
function apiConnectionFields(
  container,
  initial = {},
  {
    onChange = () => {},
    prefix = "connection",
    inputId = "connection-key",
  } = {},
) {
  const presets = snapshot.provider_presets || {};
  const box = node("div", undefined, "api-connection-fields");
  container.append(box);
  const type = formInput(box, prefix + "-type", "接口类型", {
    choices: [
      ["custom", "自定义 / OpenAI 兼容接口"],
      ...Object.keys(presets).map((k) => [k, providerLabels[k] || k]),
    ],
  });
  type.dataset.connectionField = "preset";
  type.value =
    initial.preset && presets[initial.preset] ? initial.preset : "custom";
  const protocol = formInput(box, prefix + "-protocol", "接口协议", {
    choices: Object.entries(protocolLabels),
    value: initial.protocol || "openai_responses",
  });
  protocol.dataset.connectionField = "protocol";
  const endpoint = formInput(box, prefix + "-url", "接口地址", {
    type: "url",
    required: true,
    value: initial.base_url || "",
    placeholder: "https://api.example.com/v1",
  });
  endpoint.dataset.connectionField = "base_url";
  const presetInfo = node("p", "", "hint");
  box.append(presetInfo);
  let notify = () => {};
  const auth = connectionCredentials(box, providerKeyName(initial), {
    onChange: () => notify(),
    prefix,
    inputId,
  });
  const advanced = node("details", undefined, "connection-protocol-options");
  advanced.append(node("summary", "高级协议设置"));
  const scheme = formInput(advanced, prefix + "-scheme", "认证请求头", {
    choices: [
      ["native", "按接口协议自动选择（推荐）"],
      ["bearer", "强制使用 Authorization: Bearer"],
    ],
    value: initial.auth_scheme || "native",
  });
  scheme.dataset.connectionField = "auth_scheme";
  const header = formInput(advanced, prefix + "-header", "会话请求头", {
    value: initial.session_header || "",
    pattern: "x-[a-z0-9-]{1,60}",
    hint: "通常留空，仅在供应商明确要求时填写，例如 x-session-id。这里不是会话编号。",
  });
  header.dataset.connectionField = "session_header";
  const catalogUrl = formInput(
    advanced,
    prefix + "-catalog-url",
    "模型目录地址",
    {
      type: "url",
      value: initial.catalog?.url || "",
      hint: "通常留空。供应商使用独立目录时填写完整地址，须与推理接口同源。",
    },
  );
  catalogUrl.dataset.connectionField = "catalog.url";
  const catalogFormat = formInput(
    advanced,
    prefix + "-catalog-format",
    "模型目录格式",
    {
      choices: [
        ["openai", "OpenAI"],
        ["anthropic", "Anthropic"],
        ["gemini", "Gemini"],
      ],
      value: initial.catalog?.format || "openai",
    },
  );
  catalogFormat.dataset.connectionField = "catalog.format";
  box.append(advanced);
  const enabled = formInput(box, prefix + "-enabled", "启用此连接", {
    type: "checkbox",
  });
  enabled.checked = initial.enabled === true;
  let previousType = type.value;
  let custom = {
    protocol: protocol.value,
    url: endpoint.value,
    scheme: scheme.value,
    header: header.value,
    catalogUrl: catalogUrl.value,
    catalogFormat: catalogFormat.value,
  };
  function refresh() {
    const spec = presets[type.value];
    protocol.closest("label").hidden = Boolean(spec);
    protocol.disabled = Boolean(spec);
    advanced.hidden = Boolean(spec);
    scheme.disabled =
      header.disabled =
      catalogUrl.disabled =
      catalogFormat.disabled =
        Boolean(spec);
    endpoint.readOnly = Boolean(spec);
    if (spec) {
      endpoint.value = spec.base_url;
      protocol.value = spec.protocol;
    }
    presetInfo.textContent = spec
      ? "地址和协议由预设提供。如需修改地址，请选择自定义接口。"
      : "地址应填写 API 根路径；认证方式按协议自动选择。";
  }
  function read() {
    const credential = auth.read();
    const provider = {
      enabled: enabled.checked,
      key_env: credential.reference,
      ...(initial.presentation ? { presentation: initial.presentation } : {}),
    };
    if (presets[type.value]) provider.preset = type.value;
    else {
      provider.protocol = protocol.value;
      provider.base_url = endpoint.value.trim();
      if (scheme.value !== "native") provider.auth_scheme = scheme.value;
      if (header.value.trim()) provider.session_header = header.value.trim();
      if (catalogUrl.value.trim())
        provider.catalog = {
          url: catalogUrl.value.trim(),
          format: catalogFormat.value,
        };
    }
    return { provider, secret: credential.secret };
  }
  function valid() {
    for (const input of box.querySelectorAll("input,select"))
      if (!input.disabled && !input.checkValidity()) {
        for (let p = input.parentElement; p; p = p.parentElement)
          if (p.tagName === "DETAILS") p.open = true;
        input.reportValidity();
        return false;
      }
    return true;
  }
  notify = () => onChange(read());
  type.addEventListener("change", () => {
    if (previousType === "custom")
      custom = {
        protocol: protocol.value,
        url: endpoint.value,
        scheme: scheme.value,
        header: header.value,
        catalogUrl: catalogUrl.value,
        catalogFormat: catalogFormat.value,
      };
    const before = endpoint.value;
    if (type.value === "custom") {
      if (!custom.url && presets[previousType]) {
        const previous = presets[previousType];
        custom = {
          protocol: previous.protocol,
          url: previous.base_url,
          scheme: previous.auth_scheme || "native",
          header: previous.session_header || "",
          catalogUrl: previous.catalog?.url || "",
          catalogFormat: previous.catalog?.format || "openai",
        };
      }
      protocol.value = custom.protocol;
      endpoint.value = custom.url || before;
      scheme.value = custom.scheme;
      header.value = custom.header;
      catalogUrl.value = custom.catalogUrl;
      catalogFormat.value = custom.catalogFormat;
    }
    refresh();
    if (endpoint.value !== before) {
      auth.reset();
      presetInfo.textContent +=
        " 目标地址已变化，请重新输入对应的 API Key。原有密钥不会删除。";
    }
    previousType = type.value;
    notify();
  });
  for (const control of [
    protocol,
    endpoint,
    scheme,
    header,
    catalogUrl,
    catalogFormat,
    enabled,
  ])
    control.addEventListener("input", notify);
  refresh();
  return { read, valid, element: box };
}
function renderProviderEditor(field, name) {
  const current = currentValue(field);
  const body = $("entry-body");
  body.replaceChildren();
  if (current.account) {
    const account =
      valueAt(["model_api", "accounts"], {})[current.account] || {};
    let auth;
    activeEditor.readConnection = () => {
      const value = currentValue(field);
      return {
        provider: {
          account: value.account,
          enabled: value.enabled === true,
          ...(value.presentation ? { presentation: value.presentation } : {}),
        },
        secret: auth?.read().secret,
      };
    };
    activeEditor.commit = () => {
      const value = activeEditor.readConnection().provider;
      if (JSON.stringify(value) !== JSON.stringify(currentValue(field)))
        changed(field.path, "set", value);
    };
    const copy = structuredClone(field);
    copy.schema.properties = Object.fromEntries(
      Object.entries(copy.schema.properties).filter(([k]) => k === "enabled"),
    );
    body.append(
      drawField(copy),
      node(
        "p",
        account.auth === "oauth"
          ? "使用供应商 OAuth 登录。保存设置后完成登录，无需填写 API Key。"
          : "使用供应商官方 API Key，无需账号登录。此连接沿用 Pi 原生供应商适配。",
        "inline-note",
      ),
    );
    if (account.auth === "api_key") {
      let previous = account.key_env;
      auth = connectionCredentials(body, previous, {
        onChange() {
          const value = auth.read();
          changed(
            ["model_api", "accounts", current.account, "key_env"],
            "set",
            value.reference,
          );
          stageConnectionSecret(previous, value.reference, value.secret);
          previous = value.reference;
          updateSaveBar();
        },
      });
    } else {
      body.append(
        button("编辑 OAuth 设置", () => {
          finishEntity();
          if (!activeEditor) openEntity("accounts", current.account);
        }),
      );
    }
  } else {
    let previousSecret = providerKeyName(current);
    const editor = apiConnectionFields(body, current, {
      onChange(value) {
        const presentation = currentValue(field).presentation;
        if (presentation) value.provider.presentation = presentation;
        else delete value.provider.presentation;
        changed(field.path, "set", value.provider);
        stageConnectionSecret(
          previousSecret,
          value.provider.key_env,
          value.secret,
        );
        previousSecret = value.provider.key_env;
        updateSaveBar();
      },
    });
    activeEditor.readConnection = () => {
      const value = editor.read();
      const presentation = currentValue(field).presentation;
      if (presentation) value.provider.presentation = presentation;
      else delete value.provider.presentation;
      return value;
    };
    activeEditor.validate = editor.valid;
    activeEditor.commit = () => {
      const value = activeEditor.readConnection();
      if (
        JSON.stringify(value.provider) !==
        JSON.stringify(valueAt(["model_api", "providers"], {})[name])
      )
        changed(field.path, "set", value.provider);
    };
  }
  body.append(renderModelPresentation(field.path, current.presentation || {}));
  const probe = providerTest(name);
  const panel = node("div", undefined, "provider-probe");
  panel.append(probe.button, probe.status);
  body.append(panel);
  body.addEventListener(
    "input",
    () => {
      if (!probe.status.hidden && !probe.button.disabled) {
        probe.status.textContent = "输入已修改，可重新测试。";
        probe.status.dataset.state = "";
      }
    },
    { signal: editorInputController.signal },
  );
}

let modelAvatarCatalog;
function loadModelAvatars() {
  modelAvatarCatalog ??= api("/api/model-avatars").catch((error) => {
    modelAvatarCatalog = undefined;
    throw error;
  });
  return modelAvatarCatalog;
}

function renderModelPresentation(path, initial = {}) {
  const box = node("section", undefined, "model-presentation");
  box.setAttribute("aria-label", "个性配置");
  box.append(node("h3", "个性配置"));
  const value = structuredClone(initial);
  const fields = node("div", undefined, "model-presentation-fields");
  const name = formInput(fields, "public-model-name", "公开名称");
  name.maxLength = 80;
  name.value = value.display_name || "";
  name.placeholder = "沿用登记名称";
  const description = formInput(fields, "public-model-description", "简短说明");
  description.maxLength = 400;
  description.value = value.description || "";
  const avatarRow = node("div", undefined, "model-avatar-row");
  const preview = node("img");
  preview.width = preview.height = 48;
  preview.alt = "模型头像预览";
  const avatarCopy = node("div", undefined, "model-avatar-copy");
  const avatarName = node("strong", "默认头像", "model-avatar-name");
  const avatarOrigin = node("span", "", "model-avatar-origin");
  avatarCopy.append(
    node("span", "头像", "model-avatar-label"),
    avatarName,
    avatarOrigin,
  );
  const actions = node("div", undefined, "model-avatar-actions");
  avatarRow.append(preview, avatarCopy, actions);
  const picker = node("div", undefined, "model-avatar-picker");
  picker.id = "model-avatar-library";
  picker.hidden = true;
  picker.setAttribute("role", "region");
  picker.setAttribute("aria-label", "内置头像");
  const browse = button(
    "选择内置头像",
    () => {
      picker.hidden = !picker.hidden;
      browse.setAttribute("aria-expanded", String(!picker.hidden));
      if (!picker.hidden) search.focus();
    },
    "secondary model-avatar-browse",
  );
  browse.setAttribute("aria-expanded", "false");
  browse.setAttribute("aria-controls", picker.id);
  const total = node("span", "", "model-avatar-count");
  total.setAttribute("aria-hidden", "true");
  browse.append(total);
  const search = formInput(picker, "avatar-search", "搜索头像");
  search.type = "search";
  search.placeholder = "提供商、名称或图案…";
  search.addEventListener("keydown", (event) => {
    if (event.key === "Enter") event.preventDefault();
  });
  const choices = node("div", undefined, "model-avatar-choices");
  const hint = node("p", "正在读取内置头像…", "hint");
  picker.append(choices, hint);
  let catalog = [];
  function builtinUrl(name) {
    const item = catalog.find((item) => item.name === name);
    return `/api/model-avatar/builtin/${encodeURIComponent(name)}${item ? `?version=${item.version}` : ""}`;
  }
  function effectiveAvatar() {
    if (value.avatar) return value.avatar;
    if (path[1] === "entries") {
      const entry = valueAt(["model_api", "entries"], {})[path[2]] || {};
      const provider = valueAt(["model_api", "providers"], {})[entry.provider];
      return provider?.presentation?.avatar || "builtin:generic";
    }
    return "builtin:generic";
  }
  function show() {
    const ref = effectiveAvatar();
    avatarName.textContent = ref.startsWith("asset:")
      ? "自定义图片"
      : catalog.find((item) => item.name === ref.slice(8))?.label || "默认头像";
    avatarOrigin.textContent =
      !value.avatar && path[1] === "entries" ? "继承连接" : "";
    avatarOrigin.hidden = !avatarOrigin.textContent;
    reset.disabled = !value.avatar;
    if (ref.startsWith("asset:")) {
      const id = ref.slice(6);
      const staged = resourceEdits.get("model-avatar-" + id);
      preview.src = staged
        ? "data:image/png;base64," + staged.value
        : `/api/model-avatar/${id}?version=${id}`;
    } else preview.src = builtinUrl(ref.slice(8));
    for (const button of choices.querySelectorAll("button[data-avatar]"))
      button.setAttribute(
        "aria-pressed",
        String(ref === button.dataset.avatar),
      );
  }
  function save() {
    changed(
      [...path, "presentation"],
      Object.keys(value).length ? "set" : "default",
      value,
    );
    const referenced = new Set();
    for (const group of ["providers", "entries"])
      for (const item of Object.values(valueAt(["model_api", group], {}))) {
        const ref = item.presentation?.avatar;
        if (ref?.startsWith("asset:"))
          referenced.add("model-avatar-" + ref.slice(6));
      }
    for (const id of resourceEdits.keys())
      if (id.startsWith("model-avatar-") && !referenced.has(id))
        resourceEdits.delete(id);
    updateSaveBar();
    show();
  }
  name.addEventListener("input", () => {
    if (name.value.trim()) value.display_name = name.value.trim();
    else delete value.display_name;
    save();
  });
  description.addEventListener("input", () => {
    if (description.value) value.description = description.value;
    else delete value.description;
    save();
  });
  function filter() {
    const query = search.value.trim().toLocaleLowerCase();
    let count = 0;
    for (const group of choices.children) {
      let visible = 0;
      for (const button of group.querySelectorAll("button[data-avatar]")) {
        button.hidden = !button.dataset.search.includes(query);
        if (!button.hidden) visible++;
      }
      group.hidden = !visible;
      count += visible;
    }
    hint.textContent = count ? `${count} 个头像` : "没有匹配的头像";
  }
  search.addEventListener("input", filter);
  loadModelAvatars()
    .then((result) => {
      if (!box.isConnected) return;
      catalog = result.avatars;
      for (const [groupId, label] of [
        ["providers", "AI 提供商"],
        ["general", "通用头像"],
      ]) {
        const group = node("section", undefined, "model-avatar-group");
        group.setAttribute("aria-label", label);
        group.append(node("h4", label));
        const grid = node("div", undefined, "model-avatar-grid");
        for (const item of catalog.filter((item) => item.group === groupId)) {
          const ref = "builtin:" + item.name;
          const choice = button("", () => {
            value.avatar = ref;
            status.textContent = uploadHint;
            save();
            picker.hidden = true;
            browse.setAttribute("aria-expanded", "false");
            browse.focus();
          });
          choice.type = "button";
          choice.className = "model-avatar-choice";
          choice.dataset.avatar = ref;
          choice.dataset.search =
            `${item.label} ${item.name}`.toLocaleLowerCase();
          choice.setAttribute("aria-label", item.label);
          choice.title = item.label;
          const icon = node("img");
          icon.alt = "";
          icon.loading = "lazy";
          icon.src = builtinUrl(item.name);
          choice.append(icon, node("span", item.label));
          grid.append(choice);
        }
        group.append(grid);
        choices.append(group);
      }
      total.textContent = String(catalog.length);
      filter();
      show();
    })
    .catch(() => {
      hint.textContent = "内置头像暂时无法读取，可继续使用自定义图片。";
    });
  const upload = document.createElement("input");
  upload.type = "file";
  upload.hidden = true;
  upload.accept = "image/png,image/jpeg,image/webp";
  upload.setAttribute("aria-label", "上传模型头像");
  const uploadButton = button("上传图片", () => upload.click());
  const uploadHint = "PNG、JPEG 或 WebP，最大 2 MiB。保存配置后生效。";
  const status = node("p", uploadHint, "hint model-avatar-status");
  status.setAttribute("role", "status");
  upload.addEventListener("change", async () => {
    const file = upload.files[0];
    if (!file) return;
    if (file.size > 2 * 1024 * 1024) {
      status.textContent = "图片不能超过 2 MiB。";
      return;
    }
    upload.disabled = true;
    uploadButton.disabled = true;
    const version = snapshot.version;
    try {
      const data = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(",")[1]);
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });
      const result = await api("/api/model-avatar", {
        version,
        data_base64: data,
      });
      if (!box.isConnected || snapshot.version !== version) return;
      resourceEdits.set(result.resource.id, result.resource);
      value.avatar = result.avatar;
      save();
      status.textContent = "图片已校验，保存配置后生效。";
    } catch (error) {
      status.textContent = error.message;
    } finally {
      upload.disabled = false;
      uploadButton.disabled = false;
      upload.value = "";
    }
  });
  const reset = button(
    "恢复默认",
    () => {
      delete value.avatar;
      status.textContent = uploadHint;
      save();
    },
    "text-button",
  );
  reset.setAttribute("aria-label", "恢复默认头像");
  actions.append(browse, uploadButton, reset);
  picker.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    event.stopPropagation();
    picker.hidden = true;
    browse.setAttribute("aria-expanded", "false");
    browse.focus();
  });
  box.append(fields, avatarRow, picker, upload, status);
  show();
  return box;
}
