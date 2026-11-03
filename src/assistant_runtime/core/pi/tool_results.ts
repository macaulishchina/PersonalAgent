/** Stable result projections. Delivery retries never invoke the original tool. */
import { ownerOutcome } from "./tool_lifecycle.ts";
import { imagesIn } from "./tool_media.ts";

const key = "personal_agent.result";
const mediaKey = "personal_agent.tool_media";
const object = (v: any) => v && typeof v === "object" && !Array.isArray(v);

async function host(path: string, value: any) {
  const response = await fetch(
    (process.env.ASSISTANT_CONTROL_URL || "") + path,
    {
      method: "POST",
      signal: AbortSignal.timeout(15000),
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + (process.env.PA_EXECUTION_TOKEN || ""),
        "x-assistant-session": process.env.ASSISTANT_CONTROL_SESSION || "",
        "x-assistant-control": process.env.ASSISTANT_CONTROL_TOKEN || "",
      },
      body: JSON.stringify(value),
    },
  );
  const result = await response.json();
  if (!response.ok)
    throw new Error(result.error || "result_delivery_unavailable");
  return result;
}

export function excerpt(value: string, limit: number) {
  if (value.length <= limit) return value;
  const marker = "\n[…中间内容省略；请按引用读取完整结果…]\n";
  const size = Math.max(0, limit - marker.length);
  const head = Math.ceil(size * 0.65);
  return value.slice(0, head) + marker + value.slice(-(size - head));
}

function prepared(event: any) {
  const images = imagesIn(event);
  const clean = (value: any, depth = 0): any => {
    if (!value || typeof value !== "object" || depth > 16) return value;
    if (value.type === "image" && typeof value.data === "string") {
      const index = images.findIndex(
        (i) =>
          i.data === value.data &&
          i.mime_type === (value.mimeType || value.mime_type),
      );
      if (index >= 0) {
        const { data, ...rest } = value;
        return { ...rest, received_image: index };
      }
    }
    if (Array.isArray(value)) return value.map((v) => clean(v, depth + 1));
    const result: any = {};
    for (const [name, item] of Object.entries(value)) {
      if (
        name === "text" &&
        value.type === "text" &&
        typeof item === "string" &&
        item.trimStart().startsWith("{")
      ) {
        try {
          const parsed = JSON.parse(item);
          // Only normalize recognized MCP envelopes, never arbitrary textual JSON.
          result[name] =
            Array.isArray(parsed.content) && images.length
              ? JSON.stringify(clean(parsed, depth + 1))
              : item;
        } catch {
          result[name] = item;
        }
      } else result[name] = clean(item, depth + 1);
    }
    return result;
  };
  const content = clean(event.content);
  const structured = clean(event.structuredContent);
  const upstreamDetails = clean(event.details);
  const text = content
    .filter((v: any) => v.type === "text")
    .map((v: any) => v.text)
    .join("\n");
  return { images, content, structured, upstreamDetails, text };
}

