import { useEffect, useRef, useState, type ReactNode } from "react";
import { chatPath } from "./http";

export type ResultReference = { id: string; version: string; bytes?: number };
export type ResultPresentation = {
  body?: "complete" | "excerpt";
  phase?: string | null;
  turn_status?: string;
  result_offset?: number;
  result_length?: number;
};

const errors: Record<string, string> = {
  result_expired: "完整内容已过期或被清理，原执行记录保留。",
  result_missing: "完整内容文件缺失。",
  result_corrupt: "完整内容校验失败，不能作为原结果读取。",
  result_forbidden: "当前身份无权读取此结果。",
  result_version_conflict: "结果版本不匹配，请刷新后读取。",
};

function useResultReader(
  reference: ResultReference | null | undefined,
  path = "/text",
  start = 0,
  length?: number,
) {
  const [text, setText] = useState("");
  const [offset, setOffset] = useState(start);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const request = useRef<AbortController | null>(null);
  useEffect(() => {
    setText("");
    setOffset(start);
    setDone(false);
    setError("");
    setBusy(false);
    return () => {
      request.current?.abort();
      request.current = null;
    };
  }, [reference?.id, reference?.version, path, start, length]);
  const read = async () => {
    if (!reference || request.current || done) return;
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError("");
    try {
      const remaining = length === undefined ? 16000 : start + length - offset;
      if (remaining <= 0) {
        setDone(true);
        return;
      }
      const query = new URLSearchParams({
        offset: String(offset),
        version: reference.version,
        path,
        length: String(Math.min(16000, remaining)),
      });
      const response = await fetch(
        chatPath(`api/results/${encodeURIComponent(reference.id)}?${query}`),
        { signal: controller.signal },
      );
      const body = await response.json();
      if (!response.ok)
        throw new Error(errors[body.error] || body.error || "读取失败");
      if (controller.signal.aborted) return;
      if (
        typeof body.data !== "string" ||
        !Number.isSafeInteger(body.next_offset) ||
        (body.next_offset <= offset && !body.eof)
      )
        throw new Error("读取进度无效，请重试读取。");
      setText((old) => old + body.data);
      setOffset(body.next_offset);
      setDone(
        body.eof ||
          (length !== undefined && body.next_offset >= start + length),
      );
    } catch (e) {
      if (!controller.signal.aborted)
        setError(e instanceof Error ? e.message : "读取失败，请重试");
    } finally {
      if (request.current === controller) {
        request.current = null;
        setBusy(false);
      }
    }
  };
  return { text, done, busy, error, read };
}

/** One body: only an owner-declared excerpt offers expansion, replacing its preview. */
export function ResultBody({
  text,
  reference,
  presentation,
  render,
}: {
  text: string;
  reference?: ResultReference | null;
  presentation?: ResultPresentation | null;
  render: (text: string) => ReactNode;
}) {
  const reader = useResultReader(
    reference,
    "/text",
    presentation?.result_offset,
    presentation?.result_length,
  );
  const expandable = reference && presentation?.body === "excerpt";
  return (
    <>
      {render(expandable && reader.text ? reader.text : text)}
      {expandable && !reader.done && (
        <button
          className="text-button result-read-more"
          disabled={reader.busy}
          onClick={() => void reader.read()}
        >
          {reader.busy
            ? "正在读取…"
            : reader.error
              ? "重试读取"
              : reader.text
                ? "继续读取"
                : "展开全文"}
        </button>
      )}
      {expandable && reader.error && (
        <p className="result-read-error" role="status">
          {reader.error} 当前已显示的内容仍然保留。
        </p>
      )}
    </>
  );
}

/** Raw result inspection belongs inside tool/execution details, never ordinary replies. */
export function ResultContent({
  reference,
}: {
  reference?: ResultReference | null;
}) {
  const [path, setPath] = useState("/text");
  const reader = useResultReader(reference, path);
  if (!reference) return null;
  return (
    <details
      className="tool-result-more"
      onToggle={(event) => {
        if (
          event.currentTarget.open &&
          !reader.text &&
          !reader.done &&
          !reader.error
        )
          void reader.read();
      }}
    >
      <summary>
        原始结果
        {reference.bytes ? ` · ${(reference.bytes / 1024).toFixed(1)} KiB` : ""}
      </summary>
      <label>
        内容范围{" "}
        <select
          aria-label="结果内容范围"
          value={path}
          onChange={(e) => setPath(e.target.value)}
        >
          <option value="/text">正文</option>
          <option value="">完整数据（JSON）</option>
        </select>
      </label>
      {reader.text && <pre className="tool-result-text">{reader.text}</pre>}
      {reader.error && <p role="status">{reader.error}</p>}
      {!reader.done && (
        <button
          className="text-button"
          disabled={reader.busy}
          onClick={() => void reader.read()}
        >
          {reader.busy
            ? "正在读取…"
            : reader.error
              ? "重试读取"
              : reader.text
                ? "继续读取"
                : "读取内容"}
        </button>
      )}
    </details>
  );
}
