/** Version 1: operator pages only. No credentials or arbitrary HTTP bridge. */
export type Path = string[];
export type Schema = {
  type?: string;
  title?: string;
  description?: string;
  default?: unknown;
  enum?: unknown[];
  readOnly?: boolean;
  writeOnly?: boolean;
  properties?: Record<string, Schema>;
  additionalProperties?: Schema | boolean;
  items?: Schema;
  [key: string]: unknown;
};
export type Patch = {
  path: Path;
  op: "set" | "default" | "create" | "replace_secret" | "clear_secret";
  value?: unknown;
};
export type Field = {
  path: Path;
  schema: Schema;
  value?: unknown;
  loaded_value?: unknown;
  source: string;
  explicit: boolean;
  files: string[];
};
export type Resource = {
  id: string;
  kind: string;
  title: string;
  sensitive: boolean;
  configured?: boolean;
  value?: string;
  modified?: boolean;
  read_only?: boolean;
};
export type Operation = {
  title?: string;
  description?: string;
  confirmation?: string;
  read_only?: boolean;
  input?: Schema;
  output: Schema;
};
export type State = {
  owner: string;
  version: string;
  loadedVersion?: string;
  schema: Schema;
  fields: Field[];
  resources: Resource[];
  actions: Record<string, Operation>;
  dirty: boolean;
  saving: boolean;
  errors: { path: unknown; message: string }[];
  application?: unknown;
  pendingRestart?: boolean;
};
export type ActionResult<T = unknown> = {
  request_id: string;
  state: "completed" | "rejected" | "unknown" | "unavailable";
  result?: T;
  code?: string;
  message?: string;
  issues?: unknown[];
};
export type Client = {
  state: () => Promise<State>;
  subscribe: (listener: (state: State) => void) => () => void;
  patch: (patch: Patch) => Promise<unknown>;
  resource: (
    id: string,
    op: "set" | "replace_secret" | "clear_secret",
    value?: string,
  ) => Promise<unknown>;
  action: <T = unknown>(
    name: string,
    args?: unknown,
    requestId?: string,
  ) => Promise<ActionResult<T>>;
  resize: (height: number) => Promise<unknown>;
  close: () => void;
};
function requestId(): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (n) =>
    n.toString(16).padStart(2, "0"),
  ).join("");
}
export class PageError extends Error {
  constructor(
    message: string,
    public code: string,
    public issues: unknown[] = [],
  ) {
    super(message);
  }
}

export function connect(): Promise<Client> {
  if (window.parent === window)
    return Promise.reject(
      new Error("请在 PA 配置后台打开页面，独立开发请使用 mockClient。"),
    );
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      window.removeEventListener("message", handshake);
      reject(new Error("配置后台未响应"));
    }, 12000);
    const handshake = (event: MessageEvent) => {
      if (
        event.source !== window.parent ||
        event.origin !== location.origin ||
        event.data?.type !== "pa-plugin-connect" ||
        event.data.version !== 1 ||
        !event.ports[0]
      )
        return;
      clearTimeout(timer);
      window.removeEventListener("message", handshake);
      const port = event.ports[0],
        listeners = new Set<(s: State) => void>(),
        pending = new Map<
          string,
          {
            resolve: (value: any) => void;
            reject: (reason: unknown) => void;
            timer: ReturnType<typeof setTimeout>;
          }
        >();
      let closed = false;
      port.onmessage = ({ data }) => {
        if (closed) return;
        if (data.type === "state") {
          for (const listener of listeners) listener(data.state);
          return;
        }
        const call = pending.get(data.id);
        if (!call) return;
        pending.delete(data.id);
        clearTimeout(call.timer);
        if (data.error)
          call.reject(
            new PageError(
              data.error.message,
              data.error.code,
              data.error.issues,
            ),
          );
        else call.resolve(data.result);
      };
      const request = (method: string, args: unknown = {}): Promise<any> =>
        new Promise((resolve, reject) => {
          if (closed) {
            reject(new Error("页面已关闭"));
            return;
          }
          const id = requestId();
          const timer = setTimeout(() => {
            pending.delete(id);
            reject(
              new PageError(
                "响应未确认，请查询状态；不会自动重试。",
                "response_unknown",
              ),
            );
          }, 45000);
          pending.set(id, { resolve, reject, timer });
          port.postMessage({ id, method, arguments: args });
        });
      const close = () => {
        closed = true;
        port.close();
        listeners.clear();
        for (const p of pending.values()) {
          clearTimeout(p.timer);
          p.reject(new Error("页面已关闭"));
        }
        pending.clear();
        window.removeEventListener("pagehide", close);
      };
      window.addEventListener("pagehide", close, { once: true });
      resolve({
        state: () => request("state"),
        subscribe: (listener) => {
          listeners.add(listener);
          return () => {
            listeners.delete(listener);
          };
        },
        patch: (patch) => request("patch", patch),
        resource: (id, op, value) => request("resource", { id, op, value }),
        action: (name, args = {}, id = requestId()) =>
          request("action", { name, arguments: args, requestId: id }),
        resize: (height) => request("resize", { height }),
        close,
      });
    };
    window.addEventListener("message", handshake);
    window.parent.postMessage(
      { type: "pa-plugin-ready", version: 1 },
      location.origin,
    );
  });
}

/** Independent Vite preview only. Never writes PA configuration. */
export function mockClient(
  initial: State,
  actions: Record<string, (args: any) => unknown> = {},
): Client {
  let state = structuredClone(initial);
  const listeners = new Set<(s: State) => void>();
  const emit = () => {
    for (const l of listeners) l(structuredClone(state));
  };
  return {
    state: async () => structuredClone(state),
    subscribe: (l) => {
      listeners.add(l);
      return () => {
        listeners.delete(l);
      };
    },
    resize: async () => null,
    close: () => listeners.clear(),
    patch: async (patch) => {
      const field = state.fields.find((f) =>
        f.path.every((p, i) => patch.path[i] === p),
      );
      if (!field) throw new Error("未声明字段");
      const value =
        patch.op === "default"
          ? field.schema.default
          : patch.op === "replace_secret"
            ? { configured: true }
            : patch.op === "clear_secret"
              ? { configured: false }
              : patch.value;
      if (patch.path.length === field.path.length) field.value = value;
      else {
        let target: any = (field.value ??= {});
        for (const part of patch.path.slice(field.path.length, -1))
          target = target[part] ??= {};
        target[patch.path.at(-1)!] = value;
      }
      state.dirty = true;
      emit();
      return null;
    },
    resource: async (id, op, value) => {
      const r = state.resources.find((r) => r.id === id);
      if (!r) throw new Error("未声明资源");
      r.configured = op !== "clear_secret";
      if (!r.sensitive) r.value = value;
      r.modified = true;
      state.dirty = true;
      emit();
      return null;
    },
    action: async (name, args = {}, id = requestId()) => {
      if (!actions[name]) throw new Error("未模拟此操作");
      return {
        request_id: id,
        state: "completed",
        result: (await actions[name](args)) as any,
      };
    },
  };
}
