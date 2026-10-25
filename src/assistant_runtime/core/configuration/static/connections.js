"use strict";

// Product connection forms. Persistence and credentials still use the shared configuration service.
const providerLabels = {
  openai: "OpenAI",
  claude: "Anthropic",
  gemini: "Google Gemini",
  deepseek: "DeepSeek",
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
  };
  function refresh() {
    const spec = presets[type.value];
    protocol.closest("label").hidden = Boolean(spec);
    protocol.disabled = Boolean(spec);
    advanced.hidden = Boolean(spec);
    scheme.disabled = header.disabled = Boolean(spec);
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
    };
    if (presets[type.value]) provider.preset = type.value;
    else {
      provider.protocol = protocol.value;
      provider.base_url = endpoint.value.trim();
      if (scheme.value !== "native") provider.auth_scheme = scheme.value;
      if (header.value.trim()) provider.session_header = header.value.trim();
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
        };
      }
      protocol.value = custom.protocol;
      endpoint.value = custom.url || before;
      scheme.value = custom.scheme;
      header.value = custom.header;
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
  for (const control of [protocol, endpoint, scheme, header, enabled])
    control.addEventListener("input", notify);
  refresh();
  return { read, valid, element: box };
}
function renderProviderEditor(field, name) {
  const current = currentValue(field);
  const body = $("entry-body");
  body.replaceChildren();
  if (current.account) {
    activeEditor.readConnection = () => {
      const value = currentValue(field);
      return {
        provider: { account: value.account, enabled: value.enabled === true },
      };
    };
    activeEditor.commit = () => {
      const value = activeEditor.readConnection().provider;
      if (JSON.stringify(value) !== JSON.stringify(currentValue(field)))
        changed(field.path, "set", value);
    };
    const copy = structuredClone(field);
    copy.schema.properties = Object.fromEntries(
      Object.entries(copy.schema.properties).filter(([k]) =>
        ["account", "enabled"].includes(k),
      ),
    );
    body.append(
      drawField(copy),
      node(
        "p",
        "此连接使用原生账号认证，不需要填写 API 地址或网关鉴权参数。",
        "inline-note",
      ),
    );
    body.append(
      button("编辑账号认证", () => {
        finishEntity();
        if (!activeEditor) openEntity("accounts", current.account);
      }),
    );
  } else {
    let previousSecret = providerKeyName(current);
    const editor = apiConnectionFields(body, current, {
      onChange(value) {
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
    activeEditor.readConnection = editor.read;
    activeEditor.validate = editor.valid;
    activeEditor.commit = () => {
      const value = editor.read();
      if (
        JSON.stringify(value.provider) !==
        JSON.stringify(valueAt(["model_api", "providers"], {})[name])
      )
        changed(field.path, "set", value.provider);
    };
  }
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
