import { ArrowUpRight, ChevronDown } from "lucide-react";
import { RUN_LABELS } from "./task-presentation";

export type ToolSummary = {
  kind: "create_task" | "get_task" | "fetch_webpage";
  outcome: "completed" | "failed";
  task_id?: string;
  title?: string;
  model?: string;
  effort?: string;
  state?: string;
  summary?: string;
  summary_truncated?: boolean;
  domain?: string;
  url?: string;
  provider?: string;
  elapsed_ms?: number;
  truncated?: boolean;
  cached?: boolean;
  error_code?: string;
};

export function toolOutcome(value: ToolSummary): string {
  if (value.outcome === "failed") return "失败";
  return {
    create_task: "已创建",
    get_task: "已查询",
    fetch_webpage: "读取成功",
  }[value.kind];
}

const errors: Record<string, string> = {
  authentication_failed: "服务鉴权失败",
  credentials_missing: "尚未配置凭据",
  credentials_invalid: "服务凭据无效",
  insufficient_balance: "服务余额不足",
  rate_limited: "服务暂时限流",
  provider_timeout: "读取超时",
  search_timeout: "读取超时",
  provider_network_error: "网络连接失败",
  provider_access_denied: "服务拒绝访问",
  page_unavailable: "网页不可访问",
  page_content_unavailable: "未能取得网页正文",
};

export function ToolResult({
  value,
  onOpenTask,
}: {
  value: ToolSummary;
  onOpenTask: (id: string) => void;
}) {
  if (value.outcome === "failed")
    return (
      <div className="tool-result tool-result-error">
        {errors[value.error_code || ""] ||
          "未取得有效结果，可查看助手的后续说明。"}
      </div>
    );

  if (value.kind === "create_task" || value.kind === "get_task") {
    const preview = value.summary?.slice(0, 100);
    return (
      <div className="tool-result">
        {value.task_id && (
          <button
            className="tool-result-link"
            onClick={() => onOpenTask(value.task_id!)}
          >
            <span>{value.title || "查看任务"}</span>
            <ArrowUpRight size={13} aria-hidden="true" />
          </button>
        )}
        {value.kind === "create_task" ? (
          (value.model || value.effort) && (
            <div
              className="tool-result-meta"
              title="创建时选定的执行配置；最新执行情况见任务详情"
            >
              {[value.model, value.effort].filter(Boolean).join(" · ")}
            </div>
          )
        ) : (
          <>
            <div className="tool-result-meta">
              查询时：
              {RUN_LABELS[value.state || ""] ||
                (value.state ? "执行状态待核实" : "暂无执行记录")}
            </div>
            {value.summary &&
              (value.summary.length > 100 || value.summary_truncated ? (
                <details className="tool-result-more">
                  <summary>
                    <span className="tool-result-preview">{preview}…</span>
                    <span className="tool-result-collapse">收起结果</span>
                    <ChevronDown size={13} aria-hidden="true" />
                  </summary>
                  <div className="tool-result-text">
                    {value.summary}
                    {value.summary_truncated && "…（更多内容见任务详情）"}
                  </div>
                </details>
              ) : (
                <div className="tool-result-text">{value.summary}</div>
              ))}
          </>
        )}
      </div>
    );
  }
  // The adapter supplies only HTTP(S) URLs without credentials/query tokens.
  let url: URL | undefined;
  try {
    url = value.url ? new URL(value.url) : undefined;
  } catch {
    /* No link for malformed historical data. */
  }
  const href =
    url &&
    ["http:", "https:"].includes(url.protocol) &&
    !url.username &&
    !url.password &&
    !url.search
      ? url.href
      : undefined;
  return (
    <div className="tool-result">
      <div className="tool-result-heading">
        {href ? (
          <a
            className="tool-result-link"
            href={href}
            target="_blank"
            rel="noopener noreferrer"
          >
            <span>{value.domain || "原网页"}</span>
            <ArrowUpRight size={13} aria-hidden="true" />
          </a>
        ) : (
          <span>{value.domain || "网页正文"}</span>
        )}
        {value.elapsed_ms != null && (
          <span className="tool-result-meta">
            {value.elapsed_ms < 1000
              ? `${value.elapsed_ms} ms`
              : `${(value.elapsed_ms / 1000).toFixed(1)} 秒`}
          </span>
        )}
        {value.truncated && (
          <span className="tool-result-meta">正文已截断</span>
        )}
      </div>
      {(value.title || value.provider || value.cached) && (
        <details className="tool-result-more">
          <summary>
            <span>{value.title || "读取详情"}</span>
            <ChevronDown size={13} aria-hidden="true" />
          </summary>
          <div className="tool-result-meta">
            {[
              value.provider && `服务：${value.provider}`,
              value.cached && "使用缓存",
            ]
              .filter(Boolean)
              .join(" · ")}
          </div>
        </details>
      )}
    </div>
  );
}
