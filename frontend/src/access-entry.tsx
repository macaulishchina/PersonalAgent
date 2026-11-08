import { useEffect, useRef, useState } from "react";
import { entranceBase } from "./http";
import { LoaderCircle, LogIn, Smartphone } from "lucide-react";

type Request = {
  id: string;
  code: string;
  state: string;
  deadline: number;
  permission: string;
};
type Status = {
  title: string;
  account_signed_in: boolean;
  request: Request | null;
  visit_path?: string;
};

export function AccessEntry() {
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [login, setLogin] = useState(false);
  const [password, setPassword] = useState("");
  const [waiting, setWaiting] = useState(false);
  const [revision, setRevision] = useState(0);
  const claiming = useRef(false);
  const alive = useRef(true);
  async function call(action: string, body?: object, signal?: AbortSignal) {
    const response = await fetch(
      entranceBase + "api/" + action,
      body === undefined
        ? { signal }
        : {
            method: "POST",
            signal,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          },
    );
    if (!response.ok)
      throw new Error(
        response.status === 401
          ? "入口已关闭或当前授权已失效。"
          : "暂时无法处理，请稍后重试。",
      );
    return response.json();
  }
  function enter(path: string, kind: "account" | "visit") {
    if (path !== entranceBase + kind + "/") throw new Error("会话入口无效。");
    location.assign(path);
  }
  useEffect(() => {
    alive.current = true;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const result: Status = await call(
          "access",
          undefined,
          controller.signal,
        );
        if (!alive.current) return;
        setStatus(result);
        setError("");
        const state = result.request?.state;
        setWaiting(state === "pending" || state === "approved");
        if (state === "consumed" && result.visit_path) {
          enter(result.visit_path, "visit");
          return;
        }
        if (state === "approved" && !claiming.current) {
          claiming.current = true;
          try {
            const grant = await call("claim", {}, controller.signal);
            if (alive.current) enter(grant.path, "visit");
          } catch {
            if (alive.current) {
              setWaiting(false);
              setError("授权领取未完成。请刷新核对状态；不要重复申请。");
            }
          }
          return;
        }
        if (state === "pending") timer = setTimeout(refresh, 1000);
      } catch {
        if (!controller.signal.aborted && alive.current) {
          setError("无法读取访问状态，请刷新重试。");
          setWaiting(false);
        }
      }
    };
    void refresh();
    return () => {
      alive.current = false;
      controller.abort();
      clearTimeout(timer);
    };
  }, [revision]);
  async function start() {
    setBusy(true);
    setError("");
    claiming.current = false;
    try {
      const result = await call("request", {});
      setStatus((current) =>
        current ? { ...current, request: result.request } : current,
      );
      setWaiting(true);
      setRevision((current) => current + 1);
    } catch (error) {
      setError(error instanceof Error ? error.message : "申请失败，请重试。");
    } finally {
      setBusy(false);
    }
  }
  async function signIn(event?: React.FormEvent) {
    event?.preventDefault();
    setBusy(true);
    setError("");
    try {
      if (!status?.account_signed_in) {
        const response = await fetch("/chat/api/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token: password }),
        });
        setPassword("");
        if (!response.ok) throw new Error("Web 访问口令不正确或尝试过于频繁。");
      }
      const result = await call("account", {});
      enter(result.path, "account");
    } catch (error) {
      setError(error instanceof Error ? error.message : "登录失败，请重试。");
    } finally {
      setBusy(false);
    }
  }
  const state = status?.request?.state;
  const message =
    state === "denied"
      ? "设备拒绝了本次申请。"
      : state === "expired"
        ? "本次申请已过期，请重新申请。"
        : state === "cancelled"
          ? "本次申请已取消。"
          : state === "consumed"
            ? "上次授权已使用或访问已结束，可以重新申请。"
            : "";
  return (
    <main className="welcome login access-entry">
      <h1>{status?.title || "访问对话"}</h1>
      <p>选择设备授权，或使用已有 Web 身份登录。</p>
      <div className="access-choices">
        <section className="access-choice">
          <Smartphone size={22} aria-hidden="true" />
          <h2>申请访问</h2>
          <p>在设备上确认，访问时长由设备决定。</p>
          {waiting ? (
            <div role="status">
              <LoaderCircle className="spin" size={18} />
              <p>等待设备确认</p>
              <strong className="access-code">{status?.request?.code}</strong>
              <p>请核对设备上的验证码。</p>
              <button
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  try {
                    await call("cancel", {});
                    setWaiting(false);
                    setRevision((current) => current + 1);
                  } catch {
                    setError("取消未确认，请刷新核对。");
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                取消申请
              </button>
            </div>
          ) : (
            <button
              className="primary"
              disabled={busy || !status}
              onClick={() => void start()}
            >
              申请访问
            </button>
          )}
          {message && <p role="status">{message}</p>}
        </section>
        <section className="access-choice">
          <LogIn size={22} aria-hidden="true" />
          <h2>登录进入</h2>
          <p>使用 Web 访问口令，无需设备批准，沿用正常登录有效期。</p>
          {!login ? (
            <button
              disabled={busy || !status || waiting}
              onClick={() => {
                if (status?.account_signed_in) void signIn();
                else setLogin(true);
              }}
            >
              {status?.account_signed_in ? "使用已登录身份进入" : "登录进入"}
            </button>
          ) : (
            <form onSubmit={(event) => void signIn(event)}>
              <label htmlFor="web-password">Web 访问口令</label>
              <input
                id="web-password"
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="current-password"
                required
                autoFocus
              />
              <button className="primary" disabled={busy || waiting}>
                登录并进入
              </button>
            </form>
          )}
        </section>
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </main>
  );
}
