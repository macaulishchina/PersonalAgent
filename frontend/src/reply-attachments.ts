import type { Element, Root } from "hast";

declare module "hast" {
  interface ElementData {
    replyImage?: string;
    replyImageUnavailable?: boolean;
    replyGallery?: string[];
  }
}

type Reference = { id: string; type?: string };
const attachmentImage = /^attachment:([a-f0-9]{32})$/;

/** Work on parsed Markdown so code samples and plain bracketed text stay intact. */
export function bindReplyImages(
  attachments: readonly Reference[],
  inlineIds: ReadonlySet<string>,
) {
  const images = new Set(
    attachments
      .filter((a) => inlineIds.has(a.id) && a.type?.startsWith("image/"))
      .map((a) => a.id),
  );
  return (tree: Root) => {
    const placed = new Set<string>();
    const visit = (node: Root | Element, inLink = false) => {
      for (const child of node.children) {
        if (child.type !== "element") continue;
        if (child.tagName === "img") {
          const src = child.properties.src;
          const id = typeof src === "string" && attachmentImage.exec(src)?.[1];
          if (!id) continue;
          if (!images.has(id)) {
            child.data = { ...child.data, replyImageUnavailable: true };
          } else if (!inLink && !placed.has(id)) {
            placed.add(id);
            child.data = { ...child.data, replyImage: id };
          }
        } else {
          visit(child, inLink || child.tagName === "a");
        }
      }
    };
    visit(tree);
    const remaining = attachments.filter((a) => !placed.has(a.id));
    if (remaining.length) {
      tree.children.push({
        type: "element",
        tagName: "div",
        properties: {},
        children: [],
        data: { replyGallery: remaining.map((a) => a.id) },
      });
    }
  };
}
