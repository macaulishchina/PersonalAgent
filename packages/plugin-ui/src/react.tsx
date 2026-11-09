import { useEffect, useState } from "react";
import type { Client, State, Field } from "./index";
export function useConfiguration(client: Client) {
  const [state, setState] = useState<State | null>(null),
    [error, setError] = useState<string>("");
  useEffect(() => {
    let alive = true;
    const off = client.subscribe((s) => {
      if (alive) setState(s);
    });
    client
      .state()
      .then((s) => {
        if (alive) setState(s);
      })
      .catch((e) => {
        if (alive) setError(e.message);
      });
    return () => {
      alive = false;
      off();
    };
  }, [client]);
  return { state, error };
}
/** Optional scalar editor. Complex interaction belongs to the plugin. */
export function AutoField({ client, field }: { client: Client; field: Field }) {
  const [error, setError] = useState("");
  const secret = Boolean(field.schema.writeOnly || field.schema["x-sensitive"]);
  const write = (value: unknown) => {
    setError("");
    client
      .patch({ path: field.path, op: secret ? "replace_secret" : "set", value })
      .catch((e) => setError(e.message));
  };
  const rule = field.schema,
    value = field.value;
  const [text, setText] = useState(secret ? "" : String(value ?? ""));
  useEffect(() => {
    if (!secret) setText(String(value ?? ""));
  }, [value, secret]);
  return (
    <label className="pa-field">
      <span>{rule.title || field.path.join(".")}</span>
      {rule.enum ? (
        <select
          aria-label={rule.title || field.path.join(".")}
          disabled={rule.readOnly}
          value={String(value ?? "")}
          onChange={(e) =>
            write(rule.enum!.find((v) => String(v) === e.target.value))
          }
        >
          {rule.enum.map((v) => (
            <option key={String(v)}>{String(v)}</option>
          ))}
        </select>
      ) : rule.type === "boolean" ? (
        <input
          aria-label={rule.title || field.path.join(".")}
          disabled={rule.readOnly}
          type="checkbox"
          checked={Boolean(value)}
          onChange={(e) => write(e.target.checked)}
        />
      ) : (
        <input
          aria-label={rule.title || field.path.join(".")}
          disabled={rule.readOnly}
          type={
            secret
              ? "password"
              : rule.type === "number" || rule.type === "integer"
                ? "number"
                : "text"
          }
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            if (secret && !e.target.value) return;
            write(
              rule.type === "number" || rule.type === "integer"
                ? Number(e.target.value)
                : e.target.value,
            );
          }}
          onBlur={() => {
            if (secret) setText("");
          }}
        />
      )}
      <small>{rule.description}</small>
      {error && <span role="alert">{error}</span>}
      {!rule.readOnly && !secret && (
        <button
          type="button"
          onClick={() => {
            client
              .patch({ path: field.path, op: "default" })
              .catch((e) => setError(e.message));
          }}
        >
          恢复默认
        </button>
      )}
    </label>
  );
}
export function AutoForm({ client, state }: { client: Client; state: State }) {
  return (
    <div>
      {state.fields
        .filter((f) => !["object", "array"].includes(f.schema.type || ""))
        .map((f) => (
          <AutoField key={f.path.join(".")} client={client} field={f} />
        ))}
    </div>
  );
}