export function registerResultDelivery(pi: any, allowed?: string[]) {
  if (process.env.ASSISTANT_CONTROL_TOKEN) return;
  const limit = Number(process.env.ASSISTANT_TOOL_RESULT_MAX_CHARS || 6000);
  const canRead = !Array.isArray(allowed) || allowed.includes("read_result");
  if (canRead)
    pi.registerTool({
      name: "read_result",
      label: "读取完整结果",
      description:
        "读取本执行获授权的结果引用；用于大工具返回或子任务报告。不会重新执行原操作。默认读取文本；JSON 使用 RFC6901 路径与分页。",
      parameters: {
        type: "object",
        required: ["id"],
        additionalProperties: false,
        properties: {
          id: { type: "string" },
          version: { type: "string" },
          format: { type: "string", enum: ["text", "json"] },
          path: { type: "string" },
          offset: { type: "integer", minimum: 0 },
          length: { type: "integer", minimum: 1, maximum: 6000 },
        },
      },
      executionMode: "parallel",
      async execute(_id: string, args: any) {
        try {
          const value = await host("/result-read", {
            action: "result_read",
            arguments: args,
          });
          return { content: [{ type: "text", text: JSON.stringify(value) }] };
        } catch (error) {
          return {
            isError: true,
            content: [{ type: "text", text: String(error) }],
          };
        }
      },
    });
  pi.on("tool_result", async (event: any) => {
    if (event.toolName === "read_result") return; // Never externalize a reader's own response.
    const details = object(event.details)
      ? { ...event.details }
      : event.details === undefined
        ? {}
        : { upstream_details: event.details };
    delete details[key];
    delete details[mediaKey];
    const p = prepared(event);
    const presentation = object(details["personal_agent.presentation"])
      ? details["personal_agent.presentation"]
      : {};
    const summary =
      typeof presentation.summary === "string" &&
      presentation.summary.trim() &&
      presentation.summary.length <= limit
        ? presentation.summary
        : undefined;
    const structuredText =
      p.structured === undefined ? "" : JSON.stringify(p.structured);
    const large = p.text.length + structuredText.length > limit;
    if ((!large || !canRead) && !p.images.length) {
      if (
        object(event.details) &&
        (key in event.details || mediaKey in event.details)
      )
        return { details };
      return;
    }
    let receipt: any;
    try {
      receipt = await host("/tool-result", {
        call_id: event.toolCallId,
        images: p.images,
        payload: {
          text: p.text,
          content: p.content,
          structured: p.structured,
          details: p.upstreamDetails,
          upstream_complete:
            typeof presentation.upstream_complete === "boolean"
              ? presentation.upstream_complete
              : null,
          tool: event.toolName,
          is_error: event.isError === true,
        },
      });
    } catch {
      receipt = {
        state: "failed",
        issues: [{ code: "result_delivery_unavailable" }],
        attachments: [],
      };
    }
    details[key] = receipt;
    if (receipt.media) details[mediaKey] = receipt.media;
    const ready =
      receipt.reference?.state === "ready" &&
      (!p.images.length || receipt.media?.state === "ready");
    // Keep the model projection small and stable; full delivery diagnostics remain in details.
    const projection = {
      state: receipt.state,
      business_is_error: event.isError === true,
      reference: receipt.reference,
      issues: receipt.issues,
      ...(receipt.media?.issues?.length
        ? { media_issues: receipt.media.issues }
        : {}),
    };
    const outcome =
      ownerOutcome(event.structuredContent) || ownerOutcome(details);
    const evidence = outcome
      ? "\n[Tool outcome] " + JSON.stringify(outcome)
      : "";
    const header = {
      type: "text",
      text:
        "[Framework result delivery] " +
        JSON.stringify(projection) +
        evidence +
        (ready
          ? " Received content retained; registered images are already attached. No additional save/export is needed." +
            (canRead
              ? " Use read_result with this reference for omitted content."
              : "")
          : " Retention is incomplete; original business outcome is unchanged. Do not repeat the operation to recover output."),
    };
    if (!ready || (!canRead && large))
      return { content: [header, ...event.content], details };
    const body =
      p.text +
      (structuredText && structuredText !== p.text
        ? "\n[Structured result]\n" + structuredText
        : "");
    const native = event.content.filter(
      (v: any) =>
        v.type !== "text" &&
        (v.type !== "image" ||
          !p.images.some(
            (i) =>
              i.data === v.data && i.mime_type === (v.mimeType || v.mime_type),
          )),
    );
    // Original image bytes remain native model inputs, once per received image.
    const images = p.images.map((i) => ({
      type: "image",
      mimeType: i.mime_type,
      data: i.data,
    }));
    return {
      content: [
        header,
        {
          type: "text",
          text: large
            ? summary
              ? "[工具提供的摘要，完整内容按引用读取]\n" + summary
              : "[节选，非完整结果]\n" + excerpt(body, limit)
            : body,
        },
        ...native,
        ...images,
      ],
      details,
      isError: event.isError,
      // Outcome evidence is small, owner-confirmed metadata, never lost with a large body.
      structuredContent: {
        result_ref: receipt.reference,
        ...(event.structuredContent?.tool_outcome
          ? { tool_outcome: event.structuredContent.tool_outcome }
          : {}),
      },
    };
  });
}
