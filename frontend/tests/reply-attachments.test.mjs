import test from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import Markdown from "react-markdown";
import { bindReplyImages } from "../src/reply-attachments.ts";

const image = (key) => ({ id: key.repeat(32), type: "image/jpeg" });
const link = (id, caption) => `![${caption}](attachment:${id})`;

function render(
  text,
  attachments,
  inlineIds = new Set(attachments.map((a) => a.id)),
) {
  const placed = [];
  const galleries = [];
  const unavailable = [];
  const html = renderToStaticMarkup(
    createElement(Markdown, {
      children: text,
      rehypePlugins: [() => bindReplyImages(attachments, inlineIds)],
      components: {
        img: ({ alt, node }) => {
          if (node.data?.replyImage) placed.push([node.data.replyImage, alt]);
          if (node.data?.replyImageUnavailable) unavailable.push(alt);
          return alt;
        },
        div: ({ node }) => {
          galleries.push(node.data.replyGallery);
          return null;
        },
      },
    }),
  );
  return { placed, galleries, unavailable, html };
}

test("registered images follow the Markdown order and are removed from the gallery", () => {
  const [a, b, c] = ["a", "b", "c"].map(image);
  const video = { id: "d".repeat(32), type: "video/mp4" };
  const result = render(
    [
      "**第二张**",
      link(b.id, "细节"),
      "**第一张**",
      link(a.id, "概览"),
      link(b.id, "重复"),
    ].join("\n\n"),
    [a, b, c, video],
  );
  assert.deepEqual(result.placed, [
    [b.id, "细节"],
    [a.id, "概览"],
  ]);
  assert.deepEqual(result.galleries, [[c.id, video.id]]);
  assert.deepEqual(result.unavailable, []);
});

test("only exact image references inside the same reply can bind to attachments", () => {
  const a = image("a");
  const other = image("b");
  const video = { id: "c".repeat(32), type: "video/mp4" };
  const syntax = link(a.id, "代码示例");
  const result = render(
    [
      "```markdown\n" + syntax + "\n```",
      "![远程图](https://invalid.example/picture.jpg)",
      "[" + link(a.id, "链接内的图片") + "](https://invalid.example/)",
      link(other.id, "另一轮图片"),
      link(video.id, "视频引用"),
      "[普通文字]",
    ].join("\n\n"),
    [a, video],
  );
  assert.deepEqual(result.placed, []);
  assert.deepEqual(result.galleries, [[a.id, video.id]]);
  assert.deepEqual(result.unavailable, ["另一轮图片", "视频引用"]);
  assert.ok(result.html.includes(syntax));
  assert.ok(result.html.includes("[普通文字]"));
});

test("an attachment arriving after the reply binds without duplicating the gallery", () => {
  const a = image("a");
  const body = link(a.id, "晚到图片");
  assert.deepEqual(render(body, []).unavailable, ["晚到图片"]);
  const result = render(body, [a]);
  assert.deepEqual(result.placed, [[a.id, "晚到图片"]]);
  assert.deepEqual(result.galleries, []);
});

test("gallery visibility alone does not authorize inline placement", () => {
  const legacy = image("a");
  const result = render(link(legacy.id, "没有轮次归属"), [legacy], new Set());
  assert.deepEqual(result.placed, []);
  assert.deepEqual(result.unavailable, ["没有轮次归属"]);
  assert.deepEqual(result.galleries, [[legacy.id]]);
});
