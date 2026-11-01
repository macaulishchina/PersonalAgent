"""Task-oriented presentation, independent of storage layout and runtime imports."""

from copy import deepcopy
from assistant_runtime.api.configuration import ConfigurationError

PAGES = (
    ("models", "模型与账号", "连接供应商，选择模型，再指定对话及辅助用途。"),
    ("execution", "对话与执行", "设置对话行为、执行并发和用量保护。"),
    ("tasks", "任务与通知", "管理后台任务及结果通知。"),
    ("resources", "文件与记忆", "管理工作区、文件与记忆。"),
    ("connections", "连接与设备", "管理设备及连接。"),
    ("permissions", "权限管理", "管理第三方 MCP 的连接和长期授权。"),
    ("plugins", "插件", "管理功能、依赖和平台认证，打开插件的独立设置。"),
    ("access", "实例管理", "管理访问入口、运行状态及维护设置。"),
)

OWNER_PAGES = {
    "models": "models",
    "pi": "models",
    "ai_usage": "execution",
    "execution": "execution",
    "conversation": "execution",
    "cli": "execution",
    "assistant": "execution",
    "tasks": "tasks",
    "notifications": "tasks",
    "events": "tasks",
    "workfiles": "resources",
    "memory": "resources",
    "integrations": "permissions",
    "devices": "connections",
}

FIELD_OWNERS = {
    "assistant_name": "assistant",
    "wake_words": "assistant",
    "idle_seconds": "assistant",
    "voice_follow_up_seconds": "assistant",
    "quiet_hours": "assistant",
    "interaction": "assistant",
    "realtime_voice": "assistant",
    "input_device": "devices",
    "default_output": "devices",
    "minimum_volume": "devices",
    "echo_cancel": "devices",
    "devices": "devices",
    "main_model": "models",
    "main_reasoning_effort": "models",
    "workspace": "workfiles",
    "memory_enabled": "memory",
    "memory_auto_capture": "memory",
    "hid_buttons": "hid_buttons",
}

LABELS = {
    "workspace": "工作区",
    "pi_binary": "Pi 可执行文件",
    "pi_shell": "工具终端",
    "unit_name": "系统服务名称",
    "model_dir": "本地模型目录",
    "root": "实例目录",
    "profile": "运行模式",
    "backend": "执行器",
    "bindings": "能力绑定",
    "plugin_backends": "平台默认实现",
    "input_device": "主语音输入",
    "default_output": "主语音输出",
    "idle_seconds": "空闲超时（秒）",
    "voice_follow_up_seconds": "语音追问等待（秒）",
    "minimum_volume": "最低音量",
    "echo_cancel": "回声消除",
    "realtime_voice": "语音音色",
    "wake_words": "唤醒词",
    "instance_skills": "实例技能",
    "memory_enabled": "启用记忆",
    "memory_auto_capture": "自动记录记忆",
}

WIDGETS = {"auto", "text", "secret", "select", "list", "connection", "model", "status"}


def validate_interface(ui):
    from .schema import validator
    import json

    if (
        not isinstance(ui, dict)
        or not isinstance(ui.get("fields", {}), dict)
        or not isinstance(ui.get("actions", {}), dict)
    ):
        raise ValueError("Invalid interface")
    if len(ui.get("fields", {})) > 256 or len(ui.get("actions", {})) > 32:
        raise ValueError("Interface exceeds bounds")
    for field in ui.get("fields", {}).values():
        if not isinstance(field, dict) or field.get("widget", "auto") not in WIDGETS:
            raise ValueError("Unsupported component")
        if "order" in field and type(field["order"]) is not int:
            raise ValueError("Field order must be an integer")
        for flag in ("advanced",):
            if flag in field and type(field[flag]) is not bool:
                raise ValueError("Interface flag must be boolean")
        when = field.get("visible_when")
        if when is not None and (
            not isinstance(when, dict)
            or not isinstance(when.get("path"), list)
            or not all(isinstance(part, str) for part in when["path"])
            or "equals" not in when
        ):
            raise ValueError("Invalid visibility condition")
    for operation in ui.get("actions", {}).values():
        if not isinstance(operation, dict) or not isinstance(operation.get("output"), dict):
            raise ValueError("Action requires an output schema")
        validator(json.dumps(operation["output"]))
        validator(json.dumps(operation.get("input", {"type": "object"})))


def describe(inventory):
    """Validate packaged UI declarations without loading plugin runtime code."""
    from jsonschema.exceptions import SchemaError

    pages = [{"id": key, "title": title, "description": detail} for key, title, detail in PAGES]
    for owner in inventory["plugins"]:
        spec = owner.get("configuration")
        if not spec or "error" in spec:
            continue
        ui = spec.get("interface", {})
        try:
            validate_interface(ui)
            from .policies import MODES

            if not isinstance(spec.get("application", {}), dict) or any(
                mode not in MODES for mode in spec.get("application", {}).values()
            ):
                raise ValueError("Invalid application policy")
        except (ValueError, TypeError, SchemaError):
            inventory.setdefault("issues", []).append({"id": owner["id"], "error": "invalid_interface"})
            ui = spec["interface"] = {}
            spec["application"] = {}
        page = OWNER_PAGES.get(owner["id"], "plugin:" + owner["id"])
        owner["page"] = page
        if page.startswith("plugin:"):
            pages.append(
                {
                    "id": page,
                    "title": ui.get("title", spec.get("title", owner["id"])),
                    "description": ui.get("description", spec.get("description", "")),
                    "owner": owner["id"],
                }
            )
        for name, field in ui.get("fields", {}).items():
            if not isinstance(field, dict) or field.get("widget", "auto") not in WIDGETS:
                raise ConfigurationError([owner["id"], "interface", name], "widget", "Unsupported component")
    return pages


def field_interface(spec, key, rule, inventory):
    from .policies import application_for

    owner = next((p for p in inventory["plugins"] if p.get("configuration") is spec), None)
    ident = owner.get("id", spec.get("namespace")) if owner else FIELD_OWNERS.get(key)
    page = owner.get("page") if owner else OWNER_PAGES.get(ident)
    if not page:
        page = {"conversation": "execution", "notifications": "tasks", "extensions": "plugins"}.get(
            rule.get("x-group", spec.get("group", "access")), rule.get("x-group", spec.get("group", "access"))
        )
    ui = deepcopy(spec.get("interface", {}).get("fields", {}).get(key, {}))
    ui.setdefault("title", LABELS.get(key, rule.get("title", key)))
    ui.setdefault("advanced", rule.get("x-advanced", False))
    ui.setdefault("widget", "auto")
    ui.setdefault("order", list(spec["schema"].get("properties", {})).index(key))
    return {"owner_id": ident, "page": page, "interface": ui, "application": application_for(spec, [key])}
