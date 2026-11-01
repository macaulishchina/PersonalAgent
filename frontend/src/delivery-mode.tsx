import { useEffect, useState } from "react";

export type DeliveryMode = "steer" | "queue";
const storageKey = "pa-delivery-mode";

export function useDeliveryMode() {
  const [mode, setMode] = useState<DeliveryMode>(() => {
    try {
      return localStorage.getItem(storageKey) === "queue" ? "queue" : "steer";
    } catch {
      return "steer";
    }
  });
  const choose = (value: DeliveryMode) => {
    setMode(value);
    try {
      localStorage.setItem(storageKey, value);
    } catch {
      // The current choice still works when browser storage is unavailable.
    }
  };
  useEffect(() => {
    const sync = (event: StorageEvent) => {
      if (event.key === storageKey || event.key === null)
        setMode(event.newValue === "queue" ? "queue" : "steer");
    };
    window.addEventListener("storage", sync);
    return () => window.removeEventListener("storage", sync);
  }, []);
  return [mode, choose] as const;
}

export function DeliveryModeSelector({
  mode,
  onChange,
  disabled,
}: {
  mode: DeliveryMode;
  onChange: (mode: DeliveryMode) => void;
  disabled: boolean;
}) {
  const steering = mode === "steer";
  return (
    <button
      type="button"
      className="delivery-switch"
      role="switch"
      aria-label="直接引导"
      aria-checked={steering}
      aria-description={steering ? "补充或调整当前执行" : "当前执行结束后发送"}
      title={steering ? "补充或调整当前执行" : "当前执行结束后发送"}
      disabled={disabled}
      onClick={() => onChange(steering ? "queue" : "steer")}
    >
      <span className="delivery-switch-thumb" aria-hidden="true" />
      <span
        className="delivery-switch-option"
        data-selected={steering}
        aria-hidden="true"
      >
        引导
      </span>
      <span
        className="delivery-switch-option"
        data-selected={!steering}
        aria-hidden="true"
      >
        排队
      </span>
    </button>
  );
}
