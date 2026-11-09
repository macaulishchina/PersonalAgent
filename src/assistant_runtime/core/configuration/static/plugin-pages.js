/* Plugin page SDK host, version 1. Identity comes from the mounted frame, never its payload. */
const pluginPages = (() => {
  let active = null,
    notification = false;
  const busy = new Set();
  let issues = [];
  const prefixOf = (owner) => owner.configuration.namespace.split(".");
  const contains = (prefix, path) =>
    prefix.every((part, i) => path[i] === part);
  function safePath(path) {
    if (
      !Array.isArray(path) ||
      path.length > 20 ||
      path.some(
        (p) =>
          typeof p !== "string" ||
          !p ||
          ["__proto__", "constructor", "prototype"].includes(p),
      )
    )
      throw new Error("无效字段路径");
    return path;
  }
  function ruleAt(rule, path) {
    let locked = rule.readOnly;
    for (const part of path) {
      rule = rule.properties?.[part] ?? rule.additionalProperties;
      if (!rule || typeof rule !== "object") throw new Error("字段未声明");
      locked ||= rule.readOnly;
    }
    return { ...rule, readOnly: locked };
  }
  function hasSecret(rule) {
    return !!(
      rule?.writeOnly ||
      rule?.["x-sensitive"] ||
      Object.values(rule?.properties || {}).some(hasSecret) ||
      (typeof rule?.additionalProperties === "object" &&
        hasSecret(rule.additionalProperties)) ||
      (rule?.items && hasSecret(rule.items))
    );
  }
  function redact(value, rule) {
    if (rule?.writeOnly || rule?.["x-sensitive"])
      return {
        configured:
          value !== undefined &&
          value !== "" &&
          value !== null &&
          value?.configured !== false,
      };
    if (Array.isArray(value)) return value.map((v) => redact(v, rule?.items));
    if (value && typeof value === "object")
      return Object.fromEntries(
        Object.entries(value).map(([k, v]) => [
          k,
          redact(v, rule?.properties?.[k] ?? rule?.additionalProperties),
        ]),
      );
    return value;
  }
  function state(owner) {
    const prefix = prefixOf(owner),
      spec = owner.configuration;
    const fields = snapshot.fields
      .filter((f) => contains(prefix, f.path))
      .map((f) => ({
        ...f,
        path: f.path.slice(prefix.length),
        value: redact(currentValue(f), f.schema),
      }));
    const resources = resourceInventory
      .filter((r) => r.id.startsWith(owner.id + ":"))
      .map((r) => {
        const edit = resourceEdits.get(r.id);
        return {
          ...r,
          id: r.id.slice(owner.id.length + 1),
          value: r.sensitive ? undefined : (edit?.value ?? r.value),
          configured: edit
            ? edit.op !== "clear_secret" && edit.op !== "delete"
            : r.configured,
          modified: !!edit,
        };
      });
    return {
      version: snapshot.version,
      loadedVersion: snapshot.loaded_version,
      owner: owner.id,
      schema: spec.schema,
      fields,
      resources,
      actions: spec.interface?.actions || {},
      application: snapshot.application,
      pendingRestart: !!pendingRestart,
      dirty:
        [...edits.values()].some((e) => contains(prefix, e.path)) ||
        resources.some((r) => r.modified),
      saving: saving || configurationBusy,
      errors: issues
        .filter((e) => contains(prefix, e.path))
        .map((e) => ({ ...e, path: e.path.slice(prefix.length) })),
    };
  }
  function notify() {
    if (notification) return;
    notification = true;
    queueMicrotask(() => {
      notification = false;
      if (active?.port && !active.closed)
        active.port.postMessage({ type: "state", state: state(active.owner) });
    });
  }
  function dispose() {
    if (!active) return;
    active.closed = true;
    active.port?.close();
    window.removeEventListener("message", active.handshake);
    clearTimeout(active.timeout);
    active = null;
  }
  function mount(owner, target) {
    dispose();
    const page = owner.configuration.interface.page;
    const frame = document.createElement("iframe");
    frame.title = page.title || owner.configuration.title || owner.id;
    frame.className = "plugin-management-frame";
    frame.setAttribute("sandbox", "allow-scripts allow-same-origin");
    frame.referrerPolicy = "same-origin";
    frame.src = `/api/plugin-pages/${encodeURIComponent(owner.id)}/assets/${page.entry.split("/").map(encodeURIComponent).join("/")}`;
    const status = node("p", "正在加载插件管理页面…", "hint");
    const session = { owner, frame, closed: false, port: null };
    active = session;
    const live = () => !session.closed && active === session;
    session.handshake = (event) => {
      if (
        !live() ||
        event.source !== frame.contentWindow ||
        event.origin !== location.origin ||
        event.data?.type !== "pa-plugin-ready" ||
        session.port
      )
        return;
      if (event.data.version !== 1) {
        status.textContent = "插件页面接口版本不支持，请更新插件";
        return;
      }
      const channel = new MessageChannel();
      session.port = channel.port1;
      let tail = Promise.resolve();
      channel.port1.onmessage = ({ data }) => {
        // Serialize edits to preserve the user's order; unmount invalidates queued work.
        const handle = async () => {
          if (!live() || !data || typeof data.id !== "string") return;
          try {
            if (JSON.stringify(data).length > 1024 * 1024)
              throw new Error("消息过大");
            const result = await dispatch(session, data, live);
            if (live()) channel.port1.postMessage({ id: data.id, result });
          } catch (error) {
            if (live())
              channel.port1.postMessage({
                id: data.id,
                error: {
                  message: error.message,
                  issues: error.issues || [],
                  code: error.code || "request_failed",
                },
              });
          }
        };
        if (data?.method === "action") void handle();
        else tail = tail.then(handle);
      };
      frame.contentWindow.postMessage(
        { type: "pa-plugin-connect", version: 1 },
        location.origin,
        [channel.port2],
      );
      status.hidden = true;
      clearTimeout(session.timeout);
      notify();
    };
    window.addEventListener("message", session.handshake);
    session.timeout = setTimeout(() => {
      if (live())
        status.textContent =
          "页面未就绪：请检查插件资源、SDK 版本或重新打开页面";
    }, 12000);
    target.append(status, frame);
  }
  async function dispatch(session, message, live) {
    const owner = session.owner,
      args = message.arguments || {},
      prefix = prefixOf(owner);
    if (message.method === "state") return state(owner);
    if (message.method === "resize") {
      session.frame.style.height = `${Math.max(360, Math.min(2400, Number(args.height) || 600))}px`;
      return null;
    }
    if (message.method === "patch") {
      if (saving || configurationBusy) throw new Error("正在保存，请稍后编辑");
      const path = safePath(args.path);
      const rule = ruleAt(owner.configuration.schema, path);
      if (rule.readOnly) throw new Error("此字段需要本机维护");
      if (
        ![
          "set",
          "default",
          "create",
          "replace_secret",
          "clear_secret",
        ].includes(args.op)
      )
        throw new Error("无效编辑操作");
      const secret = rule.writeOnly || rule["x-sensitive"];
      if (
        !(
          secret
            ? ["replace_secret", "clear_secret"]
            : ["set", "default", "create"]
        ).includes(args.op)
      )
        throw new Error("敏感字段需要独立操作");
      if (args.op === "set" && hasSecret(rule))
        throw new Error("请分别修改包含凭据的子字段");
      if (!live()) throw new Error("页面已关闭");
      issues = issues.filter((e) => !contains([...prefix, ...path], e.path));
      changed([...prefix, ...path], args.op, args.value);
      return { ok: true };
    }
    if (message.method === "resource") {
      if (saving || configurationBusy) throw new Error("正在保存，请稍后编辑");
      const r = resourceInventory.find(
        (r) => r.id === owner.id + ":" + args.id,
      );
      if (!r || r.read_only) throw new Error("资源未声明或只读");
      if (
        !(r.sensitive ? ["replace_secret", "clear_secret"] : ["set"]).includes(
          args.op,
        )
      )
        throw new Error("无效资源操作");
      resourceEdits.set(r.id, {
        id: r.id,
        op: args.op,
        ...(Object.hasOwn(args, "value") ? { value: args.value } : {}),
      });
      updateSaveBar();
      return { ok: true };
    }
    if (message.method === "action") {
      const operation = owner.configuration.interface?.actions?.[args.name];
      if (!operation) throw new Error("管理操作未声明");
      const key = owner.id + ":" + args.name;
      if (busy.has(key)) throw new Error("操作仍在进行，请勿重复提交");
      const write = operation.read_only !== true;
      if (
        write &&
        (state(owner).dirty ||
          snapshot.version !== snapshot.loaded_version ||
          pendingRestart ||
          saving ||
          configurationBusy)
      )
        throw new Error("请先保存并应用相关配置");
      if (
        write &&
        !confirm(
          operation.confirmation ||
            operation.description ||
            "确认执行此操作？这会立即影响插件运行状态。",
        )
      )
        throw new Error("已取消操作");
      busy.add(key);
      try {
        return await api(`/api/plugin-pages/${owner.id}/action`, {
          version: snapshot.version,
          action: args.name,
          arguments: args.arguments || {},
          confirmed: write,
          request_id: args.requestId,
        });
      } finally {
        busy.delete(key);
      }
    }
    throw new Error("不支持的页面接口");
  }
  return {
    mount,
    dispose,
    notify,
    errors: (values) => {
      issues = values;
      notify();
    },
  };
})();
