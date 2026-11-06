// Request policy around Pi's public retry helper. No tools or agent loop.
import { AsyncLocalStorage } from "node:async_hooks";
export const attemptContext = new AsyncLocalStorage();

function safe(value, secret) {
  if (typeof value !== "string") return undefined;
  let text = value;
  if (secret) text = text.split(secret).join("[redacted]");
  return text
    .replace(/Bearer\s+\S+|sk-[A-Za-z0-9_-]{8,}/gi, "[redacted]")
    .replace(/https?:\/\/[^\s"<>]+/g, "[endpoint]")
    .replace(/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/g, "[redacted]")
    .replace(/[\x00-\x1f\x7f]/g, " ")
    .slice(0, 600);
}

export function observeError(error, evidence, secret) {
  if (!error || typeof error !== "object") return;
  evidence.provider_code =
    safe(String(error.code || error.type || error.status || ""), secret) ||
    undefined;
  evidence.provider_message = safe(error.message, secret);
  evidence.error_type = safe(error.name, secret);
  if (error.cause && typeof error.cause === "object") {
    evidence.cause_code = safe(error.cause.code, secret);
    evidence.cause_message = safe(error.cause.message, secret);
  }
}

export function observeFetch(nativeFetch, secret) {
  return async (input, init) => {
    const evidence = attemptContext.getStore();
    if (evidence) evidence.submission = "possibly_submitted";
    let response;
    try {
      response = await nativeFetch(input, { ...init, redirect: "error" });
    } catch (error) {
      if (evidence) {
        evidence.error_type = safe(error.name, secret());
        evidence.cause_code = safe(error.cause?.code, secret());
        evidence.cause_message = safe(error.cause?.message, secret());
      }
      const code = error.cause?.code || error.code;
      if (
        evidence &&
        typeof code === "string" &&
        /^[A-Z0-9_]{1,60}$/.test(code)
      )
        evidence.transport_code = code;
      throw error;
    }
    if (evidence) {
      evidence.submission = "response_received";
      evidence.status = response.status;
      evidence.request_id = safe(
        response.headers.get("x-request-id") ||
          response.headers.get("request-id"),
        secret(),
      );
      evidence.headers = new Headers();
      for (const name of ["retry-after", "retry-after-ms", "x-should-retry"])
        if (response.headers.has(name))
          evidence.headers.set(name, response.headers.get(name));
      if (!response.ok) {
        const reader = response.clone().body?.getReader();
        if (reader) {
          const chunks = [];
          let size = 0;
          try {
            for (;;) {
              const { done, value } = await reader.read();
              if (done) break;
              size += value.byteLength;
              if (size > 16384) break;
              chunks.push(Buffer.from(value));
            }
            if (size <= 16384) {
              const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
              const error = body.error || body;
              observeError(error, evidence, secret());
            }
          } catch {
            /* Upstream bodies are optional evidence, never executable input. */
          } finally {
            void reader.cancel().catch(() => {});
          }
        }
      }
    }
    return response;
  };
}

export function failure(message, evidence, secret) {
  const raw = message?.errorMessage || "native_request_failed";
  const status = evidence.status;
  const code = evidence.provider_code || "";
  const combined = `${code} ${evidence.provider_message || ""}`;
  const nativeError = safe(raw, secret);
  let category = "unknown";
  if (
    /insufficient_quota|quota_exceeded|credit|balance|billing|spend_limit|usage_limit|quota.*exhaust|payment/i.test(
      combined,
    ) ||
    status === 402
  )
    category = "quota";
  else if (
    status === 401 ||
    (!status &&
      /no api key|missing.*key|not authenticated|unauthorized|token.*expired|expired.*token/i.test(
        raw,
      ))
  )
    category = "authentication";
  else if (status === 403) category = "permission";
  else if ([400, 404, 405, 413, 422].includes(status))
    category = "configuration";
  else if ([408, 429, 500, 502, 503, 504, 529].includes(status))
    category = "transient";
  else if (
    !status &&
    /ECONNRESET|ECONNREFUSED|EAI_AGAIN|ETIMEDOUT|fetch failed|socket hang up|network error/i.test(
      raw + " " + (evidence.transport_code || ""),
    )
  )
    category = "transient";
  else if (
    evidence.submission === "not_submitted" &&
    /unsupported|not_in_catalog|auth|credential|budget|invalid/i.test(raw)
  )
    category = "configuration";
  if (status && status >= 400 && status < 500 && category === "unknown")
    category = "configuration";
  return {
    code: "model_" + category,
    category,
    message:
      evidence.provider_message ||
      (raw !== "native_request_failed" ? nativeError : undefined) ||
      (category === "transient"
        ? "模型连接暂时失败，未收到完整回复。"
        : evidence.submission === "not_submitted" &&
            /^native_[a-z_]+$/.test(raw)
          ? raw
          : "模型请求未完成，未记录更具体的错误原因。"),
    native_error: nativeError,
    ...(evidence.sdk_terminal ? { sdk_terminal: evidence.sdk_terminal } : {}),
    ...(evidence.error_type ? { error_type: evidence.error_type } : {}),
    ...(evidence.cause_code ? { cause_code: evidence.cause_code } : {}),
    ...(evidence.cause_message ? { cause_message: evidence.cause_message } : {}),
    ...(evidence.last_event ? { last_event: evidence.last_event } : {}),
    ...(evidence.terminal_event ? { terminal_event: evidence.terminal_event } : {}),
    ...(evidence.response_id ? { response_id: evidence.response_id } : {}),
    ...(status ? { status } : {}),
    ...(code ? { provider_code: code } : {}),
    ...(evidence.transport_code
      ? { transport_code: evidence.transport_code }
      : {}),
    ...(evidence.request_id ? { request_id: evidence.request_id } : {}),
    phase: evidence.output
      ? "receiving"
      : status
        ? "response"
        : evidence.submission === "not_submitted"
          ? "preparing"
          : "sending",
    submission: evidence.submission,
    output_started: !!evidence.output,
    generation_settled: !!evidence.terminal,
    retry_allowed: !!evidence.terminal,
  };
}

export async function reliableRequest({
  retryProviderRequest,
  attempt,
  gate,
  emit,
  signal,
  policy = {},
  secret,
}) {
  let number = 0,
    unknown = 0,
    lastFailure,
    lastTerminal,
    endedAt;
  const attempts = [];
  const maximum = policy.max_retries ?? 2;
  try {
    return await retryProviderRequest(
      async () => {
        number++;
        const retryWait = endedAt ? Math.round(performance.now() - endedAt) : 0;
        await gate({
          type: "attempt_start",
          attempt: number,
          retry_wait_ms: retryWait,
        });
        const started = performance.now();
        const evidence = {
          submission: "not_submitted",
          output: false,
          terminal: false,
        };
        let result;
        try {
          result = await attemptContext.run(evidence, () => attempt(evidence));
        } catch (error) {
          if (signal.aborted) throw error;
          const key = typeof secret === "function" ? secret() : secret;
          evidence.error_type = safe(error.name, key);
          evidence.cause_code = safe(error.cause?.code, key);
          evidence.cause_message = safe(error.cause?.message, key);
          const problem = failure({ errorMessage: error.message }, evidence, key);
          problem.retry_allowed = false;
          result = {
            failure: problem,
            terminal: {
              type: "error",
              error: {
                content: [],
                stopReason: "error",
                errorMessage: problem.message,
              },
            },
          };
        }
        const problem = result.failure;
        const summary = {
          attempt: number,
          retry_wait_ms: retryWait,
          elapsed_ms: Math.round(performance.now() - started),
          ...(problem
            ? {
                category: problem.category,
                status: problem.status,
                code: problem.code,
              }
            : { category: "completed" }),
        };
        attempts.push(summary);
        await gate({
          type: "attempt_end",
          attempt: number,
          terminal: result.terminal,
          failure: problem,
          generation_settled: evidence.terminal,
        });
        endedAt = performance.now();
        if (!problem) return result.terminal;
        lastTerminal = result.terminal;
        lastFailure = {
          ...problem,
          attempts: [...attempts],
          attempt_count: number,
        };
        const eligible =
          evidence.terminal &&
          !signal.aborted &&
          !evidence.output &&
          evidence.headers?.get("x-should-retry") !== "false" &&
          (problem.category === "transient" ||
            (problem.category === "unknown" &&
              unknown++ < (policy.unknown_retries ?? 1)));
        lastFailure.stop_reason = evidence.output
          ? "partial_output"
          : !eligible
            ? problem.category === "unknown" &&
              unknown > (policy.unknown_retries ?? 1)
              ? "unknown_retries_exhausted"
              : "not_retryable"
            : number > maximum
              ? "attempts_exhausted"
              : "not_retryable";
        const error = new Error(problem.code);
        if (eligible && number <= maximum) {
          error.status = problem.status;
          error.headers = new Headers(evidence.headers);
          // Policy owns eligibility; preserve upstream delay, never its override of a fatal error.
          error.headers.set("x-should-retry", "true");
          await emit({
            type: "retry",
            attempt: number,
            next_attempt: number + 1,
            category: problem.category,
          });
        }
        throw error;
      },
      {
        maxRetries: maximum,
        maxRetryDelayMs: (policy.max_delay_seconds ?? 30) * 1000,
        signal,
      },
    );
  } catch (error) {
    if (!lastFailure || signal.aborted) throw error;
    if (error.message.startsWith("Server requested"))
      lastFailure.stop_reason = "retry_delay_exceeded";
    return {
      ...lastTerminal,
      type: "error",
      error: {
        ...lastTerminal?.error,
        role: "assistant",
        content: lastTerminal?.error?.content || [],
        stopReason: "error",
        errorMessage: lastFailure.message,
        failure: lastFailure,
      },
      failure: lastFailure,
      http_status: lastFailure.status,
    };
  }
}
