import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { MessageImage, MessageMedia, type Attachment } from "./message-media";
import { bindReplyImages } from "./reply-attachments";

export function ReplyMarkdown({
  text,
  attachments,
  inlineIds,
  onOpen,
  components,
}: {
  text: string;
  attachments: Attachment[];
  inlineIds: ReadonlySet<string>;
  onOpen: (url: string) => void;
  components: Components;
}) {
  const byId = new Map(attachments.map((a) => [a.id, a]));
  return (
    <Markdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[() => bindReplyImages(attachments, inlineIds)]}
      components={{
        ...components,
        img: ({ alt, node }) => {
          const id = node?.data?.replyImage;
          const image = typeof id === "string" ? byId.get(id) : undefined;
          if (image)
            return (
              <MessageImage
                attachment={image}
                caption={alt}
                className="reply-image"
                onOpen={onOpen}
              />
            );
          if (node?.data?.replyImageUnavailable)
            return <span>{alt ? `${alt}（图片不可用）` : "图片不可用"}</span>;
          return <span>[{alt || "外部图片"}]</span>;
        },
        div: ({ node, ...props }) => {
          const gallery = node?.data?.replyGallery;
          if (!gallery) return <div {...props} />;
          return (
            <MessageMedia
              attachments={gallery.flatMap((id) => byId.get(id) || [])}
              label="回复媒体"
              onOpen={onOpen}
            />
          );
        },
      }}
    >
      {text}
    </Markdown>
  );
}
