import { useEffect, useRef, useState } from "react";
import { chatPath } from "./http";

export type ResultReference = { id: string; version: string; bytes?: number };

const errors: Record<string, string> = {
  result_expired: "完整内容已过期或被清理，原执行记录保留。",
  result_missing: "完整内容文件缺失。",
  result_corrupt: "完整内容校验失败，不能作为原结果读取。",
  result_forbidden: "当前身份无权读取此结果。",
  result_version_conflict: "结果版本不匹配，请刷新后读取。",
};

export function ResultContent({
  reference,
}: {
  reference?: ResultReference | null;
}) {
  const [path, setPath] = useState("/text");
  const [text, setText] = useState("");
  const [offset, setOffset] = useState(0);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const request = useRef<AbortController | null>(null);
  useEffect(() => {
    setText("");
    setOffset(0);
    setDone(false);
    setError("");
    setBusy(false);
    return () => request.current?.abort();
  }, [reference?.id, reference?.version, path]);
  if (!reference) return null;
  const read = async () => {
    if (busy || done) return;
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError("");
    try {
      const query = new URLSearchParams({
        offset: String(offset),
        version: reference.version,
        path,
      });
      const response = await fetch(
        chatPath(`api/results/${encodeURIComponent(reference.id)}?${query}`),
        { signal: controller.signal },
      );
      const body = await response.json();
      if (!response.ok)
        throw new Error(errors[body.error] || body.error || "读取失败");
      if (controller.signal.aborted) return;
      setText((old) => old + body.data);
      setOffset(body.next_offset);
      setDone(body.eof);
    } catch (e) {
      if (!controller.signal.aborted)
        setError(e instanceof Error ? e.message : "读取失败，请重试");
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  };
  return (
    <details
      className="tool-result-more"
      onToggle={(event) => {
        if (event.currentTarget.open && !text && !done && !error) void read();
      }}
    >
      <summary>
        完整结果
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
      {text && (
        <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
          {text}
        </pre>
      )}
      {error && <p role="status">{error}</p>}
      {!done && (
        <button
          className="text-button"
          disabled={busy}
          onClick={() => void read()}
        >
          {busy
            ? "正在读取…"
            : error
              ? "重试读取"
              : text
                ? "继续读取"
                : "读取内容"}
        </button>
      )}
    </details>
  );
}
