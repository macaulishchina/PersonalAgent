import { chatPath } from "./http";
import { useLayoutEffect, useRef } from "react";
import { X } from "lucide-react";

export type Attachment = {
  id: string;
  name: string;
  type?: string;
  url?: string;
};

export function ImagePreview({
  url,
  onClose,
}: {
  url: string;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useLayoutEffect(() => {
    const element = dialog.current!;
    element.showModal();
    return () => element.close();
  }, []);
  return (
    <dialog
      ref={dialog}
      className="lightbox"
      aria-label="查看图片"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape") event.stopPropagation();
      }}
      onPointerDown={(event) => event.stopPropagation()}
      onClick={onClose}
    >
      <button aria-label="关闭图片" onClick={onClose}>
        <X size={24} />
      </button>
      <img
        src={url}
        alt="图片预览"
        onClick={(event) => event.stopPropagation()}
      />
    </dialog>
  );
}

export function MessageMedia({
  attachments,
  onOpen,
}: {
  attachments: Attachment[];
  onOpen: (url: string) => void;
}) {
  if (!attachments.length) return null;
  return (
    <div className="message-media">
      {attachments.map((a) =>
        a.type === "video/mp4" ? (
          <figure className="message-video" key={a.id}>
            <video
              src={chatPath("api/uploads/" + a.id)}
              controls
              playsInline
              preload="metadata"
              aria-label={a.name || "摄像头录像"}
            />
            <figcaption>
              <span>{a.name}</span>
              <a href={chatPath("api/uploads/" + a.id)} download={a.name}>
                下载
              </a>
            </figcaption>
          </figure>
        ) : (
          <button
            key={a.id}
            aria-label={"查看图片 " + a.name}
            onClick={() => onOpen(chatPath("api/uploads/" + a.id))}
          >
            <img src={chatPath("api/uploads/" + a.id)} alt={a.name} loading="lazy" />
          </button>
        ),
      )}
    </div>
  );
}
