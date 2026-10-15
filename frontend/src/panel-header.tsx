import { X } from "lucide-react";
import type { ReactNode } from "react";

export function PanelHeader({
  title,
  onClose,
  actions,
}: {
  title: string;
  onClose: () => void;
  actions?: ReactNode;
}) {
  return (
    <div className="panel-header">
      <h2>{title}</h2>
      {actions}
      <button
        type="button"
        className="icon-button"
        data-panel-close
        onClick={onClose}
        aria-label="关闭面板"
      >
        <X size={19} />
      </button>
    </div>
  );
}
