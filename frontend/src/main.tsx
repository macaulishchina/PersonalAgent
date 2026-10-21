import React, { useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ArrowUp,
  ArrowDown,
  Plus,
  X,
  Copy,
  Check,
  AudioLines,
  ChevronDown,
  Square,
  LogOut,
  ListTodo,
  Bell,
  Quote,
  LoaderCircle,
  WifiOff,
  MessageSquare,
} from "lucide-react";
import "./style.css";
import { ActivityPanel, type PanelSection } from "./activity";
import { MessageMedia, ImagePreview, type Attachment } from "./message-media";
import { ToolResult, toolOutcome, type ToolSummary } from "./tool-result";
import {
  isReport,
  messageChannel,
  needsAttention,
  reportTask,
  groupMessages,
  type MessageOrigin,
} from "./message-presentation";
import { TaskReport, ReportCollection } from "./task-report";

type Message = MessageOrigin & {
  id: string;
  ordinal: number;
  role: string;
  source: string;
  kind: string;
  text: string;
  status: string;
  attachments: Attachment[];
  updated: number;
  turn_id?: string | null;
  tool_summary?: ToolSummary | null;
  task_id?: string | null;
};
type MessageGroup = {
  id: string;
  role: string;
  turn_id?: string | null;
  messages: Message[];
};
type State = {
  name: string;
  voice: string;
  speaking: boolean;
  task_running: boolean;
  quiet: boolean;
  session_id: string;
  web_dispatching: boolean;
  notifications_unread?: number;
  sync_error?: string;
};
const initial: State = {
  name: "个人助理",
  voice: "standby",
  speaking: false,
  task_running: false,
  quiet: false,
  session_id: "",
  web_dispatching: false,
};
class LoginRequired extends Error {}
async function api(path: string, options: RequestInit = {}) {
  const r = await fetch("/api/" + path, {
    ...options,
    credentials: "same-origin",
    headers:
      options.body instanceof FormData
        ? options.headers
        : { "Content-Type": "application/json", ...options.headers },
  });
  if (r.status === 401 && path !== "login") throw new LoginRequired();
  const raw = await r.text();
  let d;
  try {
    d = JSON.parse(raw);
  } catch {
    d = { error: raw };
  }
  if (!r.ok) throw new Error(d.error || "连接暂时不可用");
  return d;
}
function nonce() {
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 15) | 64;
  b[8] = (b[8] & 63) | 128;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}
