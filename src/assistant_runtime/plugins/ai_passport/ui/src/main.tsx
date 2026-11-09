import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { createRoot } from "react-dom/client";
import {
  connect,
  type Client,
  type Field,
  type Schema,
  type State,
} from "@personal-agent/plugin-ui";
import { useConfiguration } from "@personal-agent/plugin-ui/react";
import "@personal-agent/plugin-ui/theme.css";
import "./style.css";

type Receipt = {
  desired: number;
  applied: number;
  actual: number | boolean | string | null;
};
type Device = {
  device_id: string;
  session_id: string;
  name: string;
  connected: boolean;
  configuration: Record<string, Receipt>;
};
type Network = { ssid: string; password?: { configured: boolean } };
type Overrides = Record<string, unknown> & {
  wifi_networks?: Record<string, Network>;
};

const groups = [
  ["播报", ["speech_mode", "volume", "voice", "rate"]],
  ["屏幕与缓存", ["brightness", "text_size", "show_images", "offline_cache"]],
  ["输入与休眠", ["input_mode", "keep_online", "sleep_seconds"]],
  ["连接", ["server_url", "trusted_ca", "max_connections"]],
] as const;

function Panel({
  title,
  description,
  actions,
  children,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="pa-panel">
      <header className="panel-heading">
        <div>
          <h2>{title}</h2>
          {description && <p className="muted">{description}</p>}
        </div>
        {actions && <div className="actions">{actions}</div>}
      </header>
      {children}
    </section>
  );
}

function Setting({
  field,
  value,
  explicit,
  saving,
  edit,
  reset,
  inherited = false,
}: {
  field: Pick<Field, "path" | "schema">;
  value: unknown;
  explicit: boolean;
  saving: boolean;
  edit: (value: unknown) => void;
  reset: () => void;
  inherited?: boolean;
}) {
  const rule = field.schema;
  const title = rule.title || field.path.at(-1);
  const labels = rule["x-labels"] as Record<string, string> | undefined;
  const numeric = rule.type === "number" || rule.type === "integer";
  const [numberText, setNumberText] = useState(String(value ?? ""));
  useEffect(() => setNumberText(String(value ?? "")), [value]);
  return (
    <div className="setting">
      <div>
        <strong>{title}</strong>
        {rule.description && <p className="muted">{rule.description}</p>}
        {inherited && (
          <small className="muted">
            {explicit ? "专属设置" : "沿用默认设置"}
          </small>
        )}
      </div>
      <div className="setting-control">
        {rule.enum ? (
          <select
            aria-label={title}
            disabled={saving}
            value={String(value ?? "")}
            onChange={(e) =>
              edit(rule.enum!.find((v) => String(v) === e.target.value))
            }
          >
            {value === undefined && (
              <option value="" disabled>
                请选择
              </option>
            )}
            {rule.enum.map((v) => (
              <option key={String(v)} value={String(v)}>
                {labels?.[String(v)] || String(v)}
              </option>
            ))}
          </select>
        ) : rule.type === "boolean" ? (
          <input
            aria-label={title}
            disabled={saving}
            type="checkbox"
            checked={Boolean(value)}
            onChange={(e) => edit(e.target.checked)}
          />
        ) : rule["x-advanced"] ? (
          <textarea
            aria-label={title}
            disabled={saving}
            rows={3}
            value={String(value ?? "")}
            onChange={(e) => edit(e.target.value)}
          />
        ) : (
          <input
            aria-label={title}
            disabled={saving}
            type={numeric ? "number" : "text"}
            step={rule.type === "integer" ? 1 : "any"}
            min={numeric ? (rule.minimum as number) : undefined}
            max={numeric ? (rule.maximum as number) : undefined}
            value={numeric ? numberText : String(value ?? "")}
            onBlur={() => {
              if (numeric && numberText === "")
                setNumberText(String(value ?? ""));
            }}
            onChange={(e) => {
              const text = e.target.value;
              if (!numeric) {
                edit(text);
                return;
              }
              setNumberText(text);
              if (text !== "") edit(Number(text));
            }}
          />
        )}
        {explicit && (
          <button
            type="button"
            className="quiet"
            disabled={saving}
            onClick={reset}
          >
            {inherited ? "沿用默认" : "恢复默认"}
          </button>
        )}
      </div>
    </div>
  );
}

