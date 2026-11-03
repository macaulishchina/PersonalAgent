/** Retain received images through the host, never through another tool call. */
import { createHash } from "node:crypto";

export function imagesIn(event: any) {
  const images: any[] = [];
  const seen = new Set<string>();
  const visit = (value: any, depth = 0) => {
    if (!value || typeof value !== "object" || depth > 6) return;
    if (value.type === "image" && typeof value.data === "string") {
      const mime = value.mimeType || value.mime_type;
      const key = createHash("sha256")
        .update(String(mime))
        .update(value.data)
        .digest("hex");
      if (!seen.has(key) && images.length < 4) {
        seen.add(key);
        images.push({ data: value.data, mime_type: mime });
      }
    } else if (
      value.type === "text" &&
      typeof value.text === "string" &&
      value.text.length <= 16 * 1024 * 1024 &&
      value.text.trimStart().startsWith("{")
    ) {
      try {
        visit(JSON.parse(value.text), depth + 1);
      } catch {
        // Ordinary text is not a result envelope.
      }
    }
    if (Array.isArray(value.content))
      for (const block of value.content.slice(0, 32)) visit(block, depth + 1);
    visit(value.structuredContent, depth + 1);
  };
  visit(event);
  return images;
}
