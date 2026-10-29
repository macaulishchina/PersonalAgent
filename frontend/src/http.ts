// The kernel management listener owns the origin; chat is an optional mount.
export const chatPath = (path: string) => "/chat/" + path.replace(/^\/+/, "");