function Wifi({
  networks,
  saving,
  edit,
  password,
  clearPassword,
  remove,
}: {
  networks: Record<string, Network>;
  saving: boolean;
  edit: (slot: string, ssid: string) => void;
  password: (slot: string, value: string) => void;
  clearPassword: (slot: string) => void;
  remove: (slot: string) => void;
}) {
  const [adding, setAdding] = useState(false);
  const free = ["1", "2", "3", "4", "5"].filter(
    (slot) => !Object.hasOwn(networks, slot),
  );
  return (
    <Panel title="Wi-Fi 连接顺序">
      <p className="muted">
        设备从优先级 1 开始尝试。密码未设置时按开放网络连接。
      </p>
      {Object.entries(networks).map(([slot, network]) => (
        <div className="network" key={slot}>
          <strong className="priority">{slot}</strong>
          <label>
            Wi-Fi 名称
            <input
              value={network.ssid}
              maxLength={32}
              disabled={saving}
              onChange={(e) => edit(slot, e.target.value)}
            />
          </label>
          <label>
            Wi-Fi 密码
            <input
              type="password"
              autoComplete="off"
              maxLength={64}
              disabled={saving}
              placeholder={
                network.password?.configured
                  ? "已设置 · 输入新密码可替换"
                  : "未设置"
              }
              onChange={(e) => {
                if (e.target.value) password(slot, e.target.value);
                else clearPassword(slot);
              }}
              onBlur={(e) => {
                e.target.value = "";
              }}
            />
          </label>
          <div className="network-actions">
            {network.password?.configured && (
              <button
                type="button"
                className="quiet"
                disabled={saving}
                onClick={() => clearPassword(slot)}
              >
                清除密码
              </button>
            )}
            <button
              type="button"
              className="quiet danger"
              disabled={saving}
              onClick={() => remove(slot)}
            >
              移除
            </button>
          </div>
        </div>
      ))}
      {adding && free.length > 0 ? (
        <form
          className="actions"
          onSubmit={(e) => {
            e.preventDefault();
            const data = new FormData(e.currentTarget);
            edit(String(data.get("priority")), String(data.get("ssid")));
            setAdding(false);
          }}
        >
          <label>
            优先级{" "}
            <select name="priority">
              {free.map((slot) => (
                <option key={slot}>{slot}</option>
              ))}
            </select>
          </label>
          <label>
            新 Wi-Fi 名称{" "}
            <input autoFocus name="ssid" required maxLength={32} />
          </label>
          <button type="submit" disabled={saving}>
            添加
          </button>
          <button
            type="button"
            className="quiet"
            onClick={() => setAdding(false)}
          >
            取消
          </button>
        </form>
      ) : (
        <button
          type="button"
          disabled={saving || !free.length}
          onClick={() => setAdding(true)}
        >
          添加 Wi-Fi
        </button>
      )}
    </Panel>
  );
}

function DeviceStatus({ device }: { device: Device }) {
  const revisions = Object.values(device.configuration);
  const pending = revisions.filter(
    (value) => value.desired !== value.applied,
  ).length;
  return (
    <>
      <span className={device.connected ? "badge online" : "badge"}>
        {device.connected ? "在线" : "离线"}
      </span>
      <span className="muted">
        {pending
          ? `${pending} 项待设备确认`
          : revisions.length
            ? "配置已同步"
            : "尚未同步配置"}
      </span>
    </>
  );
}

