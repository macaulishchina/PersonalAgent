import { useEffect, useState } from "react";
import { CornerDownLeft, ListOrdered } from "lucide-react";

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
      title={
        steering ? "当前：引导 · 点击切换为排队" : "当前：排队 · 点击切换为引导"
      }
      disabled={disabled}
      onClick={() => onChange(steering ? "queue" : "steer")}
    >
      {steering ? (
        <CornerDownLeft size={13} aria-hidden="true" />
      ) : (
        <ListOrdered size={13} aria-hidden="true" />
      )}
      <span aria-hidden="true">{steering ? "引导" : "排队"}</span>
    </button>
  );
}
