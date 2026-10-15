import { useRef } from "react";
import { Drawer } from "./drawer";
import { TaskPanel } from "./tasks";
import { NotificationPanel } from "./notifications";
export type PanelSection = "tasks" | "notifications";

export function ActivityPanel({
  section,
  onClose,
  api,
  nonce,
  unreadCount,
  onReference,
}: {
  section: PanelSection | null;
  onClose: () => void;
  api: (path: string, options?: RequestInit) => Promise<any>;
  nonce: () => string;
  unreadCount: number;
  onReference: (notice: { id: string; title: string }) => void;
}) {
  const lastSection = useRef<PanelSection>("tasks");
  if (section) lastSection.current = section;
  const displayed = section ?? lastSection.current;
  return (
    <Drawer
      id="activity-panel"
      label="任务与通知"
      open={section !== null}
      onClose={onClose}
    >
      <div className="activity-pages">
        <section
          className="activity-page"
          id="task-panel"
          aria-label="后台任务"
          hidden={displayed !== "tasks"}
        >
          <TaskPanel
            open={section === "tasks"}
            onClose={onClose}
            api={api}
            nonce={nonce}
          />
        </section>
        <section
          className="activity-page"
          id="notification-panel"
          aria-label="通知"
          hidden={displayed !== "notifications"}
        >
          <NotificationPanel
            open={section === "notifications"}
            onClose={onClose}
            unreadCount={unreadCount}
            api={api}
            onReference={onReference}
          />
        </section>
      </div>
    </Drawer>
  );
}