function Mark() {
  return (
    <span className="brand-mark" aria-hidden="true">
      <i />
      <i />
    </span>
  );
}
async function clipboard(text: string) {
  if (navigator.clipboard) await navigator.clipboard.writeText(text);
  else {
    const t = document.createElement("textarea");
    t.value = text;
    document.body.appendChild(t);
    t.select();
    const ok = document.execCommand("copy");
    t.remove();
    if (!ok) throw new Error("复制失败");
  }
}
function textOf(node: React.ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (React.isValidElement<{ children?: React.ReactNode }>(node))
    return textOf(node.props.children);
  return "";
}
function CodeBlock({ children }: React.ComponentProps<"pre">) {
  const [label, setLabel] = useState("复制代码");
  return (
    <div className="code-block">
      <button
        type="button"
        onClick={async () => {
          try {
            await clipboard(textOf(children));
            setLabel("已复制");
          } catch {
            setLabel("请选择代码复制");
          }
        }}
      >
        {label}
      </button>
      <pre>{children}</pre>
    </div>
  );
}
const markdownComponents = {
  pre: CodeBlock,
  a: ({ children, href }: React.ComponentProps<"a">) => (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  ),
  img: ({ alt }: React.ComponentProps<"img">) => (
    <span>[{alt || "外部图片"}]</span>
  ),
};
const toolLabels: Record<string, string> = {
  device: "设备操作",
  memory: "记忆操作",
  search_memory: "查询记忆",
  read_memory: "读取记忆",
  remember_memory: "保存记忆",
  update_memory: "更新记忆",
  forget_memory: "遗忘记忆",
  list_devices: "查看设备",
  inspect_device: "检查设备状态",
  capture_image: "获取画面",
  record_video: "录制视频",
  move_camera: "调整摄像头方向",
  stop_camera: "停止摄像头转向",
  select_audio_output: "切换音箱",
  set_audio_volume: "调整音量",
  stop_playback: "停止播报",
  end_voice_conversation: "结束语音对话",
  tasks: "后台任务",
  list_mcp_servers: "查看 MCP 接入",
  inspect_mcp_server: "查看 MCP 配置",
  configure_mcp_server: "配置 MCP",
  set_mcp_server_enabled: "调整 MCP 启用状态",
  remove_mcp_server: "移除 MCP 接入",
  verify_mcp_server: "验证 MCP 连接",
  apply_mcp_configuration: "应用 MCP 配置",
  reload_mcp_configuration: "读取 MCP 配置",
  begin_mcp_login: "开始 MCP 授权",
  publish_notification: "发送通知",
  update_notification: "更新通知",
  list_notifications: "查看通知",
  get_notification: "读取通知",
  mark_notification_read: "标记通知已读",
  create_task: "创建后台任务",
  list_tasks: "查看任务",
  get_task: "查看任务进度",
  list_task_models: "查看可用模型",
  fetch_webpage: "读取网页",
  supplement_task: "补充任务要求",
  cancel_task_run: "取消本次执行",
  pause_task: "暂停任务计划",
  resume_task: "恢复任务计划",
};
function messageDetail(message: Message) {
  if (message.kind !== "detail") return null;
  const [heading, ...lines] = message.text.trim().split("\n");
  if (heading === "调用工具") {
    const [name, ...body] = lines;
    return {
      name: name || heading,
      label: toolLabels[name] || (name ? `调用 ${name}` : heading),
      body: body.join("\n").trim(),
    };
  }
  if (["执行命令", "修改文件", "检索资料"].includes(heading)) {
    return { name: heading, label: heading, body: lines.join("\n").trim() };
  }
  // Earlier voice-task replies were stored as details despite being plain prose.
  return null;
}
function ToolActivity({
  message,
  name,
  label,
}: {
  message: Message;
  name: string;
  label: string;
}) {
  const busy = message.status === "streaming";
  const failed = message.status === "failed";
  const labels: Record<string, string> = {
    streaming: "进行中",
    complete: "完成",
    failed: "失败",
    interrupted: "已取消",
    unknown: "未确认",
  };
  const status =
    message.tool_summary && ["complete", "failed"].includes(message.status)
      ? toolOutcome(message.tool_summary)
      : labels[message.status] || "未确认";
  return (
    <span className={"tool-activity" + (failed ? " failed" : "")} title={name}>
      <span
        className={"task-indicator" + (busy ? " busy" : "")}
        aria-hidden="true"
      />
      <span className="tool-label">{label}</span>
      <span className="tool-status">{status}</span>
    </span>
  );
}
function MessageEntry({
  message: m,
  onOpen,
  onOpenTask,
  onResolvedTask,
  showReportSource = false,
}: {
  message: Message;
  onOpen: (url: string) => void;
  onOpenTask: (id: string) => void;
  onResolvedTask: (id: string) => void;
  showReportSource?: boolean;
}) {
  const detail = messageDetail(m);
  const statuses: Record<string, string> = {
    sending: "正在发送",
    unknown: "状态待确认",
    interrupted: "已取消",
    failed: "未完成",
  };
  return (
    <article
      className={
        "message " +
        m.role +
        (isReport(m) ? " report-message" : "") +
        (detail ? " detail" + (!detail.body ? " simple-tool" : "") : "")
      }
      data-message-id={m.id}
      aria-label={
        detail ? detail.label : m.role === "user" ? "你的消息" : "助手消息"
      }
    >
      {isReport(m) ? (
        <TaskReport
          message={m}
          showSource={showReportSource}
          onOpenTask={onOpenTask}
          onResolvedTask={onResolvedTask}
          loadNotice={(id) =>
            api("notifications", {
              method: "POST",
              body: JSON.stringify({ action: "get", arguments: { id } }),
            })
          }
          renderBody={(text) => (
            <div className="message-body">
              <Markdown
                remarkPlugins={[remarkGfm]}
                components={markdownComponents}
              >
                {text}
              </Markdown>
            </div>
          )}
        >
          <MessageMedia attachments={m.attachments} onOpen={onOpen} />
        </TaskReport>
      ) : detail ? (
        <>
          {!detail.body ? (
            <ToolActivity message={m} name={detail.name} label={detail.label} />
          ) : (
            <details>
              <summary>
                <ToolActivity
                  message={m}
                  name={detail.name}
                  label={detail.label}
                />
                <ChevronDown size={14} />
              </summary>
              <div className="detail-body">
                <Markdown
                  remarkPlugins={[remarkGfm]}
                  components={markdownComponents}
                >
                  {detail.body}
                </Markdown>
              </div>
            </details>
          )}
          {m.tool_summary && (
            <ToolResult value={m.tool_summary} onOpenTask={onOpenTask} />
          )}
          <MessageMedia attachments={m.attachments} onOpen={onOpen} />
        </>
      ) : (
        <>
          <MessageMedia attachments={m.attachments} onOpen={onOpen} />
          <div className="message-body">
            <Markdown
              remarkPlugins={[remarkGfm]}
              components={markdownComponents}
            >
              {m.text}
            </Markdown>
            {m.status === "streaming" && !m.text && (
              <span className="thinking">
                正在思考<span>···</span>
              </span>
            )}
          </div>
          {statuses[m.status] && (
            <div
              className={
                "message-status" +
                (["unknown", "failed"].includes(m.status) ? " error" : "")
              }
              role="status"
            >
              {statuses[m.status]}
            </div>
          )}
        </>
      )}
    </article>
  );
}
function ConversationGroup({
  group,
  name,
  copied,
  onCopy,
  onOpen,
  onOpenTask,
}: {
  group: MessageGroup;
  name: string;
  copied: string;
  onCopy: (id: string, text: string) => Promise<void>;
  onOpen: (url: string) => void;
  onOpenTask: (id: string) => void;
}) {
  const user = group.role === "user";
  const [resolvedTask, setResolvedTask] = useState<string | null>(null);
  const latest = isReport(group.messages[0])
    ? group.messages.at(-1)!
    : group.messages[0];
  const channel = group.messages.some(needsAttention)
    ? "attention"
    : !latest.report_group?.kind && (resolvedTask || reportTask(latest))
      ? "report"
      : messageChannel(latest);
  const speaker = user ? "你" : name;
  const date = new Date(latest.updated * 1000);
  const text = group.messages
    .filter((m) => !messageDetail(m))
    .map((m) => m.text)
    .filter(Boolean)
    .join("\n\n");
  const entries = group.messages.map((m) => (
    <MessageEntry
      key={m.id}
      message={m}
      onOpen={onOpen}
      onOpenTask={onOpenTask}
      onResolvedTask={setResolvedTask}
      showReportSource={group.messages.length === 1}
    />
  ));
  return (
    <section
      className={"message-group " + group.role}
      aria-label={speaker + "的消息"}
      data-speaker={group.role}
    >
      <div className="message-meta">
        <span className="speaker">{speaker}</span>
        {!user && (
          <span
            className="source-badge"
            title={
              channel === "voice" ? "语音回复，可能因插话而中断" : undefined
            }
          >
            {channel === "voice" ? (
              <AudioLines size={12} />
            ) : channel === "report" ? (
              <ListTodo size={12} />
            ) : channel === "notification" || channel === "attention" ? (
              <Bell size={12} />
            ) : (
              <MessageSquare size={12} />
            )}
            {
              {
                voice: "语音",
                text: "文字",
                report: "任务报告",
                notification: "通知",
                attention: "需要关注",
                legacy: "历史记录",
              }[channel]
            }
          </span>
        )}
        <time
          dateTime={date.toISOString()}
          title={date.toLocaleString("zh-CN")}
        >
          {date.toLocaleDateString() === new Date().toLocaleDateString()
            ? date.toLocaleTimeString("zh-CN", {
                hour: "2-digit",
                minute: "2-digit",
              })
            : date.toLocaleString("zh-CN", {
                month: "2-digit",
                day: "2-digit",
                hour: "2-digit",
                minute: "2-digit",
              })}
        </time>
        {user && group.messages.every((m) => m.source === "voice") && (
          <span className="source-badge" title="通过语音发送">
            <AudioLines size={12} />
            语音
          </span>
        )}
        {user && group.messages.every((m) => m.kind === "legacy") && (
          <span className="source-badge">历史记录</span>
        )}
        {!user && text && (
          <button
            className="copy"
            type="button"
            aria-label="复制回复"
            title={copied === group.id ? "已复制" : "复制这组回复"}
            onClick={() => onCopy(group.id, text)}
          >
            {copied === group.id ? <Check size={14} /> : <Copy size={14} />}
          </button>
        )}
      </div>
      {group.messages.length > 1 && isReport(group.messages[0]) ? (
        <ReportCollection messages={group.messages} onOpenTask={onOpenTask}>
          {entries}
        </ReportCollection>
      ) : (
        entries
      )}
    </section>
  );
}
function ResponseActivity({
  name,
  label,
  disconnected,
}: {
  name: string;
  label: string;
  disconnected: boolean;
}) {
  return (
    <section
      className="message-group assistant response-activity"
      aria-label="助手响应状态"
    >
      <div className="message-meta">
        <span className="speaker">{name}</span>
      </div>
      <div
        className="response-progress"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        {disconnected ? (
          <WifiOff size={15} aria-hidden="true" />
        ) : (
          <span className="response-dots" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
        )}
        <span>{label}</span>
      </div>
    </section>
  );
}
function App() {
  const [activePanel, setActivePanel] = useState<PanelSection | null>(null);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [notificationContext, setNotificationContext] = useState<{
    id: string;
    title: string;
  } | null>(null);
  const [authorized, setAuthorized] = useState<boolean | null>(null),
    [token, setToken] = useState(""),
    [error, setError] = useState("");
  const [state, setState] = useState<State>(initial),
    [messages, setMessages] = useState<Message[]>([]),
    [online, setOnline] = useState(false);
  const [before, setBefore] = useState<number | null>(null),
    [draft, setDraft] = useState(
      () => sessionStorage.getItem("pa-draft") || "",
    );
  const [attachments, setAttachments] = useState<Attachment[]>([]),
    [sending, setSending] = useState(false),
    [uploading, setUploading] = useState(false);
  const [zoom, setZoom] = useState<string | null>(null),
    [atBottom, setAtBottom] = useState(true),
    [copied, setCopied] = useState("");
  const events = useRef<EventSource | null>(null),
    cursor = useRef(0),
    bottom = useRef<HTMLDivElement>(null),
    input = useRef<HTMLTextAreaElement>(null),
    file = useRef<HTMLInputElement>(null),
    composer = useRef<HTMLDivElement>(null);
  const following = useRef(true),
    starting = useRef(false),
    stopped = useRef(false),
    generation = useRef(0);
  // Derive activity from transport/provider state; never persist a fake reply.
  const latestUser = messages
    .filter((message) => message.role === "user")
    .at(-1);
  const pendingDispatch = latestUser?.status === "sending";
  const foregroundBusy =
    sending || pendingDispatch || state.web_dispatching || state.task_running;
  const streamingReply = messages.some(
    (message) =>
      message.role === "assistant" &&
      message.kind === "message" &&
      message.status === "streaming" &&
      message.text.trim() &&
      message.ordinal > (latestUser?.ordinal ?? -1),
  );
  const responseLabel = !foregroundBusy
    ? ""
    : !online
      ? "连接已断开，正在重新连接…"
      : sending
        ? "正在发送…"
        : pendingDispatch || state.web_dispatching
          ? "正在准备回复…"
          : streamingReply
            ? ""
            : "正在处理…";
  const merge = useCallback(
    (items: Message[]) =>
      setMessages((prev) => {
        const map = new Map(prev.map((m) => [m.id, m]));
        for (const m of items) if (m) map.set(m.id, m);
        return [...map.values()].sort((a, b) => a.ordinal - b.ordinal);
      }),
    [],
  );
  const fail = (e: unknown) => {
    if (e instanceof LoginRequired) {
      setAuthorized(false);
      events.current?.close();
    } else setError(e instanceof Error ? e.message : "连接暂时不可用");
  };
  const connect = useCallback(async () => {
    if (starting.current || stopped.current) return;
    starting.current = true;
    const own = ++generation.current;
    try {
      const d = await api("messages");
      if (stopped.current || own !== generation.current) return;
      setAuthorized(true);
      setState(d.state);
      setMessages(d.messages);
      setBefore(d.before);
      cursor.current = d.cursor;
      events.current?.close();
      const stream = new EventSource("/api/events?after=" + cursor.current);
      events.current = stream;
      stream.onopen = () => {
        if (own === generation.current) {
          setOnline(true);
          setError("");
        }
      };
      stream.addEventListener("update", (e) => {
        if (own !== generation.current) return;
        const v = JSON.parse((e as MessageEvent).data);
        cursor.current = v.seq;
        if (v.kind === "message") merge([v.data]);
        else if (v.kind === "state") setState(v.data);
        else if (v.kind === "notice") setError(v.data.text);
      });
      stream.addEventListener("reset", () => {
        stream.close();
        setOnline(false);
        void connect();
      });
      stream.onerror = () => {
        setOnline(false);
        void api("session").catch((e) => {
          if (e instanceof LoginRequired) {
            stream.close();
            setAuthorized(false);
          }
        });
      };
    } catch (e) {
      fail(e);
    } finally {
      starting.current = false;
    }
  }, [merge]);
  useEffect(() => {
    stopped.current = false;
    void connect();
    return () => {
      stopped.current = true;
      generation.current++;
      events.current?.close();
    };
  }, [connect]);
  useEffect(() => {
    sessionStorage.setItem("pa-draft", draft);
    if (input.current) {
      input.current.style.height = "auto";
      input.current.style.height =
        Math.min(input.current.scrollHeight, 168) + "px";
    }
  }, [draft]);
  useEffect(() => {
    if (following.current)
      bottom.current?.scrollIntoView({ behavior: "instant" });
  }, [messages, responseLabel]);
  useEffect(() => {
    if (!authorized || !composer.current) return;
    let frame = 0;
    const observer = new ResizeObserver(([entry]) => {
      document.documentElement.style.setProperty(
        "--composer-height",
        `${entry.target.getBoundingClientRect().height}px`,
      );
      cancelAnimationFrame(frame);
      if (following.current)
        frame = requestAnimationFrame(() =>
          bottom.current?.scrollIntoView({ behavior: "instant" }),
        );
    });
    observer.observe(composer.current);
    return () => {
      observer.disconnect();
      cancelAnimationFrame(frame);
      document.documentElement.style.removeProperty("--composer-height");
    };
  }, [authorized]);
  useEffect(() => {
    const scroll = () => {
      const near =
        document.documentElement.scrollHeight -
          window.scrollY -
          window.innerHeight <
        180;
      following.current = near;
      setAtBottom(near);
    };
    window.addEventListener("scroll", scroll, { passive: true });
    return () => window.removeEventListener("scroll", scroll);
  }, []);
  async function login(e: React.FormEvent) {
    e.preventDefault();
    setSending(true);
    try {
      await api("login", { method: "POST", body: JSON.stringify({ token }) });
      setToken("");
      setError("");
      await connect();
    } catch (e) {
      fail(e);
    } finally {
      setSending(false);
    }
  }
  async function upload(files: FileList | File[] | null) {
    if (!files || uploading) return;
    setUploading(true);
    setError("");
    try {
      if (attachments.length + files.length > 4)
        throw new Error("每次最多添加四张图片");
      const added: Attachment[] = [];
      for (const f of Array.from(files)) {
        if (f.size > 10 * 1024 * 1024)
          throw new Error("单张图片不能超过 10 MB");
        const form = new FormData();
        form.append("image", f);
        added.push(await api("uploads", { method: "POST", body: form }));
      }
      setAttachments((old) => [...old, ...added]);
    } catch (e) {
      fail(e);
    } finally {
      setUploading(false);
      if (file.current) file.current.value = "";
    }
  }
  async function send(e?: React.FormEvent) {
    e?.preventDefault();
    if (
      sending ||
      uploading ||
      !online ||
      (!draft.trim() && !attachments.length)
    )
      return;
    setSending(true);
    following.current = true;
    setError("");
    const body = {
      request_id: nonce(),
      text: draft.trim(),
      attachments: attachments.map((a) => a.id),
      ...(notificationContext
        ? { notification_id: notificationContext.id }
        : {}),
    };
    const cached = sessionStorage.getItem("pa-outbox");
    if (cached) {
      const old = JSON.parse(cached);
      if (
        old.text === body.text &&
        old.notification_id === body.notification_id &&
        JSON.stringify(old.attachments) === JSON.stringify(body.attachments)
      )
        body.request_id = old.request_id;
    }
    sessionStorage.setItem("pa-outbox", JSON.stringify(body));
    try {
      const d = await api("messages", {
        method: "POST",
        body: JSON.stringify(body),
      });
      merge([d.message]);
      if (d.state === "unknown") {
        setError("这条消息的执行状态尚未确认，请核对任务结果，不要重复交办。");
      } else {
        setDraft((current) => (current.trim() === body.text ? "" : current));
        setAttachments([]);
        setNotificationContext(null);
        sessionStorage.removeItem("pa-outbox");
        following.current = true;
        bottom.current?.scrollIntoView({ behavior: "smooth" });
      }
    } catch (e) {
      fail(e);
    } finally {
      setSending(false);
      input.current?.focus();
    }
  }
  async function older() {
    if (!before) return;
    try {
      const h = document.documentElement.scrollHeight;
      const y = window.scrollY;
      const d = await api("messages?before=" + before);
      merge(d.messages);
      setBefore(d.before);
      requestAnimationFrame(() =>
        window.scrollTo(0, y + document.documentElement.scrollHeight - h),
      );
    } catch (e) {
      fail(e);
    }
  }
  async function control(action: string) {
    try {
      setState(
        await api("control", {
          method: "POST",
          body: JSON.stringify({ action }),
        }),
      );
    } catch (e) {
      fail(e);
    }
  }
  async function copy(id: string, text: string) {
    try {
      await clipboard(text);
      setCopied(id);
      setTimeout(() => setCopied(""), 1500);
    } catch {
      setError("复制失败，请选择文字复制");
    }
  }
  const status = !online
    ? "连接已断开"
    : state.task_running
      ? "正在处理"
      : state.speaking
        ? "正在说话"
        : state.voice === "active"
          ? "语音已连接"
          : state.quiet
            ? "语音静音中"
            : "已连接";
  return (
    <>
      <header className="header">
        <a className="brand" href="/" aria-label="Personal Agent">
          <Mark />
          <span>Personal Agent</span>
        </a>
        <div className="header-right">
          {authorized && (
            <>
              <span className={"connection " + (!online ? "disconnected" : "")}>
                <span className="dot" />
                {status}
              </span>
              <button
                className="icon-button"
                title="退出登录"
                aria-label="退出登录"
                onClick={async () => {
                  await api("logout", { method: "POST", body: "{}" });
                  events.current?.close();
                  setAuthorized(false);
                  setMessages([]);
                }}
              >
                <LogOut size={17} />
              </button>
            </>
          )}
        </div>
      </header>
      {authorized && (
        <ActivityPanel
          section={activePanel}
          selectedTaskId={selectedTaskId}
          onSelectTask={setSelectedTaskId}
          onClose={() => setActivePanel(null)}
          unreadCount={state.notifications_unread || 0}
          api={api}
          nonce={nonce}
          onOpenMedia={setZoom}
          onReference={(notice) => {
            setNotificationContext(notice);
            window.setTimeout(() => input.current?.focus(), 180);
          }}
        />
      )}
      {authorized === null ? (
        <main className="welcome">
          <LoaderCircle className="spin" />
          <p>正在连接你的助手…</p>
          {error && (
            <p role="alert">
              {error}
              <button onClick={() => connect()}>重新连接</button>
            </p>
          )}
        </main>
      ) : !authorized ? (
        <main className="welcome login">
          <Mark />
          <h1>连接你的 Personal Agent</h1>
          <p>同一个助手，同一段对话。</p>
          <form onSubmit={login}>
            <label htmlFor="access">访问口令</label>
            <input
              id="access"
              type="password"
              autoComplete="current-password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="输入服务器上的访问口令"
              required
              autoFocus
            />
            <button className="primary" disabled={sending}>
              {sending ? "正在连接…" : "进入对话"}
              <ArrowUp size={17} />
            </button>
          </form>
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
        </main>
      ) : (
        <>
          <main className="conversation" aria-label="共同会话">
            {before && (
              <button className="older" onClick={older}>
                查看更早的消息
              </button>
            )}
            {messages.length === 0 && (
              <div className="welcome empty">
                <Mark />
                <h1>从这里，继续对话。</h1>
                <p>语音和文字，共享同一段对话。</p>
              </div>
            )}
            {groupMessages(messages).map((group) => (
              <ConversationGroup
                key={group.id}
                group={group}
                name={state.name}
                copied={copied}
                onCopy={copy}
                onOpen={setZoom}
                onOpenTask={(id) => {
                  setSelectedTaskId(id);
                  setActivePanel("tasks");
                }}
              />
            ))}
            {responseLabel && (
              <ResponseActivity
                name={state.name}
                label={responseLabel}
                disconnected={!online}
              />
            )}
            <div ref={bottom} />
          </main>
          <div className="composer-wrap" ref={composer}>
            {!atBottom && (
              <button
                className="jump"
                aria-label="回到最新消息"
                onClick={() => {
                  following.current = true;
                  bottom.current?.scrollIntoView({ behavior: "smooth" });
                }}
              >
                <ArrowDown size={18} />
              </button>
            )}
            <div className="controls">
              {state.speaking && (
                <button onClick={() => control("stop")}>
                  <Square size={12} />
                  停止播报
                </button>
              )}
              {state.task_running && (
                <button onClick={() => control("cancel")}>
                  <X size={14} />
                  取消任务
                </button>
              )}
              {!online && (
                <button onClick={() => connect()}>
                  <WifiOff size={14} />
                  连接中 · 点击重连
                </button>
              )}
            </div>
            {state.sync_error && (
              <div className="notice" role="status">
                {state.sync_error}
              </div>
            )}
            {error && (
              <div className="notice" role="alert">
                <span>{error}</span>
                <button aria-label="关闭提示" onClick={() => setError("")}>
                  <X size={14} />
                </button>
              </div>
            )}
            <form
              className="composer"
              onSubmit={send}
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                void upload(e.dataTransfer.files);
              }}
            >
              {notificationContext && (
                <div className="notification-reference">
                  <Quote size={13} aria-hidden="true" />
                  <span title={notificationContext.title}>
                    引用：{notificationContext.title}
                  </span>
                  <button
                    type="button"
                    aria-label="取消引用通知"
                    onClick={() => setNotificationContext(null)}
                  >
                    <X size={13} />
                  </button>
                </div>
              )}
              {attachments.length > 0 && (
                <div className="attachments">
                  {attachments.map((a) => (
                    <div key={a.id}>
                      <img src={"/api/uploads/" + a.id} alt={a.name} />
                      <button
                        type="button"
                        aria-label={"移除 " + a.name}
                        onClick={() =>
                          setAttachments((old) =>
                            old.filter((i) => i.id !== a.id),
                          )
                        }
                      >
                        <X size={12} />
                      </button>
                    </div>
                  ))}
                </div>
              )}
              <div className="composer-entry">
                <input
                  ref={file}
                  type="file"
                  accept="image/png,image/jpeg,image/webp"
                  multiple
                  hidden
                  onChange={(e) => upload(e.target.files)}
                />
                <button
                  type="button"
                  className="icon-button attach"
                  aria-label="添加图片"
                  title="添加图片 · 支持粘贴或拖入"
                  disabled={uploading}
                  onClick={() => file.current?.click()}
                >
                  {uploading ? (
                    <LoaderCircle size={20} className="spin" />
                  ) : (
                    <Plus size={22} />
                  )}
                </button>
                <textarea
                  ref={input}
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  rows={1}
                  placeholder="发消息，或继续刚才的话题…"
                  aria-label="消息"
                  maxLength={32000}
                  onPaste={(e) => {
                    const images = Array.from(e.clipboardData.files).filter(
                      (f) => f.type.startsWith("image/"),
                    );
                    if (images.length) {
                      e.preventDefault();
                      void upload(images);
                    }
                  }}
                  onKeyDown={(e) => {
                    if (
                      e.key === "Enter" &&
                      !e.shiftKey &&
                      !e.nativeEvent.isComposing
                    ) {
                      e.preventDefault();
                      void send();
                    }
                  }}
                />
                <button
                  className="send"
                  type="submit"
                  aria-label="发送"
                  disabled={
                    sending ||
                    uploading ||
                    !online ||
                    (!draft.trim() && !attachments.length)
                  }
                >
                  {sending ? (
                    <LoaderCircle size={18} className="spin" />
                  ) : (
                    <ArrowUp size={20} />
                  )}
                </button>
              </div>
            </form>
            <div className="composer-caption">
              <div className="composer-shortcuts">
                <button
                  type="button"
                  className="task-entry"
                  aria-label="任务"
                  aria-haspopup="dialog"
                  aria-expanded={activePanel === "tasks"}
                  aria-controls="activity-panel"
                  onClick={() => {
                    setActivePanel((current) =>
                      current === "tasks" ? null : "tasks",
                    );
                  }}
                >
                  <ListTodo size={15} />
                  后台任务
                </button>
                <button
                  type="button"
                  className="task-entry"
                  aria-label="通知"
                  aria-haspopup="dialog"
                  aria-expanded={activePanel === "notifications"}
                  aria-controls="activity-panel"
                  onClick={() => {
                    setActivePanel((current) =>
                      current === "notifications" ? null : "notifications",
                    );
                  }}
                >
                  <Bell size={15} />
                  通知
                  {!!state.notifications_unread && (
                    <b className="notification-badge">
                      {state.notifications_unread > 99
                        ? "99+"
                        : state.notifications_unread}
                    </b>
                  )}
                </button>
              </div>
              <div className="composer-caption-info">
                {state.task_running
                  ? "可以继续补充要求，任务会继续进行。"
                  : "与语音入口实时同步"}
                <span>Enter 发送 · Shift + Enter 换行</span>
              </div>
            </div>
          </div>
        </>
      )}
      {zoom && <ImagePreview url={zoom} onClose={() => setZoom(null)} />}
    </>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
