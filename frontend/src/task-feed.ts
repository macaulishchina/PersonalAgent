import { useCallback, useEffect, useRef, useState } from "react";

type Page = {
  tasks?: any[];
  task?: any;
  event_inbox?: {
    pending_count: number;
    subscription: { error: string | null } | null;
  } | null;
  runs: any[];
  active_runs?: any[];
  updates?: any[];
  removed?: string[];
  before: string | null;
};
type Feed = Page & { tasks: any[] };
const empty = (): Feed => ({ tasks: [], runs: [], before: null });
const unfinished = new Set([
  "queued",
  "dispatching",
  "running",
  "waiting_user",
  "paused",
  "unknown",
]);
const unique = (items: any[]) => {
  const found = new Map<string, any>();
  for (const item of items) if (!found.has(item.id)) found.set(item.id, item);
  return [...found.values()].sort(
    (a, b) => b.created - a.created || b.id.localeCompare(a.id),
  );
};

// Poll one bounded page and previously unfinished executions. Retain older items
// and their cursor so a new arrival cannot open a gap between fetched pages.
export function useTaskFeed(
  api: (path: string, options?: RequestInit) => Promise<any>,
  path: string,
  active: boolean,
  autoMore = true,
) {
  const apiRef = useRef(api);
  apiRef.current = api;
  const [data, setData] = useState<Feed>(empty);
  const cache = useRef<Feed>(empty());
  const head = useRef<string[]>([]);
  const initialized = useRef(false);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  const failedMore = useRef(false);
  const generation = useRef(0);
  const flight = useRef<AbortController | null>(null);
  const currentPath = useRef(path);
  const sentinel = useRef<HTMLDivElement>(null);
  const scroll = useRef<HTMLDivElement>(null);
  const load = useCallback(async (more = false) => {
    if (flight.current || (more && !cache.current.before)) return;
    const controller = new AbortController();
    flight.current = controller;
    const seq = generation.current;
    setLoading(true);
    setLoadingMore(more);
    setError("");
    try {
      const known = currentPath.current.includes("task_id=")
        ? cache.current.runs
            .filter((run) => unfinished.has(run.state))
            .slice(0, 20)
            .map((run) => run.id)
        : [
            ...new Set([
              ...cache.current.runs
                .filter((run) => unfinished.has(run.state))
                .map((run) => run.task_id),
              ...head.current,
            ]),
          ].slice(0, 50);
      const page: Page = await apiRef.current(
        `${currentPath.current}${more ? `&before=${encodeURIComponent(cache.current.before!)}` : `&known=${encodeURIComponent(JSON.stringify(known))}`}`,
        { signal: controller.signal },
      );
      if (seq !== generation.current) return;
      const fresh = page.task ? page.runs : page.tasks || [];
      const previous = page.task ? cache.current.runs : cache.current.tasks;
      const oldHead = new Set(head.current);
      // A previously empty view, or more than a whole page of new arrivals while
      // hidden, needs a fresh cursor. Keeping the exhausted old cursor would
      // make the unseen middle records unreachable.
      const reset =
        !more &&
        initialized.current &&
        (!previous.length ||
          (oldHead.size > 0 &&
            fresh.length > 0 &&
            !fresh.some((item) => oldHead.has(item.id))));
      const old = reset ? empty() : cache.current;
      const removed = new Set(page.removed || []);
      const taskUpdates = page.task ? [] : page.updates || [];
      const tasks = unique([
        ...(page.tasks || []),
        ...taskUpdates,
        ...old.tasks,
      ]).filter((task) => !removed.has(task.id));
      const replacedTasks = new Set(
        [...(page.tasks || []), ...taskUpdates].map((task) => task.id),
      );
      const runs = page.task
        ? unique([
            ...(page.updates || []),
            ...(page.active_runs || []),
            ...page.runs,
            ...old.runs,
          ])
        : unique([
            ...page.runs,
            ...old.runs.filter(
              (run) =>
                !replacedTasks.has(run.task_id) && !removed.has(run.task_id),
            ),
          ]);
      const next: Feed = {
        ...page,
        tasks,
        runs,
        before:
          more || !initialized.current || reset ? page.before : old.before,
      };
      if (!more) head.current = fresh.map((item) => item.id);
      initialized.current = true;
      cache.current = next;
      setData(next);
      setLoaded(true);
    } catch (e) {
      if (seq === generation.current && !controller.signal.aborted) {
        failedMore.current = more;
        setError(String(e));
      }
    } finally {
      if (flight.current === controller) {
        flight.current = null;
        setLoading(false);
        setLoadingMore(false);
      }
    }
  }, []);
  useEffect(() => {
    generation.current++;
    flight.current?.abort();
    flight.current = null;
    if (currentPath.current !== path) {
      currentPath.current = path;
      cache.current = empty();
      head.current = [];
      initialized.current = false;
      setData(empty());
      setLoaded(false);
      setError("");
      scroll.current?.scrollTo({ top: 0 });
    }
    setLoading(false);
    setLoadingMore(false);
    if (!active) return;
    void load();
    const timer = window.setInterval(() => {
      if (!document.hidden) void load();
    }, 5000);
    return () => {
      generation.current++;
      flight.current?.abort();
      flight.current = null;
      window.clearInterval(timer);
    };
  }, [path, active, load]);
  useEffect(() => {
    if (
      !active ||
      !autoMore ||
      !data.before ||
      !sentinel.current ||
      loading ||
      error
    )
      return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) void load(true);
      },
      { root: scroll.current, rootMargin: "0px 0px 100px 0px" },
    );
    observer.observe(sentinel.current);
    return () => observer.disconnect();
  }, [active, autoMore, data.before, loading, error, load]);
  return {
    data,
    loaded,
    loading,
    loadingMore,
    error,
    scroll,
    sentinel,
    refresh: load,
    retry: () => load(failedMore.current),
  };
}
