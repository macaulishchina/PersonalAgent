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

function renderModelEditor(field) {
  const root = $("entry-body"),
    path = field.path;
  const current = () =>
    valueAt(["model_api", "entries"], {})[path.at(-1)] || {};
  const initial = current();
  const copy = structuredClone(field);
  copy.value = initial;
  const props = copy.schema.properties;
  delete props.capabilities;
  delete props.execution_validated;
  props.provider.title = "使用连接";
  props.model.title = "供应商模型 ID";
  props.default_reasoning_effort.title = "思考模式";
  const levels =
    initial.reasoning_efforts ?? initial.capabilities?.reasoning_efforts ?? [];
  const choices = ["auto", ...levels];
  const selected = initial.default_reasoning_effort || "auto";
  if (!choices.includes(selected)) choices.push(selected);
  props.default_reasoning_effort.enum = [...new Set(choices)];
  props.default_reasoning_effort.default = "auto";
  props.default_reasoning_effort.description =
    "自动遵循当前适配器的默认策略；不会强行发送 medium。显式档位仅在已知支持时提供。";
  for (const [name, definition] of Object.entries(props))
    definition["x-advanced"] = ![
      "provider",
      "model",
      "default_reasoning_effort",
    ].includes(name);
  props.reasoning_efforts.title = "覆盖 · 可用思考档位";
  props.reasoning_efforts.description =
    "仅在明确了解当前连接支持范围时覆盖；不会为未实现的协议增加能力。留空表示仅使用自动。";
  for (const name of ["images", "structured", "context_window"])
    props[name].title = "覆盖 · " + props[name].title;
  root.replaceChildren(drawField(copy));
  const summary = node("section", undefined, "capability-summary");
  summary.setAttribute("aria-label", "自动识别的模型能力");
  summary.append(node("h3", "模型能力"));
  const cap = initial.capabilities || {};
  const rows = node("dl", undefined, "capability-facts");
  const truth = (value) =>
    value === true ? "支持" : value === false ? "不支持" : "未知";
  for (const [label, key, format] of [
    [
      "上下文窗口",
      "context_window",
      (v) => (v ? v.toLocaleString() + " tokens" : "未知"),
    ],
    [
      "模型最大输出",
      "output_limit",
      (v) => (v ? v.toLocaleString() + " tokens" : "未知"),
    ],
    ["图片输入", "images", truth],
    ["结构化输出", "structured", truth],
    ["工具调用", "tools", truth],
  ]) {
    const manual = Object.hasOwn(initial, key);
    rows.append(
      node("dt", label),
      node(
        "dd",
        format(manual ? initial[key] : cap[key]) +
          (manual ? " · 手动覆盖" : ""),
      ),
    );
  }
  summary.append(rows);
  const efforts = cap.reasoning_efforts;
  summary.append(
    node(
      "p",
      efforts?.length
        ? "适配器支持的思考选项：" +
            efforts.map((e) => optionLabel({}, e, ["reasoning"])).join("、")
        : cap.reasoning === false
          ? "此模型未声明可调思考能力，使用自动即可。"
          : "尚无此连接的可靠思考档位映射，默认不指定思考参数。模型声明支持推理，不等于支持所有档位。",
      "hint",
    ),
  );
  summary.append(
    node(
      "p",
      cap.source && cap.source !== "unknown"
        ? `资料来源：${cap.source} · ${cap.reference || initial.model} · ${cap.revision.slice(0, 12)}。能力资料不代表实际验收。`
        : "未找到准确的能力资料。可使用高级覆盖或关联参考模型；不会按名称猜测。",
      "hint",
    ),
  );
  summary.append(
    node(
      "p",
      initial.execution_validated
        ? "执行验收：已有人工确认记录（保留原配置）。"
        : "执行验收：尚未确认。请使用部署指南中的有界模型验收流程；查询目录不会执行工具或消耗推理额度。",
      "hint",
    ),
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
    load.disabled = true;
    status.textContent = "正在读取模型资料，不发送推理请求…";
    const identity = [current().provider, current().model];
    try {
      const value = await describeModel(...identity, reference, observed);
      if (
        !root.contains(summary) ||
        JSON.stringify(identity) !==
          JSON.stringify([current().provider, current().model])
      )
        return;
      changed([...path, "capabilities"], "set", value);
      renderEntityEditor();
      updateSaveBar();
    } catch (error) {
      if (root.contains(summary)) {
        status.textContent = error.message;
        status.dataset.state = "error";
      }
    } finally {
      busy = false;
      load.disabled = false;
    }
  }
  const load = button(
    cap.source ? "刷新能力资料" : "自动识别能力",
    () => refresh(),
    "secondary",
  );
  const reset = button(
    "恢复自动能力",
    () => {
      for (const name of [
        "images",
        "structured",
        "context_window",
        "reasoning_efforts",
        "default_reasoning_effort",
      ])
        changed([...path, name], "default");
      changed([...path, "default_reasoning_effort"], "set", "auto");
      renderEntityEditor();
      updateSaveBar();
    },
    "subtle",
  );
  const actions = node("div", undefined, "wizard-actions");
  actions.append(load, reset);
  summary.append(actions, status);
  const reference = node("details", undefined, "advanced-fields");
  reference.append(node("summary", "高级 · 中转或私有部署的参考模型"));
  reference.append(
    node(
      "p",
      "仅借用公开规格，不表示中转已验证。格式：供应商/模型 ID；部署限制可在高级设置中覆盖。",
      "hint",
    ),
  );
  const ref = formInput(reference, "capability-reference", "参考模型", {
    placeholder: "例如 openai/gpt-…",
    value: cap.reference || "",
  });
  reference.append(
    button("读取参考资料", () => refresh(ref.value.trim()), "secondary"),
  );
  summary.append(reference);
  const overrides = root.querySelector(".advanced-fields");
  if (overrides) overrides.before(summary);
  else root.append(summary);
  root.addEventListener(
    "change",
    (event) => {
      const changedPath =
        event.target.closest("[data-field-path]")?.dataset.fieldPath;
      if (
        ["provider", "model"].some(
          (name) => changedPath === key([...path, name]),
        )
      ) {
        changed([...path, "capabilities"], "default");
        changed([...path, "execution_validated"], "set", false);
        status.textContent =
          "模型或连接已更换，请重新识别能力；旧的执行验收不再沿用。";
      }
      if (changedPath === key([...path, "reasoning_efforts"]))
        renderEntityEditor();
    },
    { signal: editorInputController.signal },
  );
  activeEditor.validate = () => {
    const value = current(),
      supported =
        value.reasoning_efforts ?? value.capabilities?.reasoning_efforts ?? [];
    const effort = value.default_reasoning_effort || "auto";
    if (effort !== "auto" && !supported.includes(effort)) {
      status.textContent = "当前思考档位不受支持，请改为自动或选择支持的档位。";
      return false;
    }
    return true;
  };
}
