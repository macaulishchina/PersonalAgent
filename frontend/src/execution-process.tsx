import { useState, type ReactNode } from "react";
import { ChevronDown } from "lucide-react";

/** Completion changes the default once; later updates preserve the user's choice. */
export function ExecutionProcess({
  settled,
  count,
  children,
}: {
  settled: boolean;
  count: number;
  children: ReactNode;
}) {
  const [choice, setChoice] = useState<{
    settled: boolean;
    open: boolean;
  } | null>(null);
  const open = choice?.settled === settled ? choice.open : !settled;
  return (
    <details className="execution-process" open={open}>
      <summary
        onClick={(event) => {
          event.preventDefault();
          setChoice({ settled, open: !open });
        }}
      >
        <ChevronDown size={13} aria-hidden="true" />
        <span>执行过程</span>
        <span className="execution-process-count">{count} 项操作</span>
      </summary>
      <div className="execution-process-content">{children}</div>
    </details>
  );
}
