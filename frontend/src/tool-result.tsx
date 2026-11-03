import { ArrowUpRight, ChevronDown } from "lucide-react";
import { RUN_LABELS } from "./task-presentation";

export type ToolSummary = {
  kind: "create_task" | "get_task" | "fetch_webpage" | "tool";
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
  error_message?: string;
  next_action?: string;
  retryable?: boolean;
  dispatch?: string;
  result_state?: string;
  stage?: string;
  field?: string;
};

export function toolOutcome(value: ToolSummary): string {
  if (value.dispatch === "not_dispatched")
    return value.stage === "authorization" ? "被禁止" : "未执行";
  if (value.result_state === "unknown") return "结果待确认";
  if (value.result_state === "cancelled") return "已取消";
  if (value.outcome === "failed") return "调用失败";
  return {
    create_task: "已创建",
    get_task: "已查询",
    fetch_webpage: "读取成功",
    tool: "调用完成",
  }[value.kind];
}

const errors: Record<string, string> = {
  invalid_arguments: "参数不符合工具要求，操作未执行。",
  revision_conflict: "任务已更新，请读取最新版本后再修改；本次修改未执行。",
  task_run_conflict: "任务已有未结束的执行，本次未重复创建。请先查看现有执行。",
  deadline_expired: "请求期限已到，操作未发出。",
  incomplete_tool_arguments: "模型未生成完整参数，操作未执行。",
  prior_result_unknown: "前次相同操作的结果尚未确认，本次未重复执行。",
  tool_unavailable: "工具尚未就绪或当前不可用，操作未执行。",
  tool_scope_denied: "此执行没有该工具的使用权限。",
  tool_permission_denied: "工具已被权限策略禁止，没有逐次审批弹窗。",
  repeated_invalid_call: "相同的无效调用反复出现，本轮已停止。",
  invalid_tool_result:
    "工具已返回，但结果格式异常；请核对操作状态，勿重复执行。",
  tool_result_unconfirmed: "工具调用后返回异常；操作是否生效需进一步核对。",
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
      <details className="tool-result">
        <summary>查看原因</summary>
        {value.error_message ||
          errors[value.error_code || ""] ||
          "未取得有效结果，可查看助手的后续说明。"}
        {value.error_code && (
          <div className="tool-result-meta">错误：{value.error_code}</div>
        )}
        {value.field && (
          <div className="tool-result-meta">字段：{value.field}</div>
        )}
        {value.task_id && (
          <button
            className="tool-result-link"
            onClick={() => onOpenTask(value.task_id!)}
          >
            查看现有任务 <ArrowUpRight size={13} aria-hidden="true" />
          </button>
        )}
      </details>
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
