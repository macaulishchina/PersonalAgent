import { useId, useLayoutEffect, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

const components: Components = {
  a: ({ children, href }) => {
    if (!href || !/^https?:\/\//i.test(href)) return <span>{children}</span>;
    let url: URL;
    try {
      url = new URL(href);
    } catch {
      return <span>{children}</span>;
    }
    const bare =
      children === href || children === href.replace(/^https?:\/\//i, "");
    return (
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        title={bare ? href : undefined}
      >
        {bare ? url.host : children}
      </a>
    );
  },
  img: ({ alt }) => <span>{alt || "图片"}</span>,
};

export function NotificationBody({ text }: { text: string }) {
  const id = useId();
  const viewport = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [clipped, setClipped] = useState(false);

  useLayoutEffect(() => {
    const box = viewport.current;
    const body = content.current;
    if (expanded || !box || !body) return;
    const measure = () => {
      if (box.clientHeight) setClipped(box.scrollHeight > box.clientHeight);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(box);
    observer.observe(body);
    return () => observer.disconnect();
  }, [expanded, text]);

  return (
    <>
      <div
        id={id}
        ref={viewport}
        className="notification-body"
        data-preview={!expanded}
        inert={!expanded && clipped}
      >
        <div className="message-body notification-markdown" ref={content}>
          <Markdown
            remarkPlugins={[remarkGfm]}
            components={components}
            skipHtml
          >
            {text}
          </Markdown>
        </div>
      </div>
      {(clipped || expanded) && (
        <button
          type="button"
          className="notification-body-toggle"
          aria-expanded={expanded}
          aria-controls={id}
          onClick={() => setExpanded(!expanded)}
        >
          <ChevronDown size={13} aria-hidden="true" />
          {expanded ? "收起正文" : "展开正文"}
        </button>
      )}
    </>
  );
}
