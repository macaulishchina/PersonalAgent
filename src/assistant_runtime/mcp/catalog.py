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
    "request_task_model_upgrade",
    "create_task",
    "list_tasks",
    "get_task",
    "get_task_event_inbox",
    "get_task_execution_trace",
    "update_task",
    "update_own_task_schedule",
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


EVENT_TOOLS = (
    "event_capabilities",
    "list_event_sources",
    "event_status",
    "query_events",
    "get_event",
    "event_changes",
    "event_statistics",
    "record_event",
    "correct_event",
    "delete_event",
    "configure_event_source",
    "set_event_source_enabled",
    "remove_event_source",
    "verify_event_source",
)

# One service descriptor catalogue shared by provider registration and exclusion.
MEDIA_TOOLS = (
    "media_status",
    "search_media",
    "inspect_media_share",
    "list_media_files",
    "transfer_media",
    "download_media",
    "get_media_operation",
    "list_media_operations",
    "cancel_media_operation",
    "resume_media_operation",
    "verify_media_operation",
    "begin_media_login",
    "get_media_login",
)

DECISION_TOOLS = ("decision_support_status", "judge_options")

SERVICES = {
    "models": {"tools": ("list_models", "infer_with_model"), "task_context": True},
    "memory": {"tools": MEMORY_TOOLS, "task_context": False},
    "device": {"tools": DEVICE_TOOLS, "task_context": False},
    "tasks": {"tools": TASK_TOOLS, "task_context": True},
    "notifications": {"tools": NOTIFICATION_TOOLS, "task_context": True},
    "integrations": {"tools": INTEGRATION_TOOLS, "task_context": True},
    "events": {"tools": EVENT_TOOLS, "task_context": True},
    "media": {"tools": MEDIA_TOOLS, "task_context": True},
    "decisions": {"tools": DECISION_TOOLS, "task_context": True},
}
