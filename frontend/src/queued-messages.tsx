import { useEffect, useRef, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  Check,
  GripVertical,
  Image,
  Pencil,
  X,
} from "lucide-react";
import type { Attachment } from "./message-media";

export type QueuedRequest = {
  id: string;
  state: string;
  text: string;
  revision: string;
  attachments: Attachment[];
  message_id: string;
};
export type QueueAction = "edit_queued" | "remove_queued" | "reorder_queue";
type Editor = { original: QueuedRequest; text: string };

export function QueuedMessages({
  items,
  disabled,
  onAction,
  onRestore,
}: {
  items: QueuedRequest[];
  disabled: boolean;
  onAction: (
    action: QueueAction,
    args: Record<string, unknown>,
  ) => Promise<boolean>;
  onRestore: (text: string) => void;
}) {
  const [editor, setEditor] = useState<Editor | null>(null);
  const [saving, setSaving] = useState(false);
  const [dragging, setDragging] = useState<string | null>(null);
  const [drop, setDrop] = useState<{ id: string; before: boolean } | null>(
    null,
  );
  const field = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    if (editor) field.current?.focus();
  }, [editor?.original.id]);
  useEffect(() => {
    if (!field.current) return;
    field.current.style.height = "auto";
    field.current.style.height =
      Math.min(field.current.scrollHeight, 112) + "px";
  }, [editor?.text]);

  const current =
    editor && items.find((item) => item.id === editor.original.id);
  const stale =
    !!editor && (!current || current.revision !== editor.original.revision);
  const rows = editor && !current ? [...items, editor.original] : items;
  const locked = disabled || !!editor;
  const order = items.map((item) => item.id);
  async function save() {
    if (
      !editor ||
      disabled ||
      stale ||
      saving ||
      (!editor.text.trim() && !editor.original.attachments.length)
    )
      return;
    setSaving(true);
    try {
      if (
        await onAction("edit_queued", {
          request_id: editor.original.id,
          revision: editor.original.revision,
          text: editor.text.trim(),
        })
      )
        setEditor(null);
    } finally {
      setSaving(false);
    }
  }
  function reorder(next: string[]) {
    if (locked || next.every((id, index) => id === order[index])) return;
    void onAction("reorder_queue", {
      request_ids: next,
      expected_order: order,
    });
  }
  function move(index: number, offset: number) {
    const next = [...order];
    [next[index], next[index + offset]] = [next[index + offset], next[index]];
    reorder(next);
  }
  if (!rows.length) return null;
  return (
    <section className="composer-queue" aria-label="待发送消息">
      <ol aria-label="排队消息">
        {rows.map((item, index) => (
          <li
            key={item.id}
            className={
              "queue-row" +
              (editor?.original.id === item.id ? " is-editing" : "")
            }
            data-queue-id={item.id}
            data-dragging={dragging === item.id || undefined}
            data-drop={
              drop?.id === item.id
                ? drop.before
                  ? "before"
                  : "after"
                : undefined
            }
            onDragOver={(event) => {
              if (locked || !dragging || dragging === item.id) return;
              event.preventDefault();
              event.dataTransfer.dropEffect = "move";
              const rect = event.currentTarget.getBoundingClientRect();
              setDrop({
                id: item.id,
                before: event.clientY < rect.top + rect.height / 2,
              });
            }}
            onDrop={(event) => {
              event.preventDefault();
              if (locked || !dragging || !drop || dragging === item.id) return;
              const next = order.filter((id) => id !== dragging);
              next.splice(
                next.indexOf(item.id) + (drop.before ? 0 : 1),
                0,
                dragging,
              );
              setDragging(null);
              setDrop(null);
              reorder(next);
            }}
          >
            {editor?.original.id === item.id ? (
              <div className="queue-editor">
                <textarea
                  ref={field}
                  rows={1}
                  maxLength={32000}
                  aria-label="修改排队消息"
                  value={editor.text}
                  readOnly={saving}
                  onChange={(event) =>
                    setEditor({ ...editor, text: event.target.value })
                  }
                  onKeyDown={(event) => {
                    if (event.key === "Escape" && !saving) {
                      event.preventDefault();
                      setEditor(null);
                    }
                    if (
                      event.key === "Enter" &&
                      !event.shiftKey &&
                      !event.nativeEvent.isComposing
                    ) {
                      event.preventDefault();
                      void save();
                    }
                  }}
                />
                <div className="queue-editor-actions">
                  {stale ? (
                    <button
                      type="button"
                      className="queue-recover"
                      onClick={() => {
                        onRestore(editor.text);
                        setEditor(null);
                      }}
                    >
                      复制到输入框
                    </button>
                  ) : (
                    <button
                      type="button"
                      aria-label="保存排队消息修改"
                      title="保存修改 · Enter"
                      disabled={
                        disabled ||
                        saving ||
                        (!editor.text.trim() && !item.attachments.length)
                      }
                      onClick={() => void save()}
                    >
                      <Check size={15} />
                    </button>
                  )}
                  <button
                    type="button"
                    aria-label="取消编辑排队消息"
                    title="取消编辑 · Esc"
                    disabled={saving}
                    onClick={() => setEditor(null)}
                  >
                    <X size={15} />
                  </button>
                </div>
                {stale && (
                  <p role="status" className="queue-edit-note">
                    {current
                      ? "消息已在其他页面更新，当前修改尚未保存。"
                      : "消息已开始执行或已移出队列，当前修改尚未发送。"}
                  </p>
                )}
              </div>
            ) : (
              <>
                <button
                  type="button"
                  className="queue-grip"
                  aria-label={`拖动第 ${index + 1} 条排队消息`}
                  title="拖动调整顺序"
                  draggable={!locked}
                  disabled={locked}
                  onDragStart={(event) => {
                    setDragging(item.id);
                    event.dataTransfer.effectAllowed = "move";
                    event.dataTransfer.setData("text/plain", item.id);
                    const row =
                      event.currentTarget.closest<HTMLElement>(".queue-row");
                    if (row)
                      event.dataTransfer.setDragImage(
                        row,
                        20,
                        row.offsetHeight / 2,
                      );
                  }}
                  onDragEnd={() => {
                    setDragging(null);
                    setDrop(null);
                  }}
                >
                  <GripVertical size={14} />
                </button>
                <span className="queue-text" title={item.text}>
                  {item.text.trim() || "图片消息"}
                </span>
                {!!item.attachments.length && (
                  <span
                    className="queue-images"
                    title={`${item.attachments.length} 张图片`}
                  >
                    <Image size={13} aria-hidden="true" />
                    {item.attachments.length}
                  </span>
                )}
                <div className="queue-actions">
                  <button
                    type="button"
                    aria-label={`上移第 ${index + 1} 条排队消息`}
                    title="上移"
                    disabled={locked || index === 0}
                    onClick={() => move(index, -1)}
                  >
                    <ArrowUp size={14} />
                  </button>
                  <button
                    type="button"
                    aria-label={`下移第 ${index + 1} 条排队消息`}
                    title="下移"
                    disabled={locked || index === items.length - 1}
                    onClick={() => move(index, 1)}
                  >
                    <ArrowDown size={14} />
                  </button>
                  <button
                    type="button"
                    aria-label={`编辑第 ${index + 1} 条排队消息`}
                    title="编辑"
                    disabled={locked}
                    onClick={() =>
                      setEditor({ original: item, text: item.text })
                    }
                  >
                    <Pencil size={14} />
                  </button>
                  <button
                    type="button"
                    aria-label={`删除第 ${index + 1} 条排队消息`}
                    title="删除"
                    disabled={locked}
                    onClick={() =>
                      void onAction("remove_queued", {
                        request_id: item.id,
                        revision: item.revision,
                      })
                    }
                  >
                    <X size={15} />
                  </button>
                </div>
              </>
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}
