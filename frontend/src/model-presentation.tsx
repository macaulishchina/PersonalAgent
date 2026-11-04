import { useState } from "react";
import { Bot } from "lucide-react";
import { chatPath } from "./http";

export type ModelPresentation = {
  model: string;
  display_name: string;
  description?: string;
  provider_display_name?: string;
  avatar?: {
    kind: "builtin" | "image";
    name?: string;
    asset_id?: string;
    version?: string;
  };
};
export type ModelCatalog = {
  models?: ModelPresentation[];
  default?: { route?: { model: string } | null };
};

export function ModelAvatar({ model }: { model?: ModelPresentation }) {
  const [failed, setFailed] = useState("");
  const avatar = model?.avatar;
  let src = "";
  if (
    avatar?.kind === "builtin" &&
    /^[a-z][a-z0-9_-]*$/.test(avatar.name || "")
  )
    src = chatPath("api/model-avatar/builtin/" + avatar.name);
  else if (
    avatar?.kind === "image" &&
    /^[a-f0-9]{64}$/.test(avatar.asset_id || "") &&
    avatar.version === avatar.asset_id
  )
    src = chatPath(
      `api/model-avatar/${avatar.asset_id}?version=${avatar.version}`,
    );
  return (
    <span className="model-avatar" aria-hidden="true">
      {src && failed !== src ? (
        <img src={src} alt="" onError={() => setFailed(src)} />
      ) : (
        <Bot size={18} />
      )}
    </span>
  );
}
