import React, { useState, useEffect } from "react";
import { createRoot } from "react-dom/client";
import {
  connect,
  mockClient,
  type Client,
  type State,
  type Field,
} from "@personal-agent/plugin-ui";
import { AutoField, useConfiguration } from "@personal-agent/plugin-ui/react";
import "@personal-agent/plugin-ui/theme.css";
import declaration from "../../src/pa_configuration_demo/configuration.json";

function App({ client }: { client: Client }) {
  const { state, error } = useConfiguration(client);
  const [notice, setNotice] = useState(""),
    [target, setTarget] = useState(""),
    [step, setStep] = useState(0),
    [name, setName] = useState("演示设备"),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    const observer = new ResizeObserver(() => {
      void client.resize(document.body.scrollHeight).catch(() => {});
    });
    observer.observe(document.body);
    return () => observer.disconnect();
  }, [client]);
  if (!state) return <p role="status">{error || "正在读取插件配置…"}</p>;
  const mode = state.fields.find((f) => f.path[0] === "mode")?.value;
  const targets = (state.fields.find((f) => f.path[0] === "targets")?.value ||
    {}) as Record<string, { name: string; priority: number }>;
  const run = async (fn: () => Promise<unknown>) => {
    setNotice("");
    try {
      await fn();
    } catch (e) {
      setNotice((e as Error).message);
    }
  };
  const operation = async (action: string, args = {}) => {
    if (busy) return;
    setBusy(true);
    try {
      await run(async () => {
        const result = await client.action(action, args);
        setNotice(
          result.state === "completed"
            ? JSON.stringify(result.result)
            : result.message || result.state,
        );
      });
    } finally {
      setBusy(false);
    }
  };
  return (
    <main>
      <section className="pa-panel">
        <div className="pa-toolbar">
          <h2>基础配置</h2>
          <small>{state.dirty ? "有未保存的修改" : "配置已载入"}</small>
        </div>
        <p>
          本页由插件独立构建。所有编辑进入外层统一保存栏，切换页面会保留草稿。
        </p>
        {state.fields
          .filter(
            (f) =>
              f.path[0] !== "targets" &&
              (f.path[0] !== "endpoint" || mode === "remote"),
          )
          .map((f) => (
            <AutoField
              key={f.path.join(".") + state.version}
              field={f}
              client={client}
            />
          ))}
        <button
          onClick={() =>
            void run(() =>
              client.patch({ path: ["secret"], op: "clear_secret" }),
            )
          }
        >
          清除私有口令
        </button>
      </section>
      <section className="pa-panel">
        <h2>命名目标</h2>
        <div className="pa-toolbar">
          <input
            aria-label="目标标识"
            placeholder="例如 desktop"
            value={target}
            onChange={(e) => setTarget(e.target.value)}
          />
          <button
            onClick={() =>
              void run(async () => {
                await client.patch({
                  path: ["targets", target],
                  op: "set",
                  value: { name: target, priority: 1 },
                });
                setTarget("");
              })
            }
          >
            添加目标
          </button>
        </div>
        {Object.entries(targets).map(([id, value]) => (
          <div className="pa-field" key={id}>
            <strong>{id}</strong>
            <input
              aria-label={id + " 名称"}
              defaultValue={value.name}
              onChange={(e) =>
                void run(() =>
                  client.patch({
                    path: ["targets", id, "name"],
                    op: "set",
                    value: e.target.value,
                  }),
                )
              }
            />
            <button
              onClick={() =>
                void run(() =>
                  client.patch({ path: ["targets", id], op: "default" }),
                )
              }
            >
              移除
            </button>
          </div>
        ))}
      </section>
      <section className="pa-panel">
        <h2>配置资源</h2>
        {state.resources.map((r) => (
          <label className="pa-field" key={r.id}>
            <span>{r.title}</span>
            {r.sensitive ? (
              <input
                aria-label={r.title}
                type="password"
                placeholder={r.configured ? "已配置，留空保持" : "未配置"}
                onChange={(e) => {
                  const v = e.target.value;
                  if (v)
                    void run(() => client.resource(r.id, "replace_secret", v));
                }}
                onBlur={(e) => {
                  e.target.value = "";
                }}
              />
            ) : (
              <textarea
                aria-label={r.title}
                defaultValue={r.value || ""}
                onChange={(e) =>
                  void run(() => client.resource(r.id, "set", e.target.value))
                }
              />
            )}
            <small>{r.modified ? "待保存" : r.kind}</small>
            {r.sensitive && (
              <button
                type="button"
                onClick={() =>
                  void run(() => client.resource(r.id, "clear_secret"))
                }
              >
                清除{r.title}
              </button>
            )}
          </label>
        ))}
      </section>
      <section className="pa-panel">
        <h2>登记向导 · {step + 1} / 2</h2>
        {step === 0 ? (
          <div className="pa-toolbar">
            <input
              aria-label="设备名称"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
            <button onClick={() => setStep(1)}>下一步</button>
          </div>
        ) : (
          <div className="pa-toolbar">
            <span>登记 {name}</span>
            <button onClick={() => setStep(0)}>上一步</button>
            <button
              disabled={busy || state.dirty}
              onClick={() => void operation("register", { name })}
            >
              确认登记
            </button>
          </div>
        )}
        <p>
          即时操作使用已生效配置。修改设置后先使用外层保存栏；未启动插件时仍可编辑配置。
        </p>
        <button disabled={busy} onClick={() => void operation("status")}>
          查询运行状态
        </button>
      </section>
      {(notice || state.errors.length > 0) && (
        <p role="status">
          {state.errors.length
            ? state.errors.map((e) => e.message).join("；")
            : notice}
        </p>
      )}
    </main>
  );
}
const schema = declaration.schema as State["schema"];
const mock: State = {
  owner: "configuration_demo",
  version: "preview",
  loadedVersion: "preview",
  schema,
  fields: Object.entries(schema.properties || {}).map(
    ([name, rule]) =>
      ({
        path: [name],
        schema: rule,
        value: rule.writeOnly ? { configured: false } : rule.default,
        source: "default",
        explicit: false,
        files: [],
      }) as Field,
  ),
  resources: declaration.resources.map((r) => ({
    ...r,
    sensitive: r.kind === "credential",
    value: r.kind === "json" ? "{}" : "",
    configured: false,
  })),
  actions: declaration.interface.actions as State["actions"],
  dirty: false,
  saving: false,
  errors: [],
};
const connection =
  window.parent === window
    ? Promise.resolve(
        mockClient(mock, {
          status: () => ({ label: "独立开发预览", registered: false }),
          register: () => ({ registered: true }),
        }),
      )
    : connect();
connection
  .then((client) =>
    createRoot(document.getElementById("root")!).render(
      <App client={client} />,
    ),
  )
  .catch((e) => {
    document.getElementById("root")!.textContent = e.message;
  });
