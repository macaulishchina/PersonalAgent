// The kernel management listener owns the origin; chat is an optional mount.
export function conversationBase(pathname: string) {
  const match = pathname.match(/^\/chat\/c\/([a-f0-9]{32})\/$/);
  const entry = pathname.match(
    /^\/chat\/e\/([a-f0-9]{32})\/(account|visit)\/$/,
  );
  if (entry) return `/chat/e/${entry[1]}/${entry[2]}/`;
  return match ? `/chat/c/${match[1]}/` : "/chat/";
}

export const chatBase = conversationBase(globalThis.location?.pathname || "/");
export const selectedConversation = chatBase !== "/chat/";
export const scopedConversation =
  selectedConversation && !chatBase.endsWith("/account/");
export const entranceBase =
  globalThis.location?.pathname.match(/^\/chat\/e\/[a-f0-9]{32}\//)?.[0] ||
  null;
export const entranceLanding = Boolean(
  entranceBase && location.pathname === entranceBase,
);
export const conversationStorageKey = (key: string) =>
  selectedConversation ? chatBase + key : key;
export const chatPath = (path: string) => chatBase + path.replace(/^\/+/, "");

// A fragment is never sent as a request URL. Erase it before the first await;
// neither browser storage nor error messages retain the bearer credential.
export async function openConversationLink() {
  if (!chatBase.startsWith("/chat/c/") || !location.hash) return false;
  const token = location.hash.slice(1);
  history.replaceState(null, "", location.pathname);
  if (!/^[A-Za-z0-9_-]{43}$/.test(token))
    throw new Error("快捷链接无效，请重新打开完整链接。");
  const response = await fetch(chatPath("api/link"), {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
  });
  if (!response.ok)
    throw new Error("快捷链接已失效、已撤销或暂时不可用，请联系管理员。");
  const body = await response.json();
  if (!/^\/chat\/c\/[a-f0-9]{32}\/$/.test(body.path))
    throw new Error("会话入口无效。");
  location.replace(body.path);
  return true;
}
