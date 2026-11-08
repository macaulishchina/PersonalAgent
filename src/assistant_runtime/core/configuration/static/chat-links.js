"use strict";
const $ = (id) => document.getElementById(id);
let before = null;
async function api(action, args = {}) {
  const response = await fetch("/api/entrances/web", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, arguments: args }),
  });
  if (response.status === 401) {
    $("management").hidden = true;
    $("created").hidden = true;
    $("link").value = "";
    $("open-link").removeAttribute("href");
    $("login-help").hidden = false;
    throw new Error("请先登录系统配置。");
  }
  const data = await response.json();
  if (!response.ok)
    throw new Error(data.error || "操作失败，请确认 Web 插件已启用。");
  return data;
}
async function action(fn) {
  const buttons = [...document.querySelectorAll("button")];
  buttons.forEach((button) => {
    button.disabled = true;
  });
  try {
    await fn();
    $("status").textContent = "";
  } catch (error) {
    $("status").textContent = error.message;
  } finally {
    buttons.forEach((button) => {
      button.disabled = false;
    });
  }
}
function option(session) {
  if ([...$("session").options].some((item) => item.value === session.id))
    return;
  const item = document.createElement("option");
  item.value = session.id;
  item.textContent = `${session.title} · ${session.id.slice(0, 8)}`;
  $("session").append(item);
  const entry = item.cloneNode(true);
  $("entrance-session").append(entry);
}
async function sessions() {
  const result = await api("sessions", before ? { before } : {});
  result.sessions.forEach(option);
  before = result.before;
  $("older").hidden = !before;
}
async function links() {
  const result = await api("links");
  $("links").replaceChildren();
  for (const link of result.links) {
    const row = document.createElement("p");
    const expired = link.expires && link.expires * 1000 <= Date.now();
    row.textContent = `${link.title} · ${link.session_id.slice(0, 8)} · ${link.permission === "read" ? "仅查看" : "可聊天"} · ${link.revoked ? "已撤销" : expired ? "已过期" : link.expires ? new Date(link.expires * 1000).toLocaleString() + " 到期" : "直到撤销"} `;
    if (!link.revoked) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = "撤销";
      button.addEventListener("click", () =>
        action(async () => {
          await api("revoke_link", { id: link.id });
          if ($("link").dataset.id === link.id) {
            $("link").value = "";
            $("open-link").removeAttribute("href");
            $("created").hidden = true;
          }
          await links();
        }),
      );
      row.append(button);
    }
    $("links").append(row);
  }
  if (!result.links.length) $("links").textContent = "尚无快捷链接。";
}
async function entrances() {
  const result = await api("entrances");
  $("entrances").replaceChildren();
  for (const entry of result.entrances) {
    const row = document.createElement("p");
    row.textContent = `${entry.title} · ${entry.endpoint_id} · ${entry.revoked ? "已关闭" : "已启用"} `;
    if (!entry.revoked) {
      const open = document.createElement("a");
      open.textContent = "打开入口";
      open.href = `/chat/e/${entry.id}/`;
      open.target = "_blank";
      open.rel = "noopener noreferrer";
      const close = document.createElement("button");
      close.type = "button";
      close.textContent = "关闭入口";
      close.addEventListener("click", () =>
        action(async () => {
          await api("revoke_entrance", { id: entry.id });
          await entrances();
          await accesses();
        }),
      );
      row.append(open, close);
    }
    $("entrances").append(row);
  }
}
async function accesses() {
  const result = await api("accesses");
  const kinds = {
    temporary: "设备临时批准",
    account: "账号会话访问",
    account_login: "Web 账号登录",
    link: "快捷链接访问",
  };
  $("accesses").replaceChildren();
  for (const access of result.accesses) {
    const row = document.createElement("p");
    row.textContent = `${access.title} · ${kinds[access.kind]} · ${access.permission === "read" ? "仅查看" : "可聊天"} · ${access.created ? new Date(access.created * 1000).toLocaleString() + " 开始 · " : ""}${new Date(access.expires * 1000).toLocaleString()} 到期 `;
    const close = document.createElement("button");
    close.type = "button";
    close.textContent = "关闭访问";
    close.addEventListener("click", () =>
      action(async () => {
        await api("close_access", { id: access.id, kind: access.kind });
        await accesses();
      }),
    );
    row.append(close);
    $("accesses").append(row);
  }
  if (!result.accesses.length) $("accesses").textContent = "当前没有授权访问。";
}
$("refresh-accesses").addEventListener("click", () => action(accesses));
$("create-entrance").addEventListener("submit", (event) => {
  event.preventDefault();
  void action(async () => {
    const result = await api("create_entrance", {
      session_id: $("entrance-session").value,
      endpoint_id: $("endpoint").value.trim(),
      title: $("entrance-title").value.trim(),
      permission: $("entrance-permission").value,
    });
    const url = new URL(result.path, location.origin).href;
    $("entrance-url").value = url;
    $("open-entrance").href = url;
    $("entrance-created").hidden = false;
    await entrances();
  });
});
$("older").addEventListener("click", () => action(sessions));
$("create-session").addEventListener("submit", (event) => {
  event.preventDefault();
  void action(async () => {
    const result = await api("create_session", {
      title: $("title").value.trim(),
    });
    option(result);
    $("session").value = result.id;
    $("create-session").reset();
  });
});
$("create-link").addEventListener("submit", (event) => {
  event.preventDefault();
  void action(async () => {
    const result = await api("create_link", {
      session_id: $("session").value,
      permission: $("permission").value,
      expires_in: Number($("expiry").value),
    });
    const url = new URL(result.path, location.origin).href;
    $("link").value = url;
    $("link").dataset.id = result.id;
    $("open-link").href = url;
    $("created").hidden = false;
    await links();
  });
});
void action(async () => {
  await sessions();
  await links();
  await entrances();
  await accesses();
  $("management").hidden = false;
});
