"""Memory extraction uses the configured no-tool model and shared usage scope."""


async def complete(service, messages, schema):
    from assistant_runtime.core.models.settings import ModelSettings
    from assistant_runtime.api.models import ModelRequest
    import json
    import uuid

    binding = ModelSettings.parse(service.config.model_api).memory
    if not binding:
        raise RuntimeError("Memory requires an explicit model_api.memory binding")
    from assistant_runtime.api.ai_usage import current_usage

    token = current_usage.set(current_usage.get() or await service.ai_usage.scope(independent="memory"))
    try:
        result = await service.runtime.invoke(
            ModelRequest(
                "infer",
                {
                    "request_id": str(uuid.uuid4()),
                    "model": binding,
                    "text": json.dumps(messages, ensure_ascii=False),
                    "schema": schema,
                },
            ),
        )
    finally:
        current_usage.reset(token)
    if result.get("state") != "completed":
        raise RuntimeError("Memory model result unconfirmed")
    return result["data"]
