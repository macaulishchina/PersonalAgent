"""Public service/tool identities shared by schema and provider registration."""

MEMORY_TOOLS = ("search_memory", "read_memory", "remember_memory", "update_memory", "forget_memory")
DEVICE_TOOLS = (
    "list_devices",
    "inspect_device",
    "capture_image",
    "record_video",
    "move_camera",
    "stop_camera",
    "list_device_operations",
    "get_device_operation",
    "cancel_device_operation",
    "set_audio_volume",
    "stop_playback",
    "end_voice_conversation",
)
TASK_TOOLS = (
    "list_task_models",
    "create_task",
    "list_tasks",
    "get_task",
    "update_task",
    "run_task",
    "pause_task",
    "resume_task",
    "terminate_task",
    "archive_task",
    "cancel_task_run",
    "pause_task_run",
    "continue_task_run",
    "supplement_task",
    "list_task_artifacts",
)

NOTIFICATION_TOOLS = (
    "publish_notification",
    "update_notification",
    "list_notifications",
    "get_notification",
    "mark_notification_read",
)

INTEGRATION_TOOLS = (
    "list_mcp_servers",
    "inspect_mcp_server",
    "verify_mcp_server",
    "configure_mcp_server",
    "set_mcp_server_enabled",
    "remove_mcp_server",
    "apply_mcp_configuration",
    "reload_mcp_configuration",
    "begin_mcp_login",
)