function Passport({ client }: { client: Client }) {
  const { state, error } = useConfiguration(client);
  const [tab, setTab] = useState<"devices" | "defaults">("devices");
  const [selected, setSelected] = useState<Device>();
  const [devices, setDevices] = useState<Device[]>([]);
  const [pairing, setPairing] = useState(false);
  const [notice, setNotice] = useState("");
  const [readError, setReadError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [reading, setReading] = useState(false);
  const [writing, setWriting] = useState(false);
  const root = useRef<HTMLElement>(null);
  const inFlight = useRef(false);
  useEffect(() => {
    if (!root.current) return;
    const observer = new ResizeObserver(([entry]) => {
      void client
        .resize(Math.ceil(entry.contentRect.height) + 12)
        .catch(() => {});
    });
    observer.observe(root.current);
    return () => observer.disconnect();
  }, [client, !!state]);
  useEffect(() => {
    if (!state?.enabled || tab !== "devices") return;
    let alive = true;
    setReading(true);
    setReadError("");
    void client
      .action<{ devices: Device[] }>("list")
      .then((reply) => {
        if (!alive) return;
        if (reply.state !== "completed" || !reply.result)
          throw new Error(reply.message || "无法读取设备列表");
        setDevices(reply.result.devices);
      })
      .catch((e) => {
        if (alive) setReadError(e.message);
      })
      .finally(() => {
        if (alive) setReading(false);
      });
    return () => {
      alive = false;
    };
  }, [client, state?.enabled, state?.loadedVersion, refresh, tab]);

  if (!state) return <p role="status">{error || "正在读取插件配置…"}</p>;
  const defaults = new Map(state.fields.map((field) => [field.path[0], field]));
  const overridesField = defaults.get("devices");
  const device =
    devices.find((d) => d.device_id === selected?.device_id) || selected;
  const values = device
    ? (overridesField?.value as Record<string, Overrides> | undefined)?.[
        device.device_id
      ] || {}
    : {};
  const networks = values.wifi_networks || {};
  const overrideRules =
    (overridesField?.schema.additionalProperties as Schema | undefined)
      ?.properties || {};
  const canOperate =
    state.enabled &&
    state.loadedVersion === state.version &&
    !state.pendingRestart &&
    !state.dirty &&
    !state.saving;
  const patch = (
    path: string[],
    op: "set" | "default" | "replace_secret" | "clear_secret",
    value?: unknown,
  ) => {
    setNotice("");
    void client.patch({ path, op, value }).catch((e) => setNotice(e.message));
  };
  const devicePath = device ? ["devices", device.device_id] : [];
  async function write(name: "pair" | "revoke", args: Record<string, string>) {
    if (!canOperate || inFlight.current) return;
    inFlight.current = true;
    setWriting(true);
    setNotice("");
    try {
      const result = await client.action(name, args);
      if (result.state !== "completed") {
        setNotice(result.message || "操作结果待确认，请刷新设备列表");
        setRefresh((n) => n + 1);
        return;
      }
      setNotice(
        name === "pair" ? "配对成功，设备会自动连接。" : "已撤销配对。",
      );
      setPairing(false);
      if (name === "revoke") setSelected(undefined);
      setRefresh((n) => n + 1);
    } catch (e) {
      setNotice((e as Error).message);
    } finally {
      inFlight.current = false;
      setWriting(false);
    }
  }
  function pair(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    void write("pair", {
      name: String(data.get("name")).trim(),
      code: String(data.get("code")),
    });
  }
  function settings(state: State, isDevice: boolean) {
    return groups.map(([title, names]) => {
      const rows = names.flatMap((name) => {
        const fallback = defaults.get(name);
        if ((isDevice && !overrideRules[name]) || (!isDevice && !fallback))
          return [];
        const path = isDevice ? [...devicePath, name] : [name];
        const field = isDevice
          ? { ...fallback, path, schema: overrideRules[name] }
          : fallback!;
        const explicit = isDevice
          ? Object.hasOwn(values, name)
          : Boolean(field.explicit || field.modified);
        return [
          <Setting
            key={name}
            field={field}
            value={isDevice ? (values[name] ?? fallback?.value) : field.value}
            explicit={explicit}
            inherited={isDevice}
            saving={state.saving}
            edit={(value) => patch(path, "set", value)}
            reset={() => patch(path, "default")}
          />,
        ];
      });
      return rows.length ? (
        <Panel key={title} title={title}>
          {rows}
        </Panel>
      ) : null;
    });
  }
  return (
    <main ref={root}>
      <div className="tabs" role="group" aria-label="AI Passport 设置范围">
        <button
          type="button"
          aria-pressed={tab === "devices"}
          onClick={() => setTab("devices")}
        >
          设备
        </button>
        <button
          type="button"
          aria-pressed={tab === "defaults"}
          onClick={() => setTab("defaults")}
        >
          默认设置
        </button>
      </div>
      {!state.enabled && (
        <p className="muted">AI Passport 未启用。可以先编辑默认设置。</p>
      )}
      {state.errors.length > 0 && (
        <ul className="error" role="alert">
          {state.errors.map((issue) => (
            <li key={JSON.stringify(issue.path)}>
              {Array.isArray(issue.path) && issue.path.join(" / ")}{" "}
              {issue.message}
            </li>
          ))}
        </ul>
      )}
      {notice && (
        <p role="status" className="notice">
          {notice}
        </p>
      )}
      {tab === "defaults" ? (
        <>
          <p className="view-description muted">
            适用于所有设备，单台设备的专属设置优先。
          </p>
          {settings(state, false)}
        </>
      ) : device ? (
        <>
          <button
            type="button"
            className="quiet"
            onClick={() => setSelected(undefined)}
          >
            ← 返回设备
          </button>
          <Panel title={device.name}>
            <div className="actions">
              <DeviceStatus device={device} />
              <button
                type="button"
                className="quiet"
                disabled={reading}
                onClick={() => setRefresh((n) => n + 1)}
              >
                刷新状态
              </button>
              <button
                type="button"
                className="quiet danger"
                disabled={!canOperate || writing}
                onClick={() =>
                  void write("revoke", { device_id: device.device_id })
                }
              >
                撤销配对
              </button>
            </div>
            {readError && <p className="error">{readError}</p>}
            <details>
              <summary>设备标识</summary>
              <p>{device.device_id}</p>
              <p>会话 {device.session_id}</p>
            </details>
          </Panel>
          {settings(state, true)}
          <Wifi
            key={device.device_id}
            networks={networks}
            saving={state.saving}
            edit={(slot, ssid) =>
              patch([...devicePath, "wifi_networks", slot, "ssid"], "set", ssid)
            }
            password={(slot, value) =>
              patch(
                [...devicePath, "wifi_networks", slot, "password"],
                "replace_secret",
                value,
              )
            }
            clearPassword={(slot) =>
              patch(
                [...devicePath, "wifi_networks", slot, "password"],
                "clear_secret",
              )
            }
            remove={(slot) =>
              patch(
                [
                  ...devicePath,
                  "wifi_networks",
                  ...(Object.keys(networks).length === 1 ? [] : [slot]),
                ],
                "default",
              )
            }
          />
        </>
      ) : (
        <Panel
          title="已配对设备"
          description="配对新设备，或查看连接状态与设备专属设置。"
          actions={
            <>
              <button
                type="button"
                className="quiet"
                disabled={!state.enabled || reading || writing}
                onClick={() => setRefresh((n) => n + 1)}
              >
                刷新
              </button>
              <button
                type="button"
                className="primary"
                disabled={!canOperate || writing}
                onClick={() => setPairing((v) => !v)}
              >
                配对新设备
              </button>
            </>
          }
        >
          {state.enabled && !canOperate && (
            <p className="muted">配置生效后可进行配对和撤销。</p>
          )}
          {pairing && (
            <form className="pair-form" onSubmit={pair}>
              <h3>配对新设备</h3>
              <p className="muted">
                先从 PP 热点页面配置 Wi-Fi 和 PA
                地址，再输入设备显示的八位配对码。
              </p>
              <div className="actions">
                <label>
                  设备名称
                  <input
                    autoFocus
                    name="name"
                    required
                    maxLength={48}
                    placeholder="例如 客厅 PP"
                  />
                </label>
                <label>
                  配对码
                  <input
                    name="code"
                    required
                    pattern="[0-9]{8}"
                    inputMode="numeric"
                    autoComplete="off"
                    maxLength={8}
                  />
                </label>
                <button
                  type="submit"
                  className="primary"
                  disabled={writing || !canOperate}
                >
                  {writing ? "正在配对…" : "确认配对"}
                </button>
                <button
                  type="button"
                  className="quiet"
                  disabled={writing}
                  onClick={() => setPairing(false)}
                >
                  取消
                </button>
              </div>
            </form>
          )}
          {readError && (
            <p className="error" role="alert">
              {readError}
            </p>
          )}
          {devices.map((d) => (
            <div className="device-row" key={d.device_id}>
              <strong>{d.name}</strong>
              <DeviceStatus device={d} />
              <button
                type="button"
                onClick={() => {
                  setSelected(d);
                  setPairing(false);
                }}
              >
                设备配置
              </button>
            </div>
          ))}
          {!devices.length && !readError && (
            <p className="device-empty muted">
              {reading ? "正在读取设备…" : "还没有配对设备。"}
            </p>
          )}
        </Panel>
      )}
    </main>
  );
}

const root = createRoot(document.getElementById("root")!);
void connect()
  .then((client) => root.render(<Passport client={client} />))
  .catch((error) => root.render(<p role="alert">{error.message}</p>));
