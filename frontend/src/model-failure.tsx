import { useRef, useState } from "react";
import { RotateCcw, ChevronDown } from "lucide-react";

export type ModelFailure = {
  code: string;
  category: string;
  message: string;
  status?: number;
  provider_code?: string;
  request_id?: string;
  retry_allowed?: boolean;
  attempt_count?: number;
  stop_reason?: string;
  model?: string;
  provider?: string;
  attempts?: {
    attempt: number;
    elapsed_ms: number;
    retry_wait_ms?: number;
    category: string;
    status?: number;
  }[];
};
const categories: Record<string, string> = {
  authentication: "模型鉴权失败",
  permission: "模型访问被拒绝",
  quota: "模型余额或额度不足",
  configuration: "模型请求被拒绝",
  transient: "模型服务暂时不可用",
  unknown: "模型请求未完成",
  result: "模型结果处理失败",
  deadline: "模型请求超时",
  transport: "模型连接中断",
  usage: "用量保护已停止请求",
};
const stops: Record<string, string> = {
  attempts_exhausted: "已达到自动重试次数",
  unknown_retries_exhausted: "未分类错误已尝试一次，未继续自动重试",
  retry_delay_exceeded: "供应商要求的等待超过自动等待上限",
  partial_output: "已收到部分输出，未自动重新生成",
  not_retryable: "不满足自动重试条件",
};
export function ModelFailureNotice({
  value,
  turn,
  disabled,
  onRetry,
}: {
  value: ModelFailure;
  turn?: string | null;
  disabled?: boolean;
  onRetry: (turn: string, requestId: string) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [accepted, setAccepted] = useState(false);
  const [error, setError] = useState("");
  const requestId = useRef<string | null>(null);
  async function retry() {
    if (!turn || busy || accepted || disabled) return;
    requestId.current ??= crypto.randomUUID();
    setBusy(true);
    setError("");
    try {
      await onRetry(turn, requestId.current);
      setAccepted(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "恢复请求未确认，请重试查询");
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="model-failure" role="status">
      <p className="model-failure-title">
        {categories[value.category] || "模型请求未完成"}
        {value.status ? `（HTTP ${value.status}）` : ""}
      </p>
      <p className="model-failure-reason">{value.message}</p>
      <div className="model-failure-actions">
        {value.retry_allowed && turn && (
          <button
            type="button"
            onClick={retry}
            disabled={busy || accepted || disabled}
          >
            <RotateCcw size={14} />
            {accepted ? "已提交继续执行" : busy ? "正在提交…" : "重试"}
          </button>
        )}
        <details>
          <summary>
            详情 <ChevronDown size={13} />
          </summary>
          <dl>
            {value.model && (
              <>
                <dt>模型</dt>
                <dd>{value.model}</dd>
              </>
            )}
            {value.provider && (
              <>
                <dt>供应商</dt>
                <dd>{value.provider}</dd>
              </>
            )}
            <dt>错误码</dt>
            <dd>{value.provider_code || value.code}</dd>
            {value.request_id && (
              <>
                <dt>请求标识</dt>
                <dd>{value.request_id}</dd>
              </>
            )}
            {value.stop_reason && (
              <>
                <dt>停止原因</dt>
                <dd>{stops[value.stop_reason] || value.stop_reason}</dd>
              </>
            )}
          </dl>
          {value.attempts?.map((a) => (
            <p key={a.attempt}>
              第 {a.attempt} 次 · {(a.elapsed_ms / 1000).toFixed(1)} 秒
              {a.retry_wait_ms
                ? ` · 重试前等待 ${(a.retry_wait_ms / 1000).toFixed(1)} 秒`
                : ""}{" "}
              · {categories[a.category] || a.category}
              {a.status ? ` · HTTP ${a.status}` : ""}
            </p>
          ))}
          {!value.retry_allowed && (
            <p>
              请先处理上述原因；存在未确认操作时，需要核对执行状态后再继续。
            </p>
          )}
        </details>
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
