"""Meter native Pi model streaming under the same host-bound execution identity."""

import asyncio
import logging
import time
import json
from contextlib import aclosing
from aiohttp import web

from assistant_runtime.api.ai_usage import UsageError, UsageScope
from .accounts import output_reservation


def usage_failure(code):
    return {
        "code": code,
        "category": "usage",
        "message": "用量保护阻止了模型请求：" + code,
        "phase": "admission",
        "submission": "not_submitted",
        "retry_allowed": False,
        "generation_settled": False,
    }


async def forward(transport, request, worker):
    client = transport.client
    if (not worker.active_turn and not worker.compacting) or request.transport.is_closing():
        raise web.HTTPConflict()
    payload = await request.json()
    if not isinstance(payload, dict) or set(payload) - {"model", "context", "effort"}:
        raise web.HTTPBadRequest()
    alias = worker.meta["model"]
    if worker.meta["reasoning_effort"] == "auto" and payload.get("effort") == "none":
        payload["effort"] = "auto"
    if payload.get("model") != alias or payload.get("effort") != worker.meta["reasoning_effort"]:
        raise web.HTTPBadRequest()
    entry = client.settings.entry(alias)
    if not client.accounts:
        raise web.HTTPBadRequest()
    context = payload.get("context")
    if not isinstance(context, dict) or not isinstance(context.get("messages"), list):
        raise web.HTTPBadRequest()
    images = any(
        part.get("type") == "image"
        for message in context["messages"]
        if isinstance(message, dict)
        for part in message.get("content", [])
        if isinstance(part, dict)
    )
    if images and not entry.get("images"):
        raise web.HTTPBadRequest()
    transport.authenticate(request)
    if worker.meta.get("controlled_context"):
        client.admission.service.controlled.authorize_session(worker.execution_session_id, required=True)
    account, args, native_bound = await client.accounts.route(alias, payload["effort"], images=images)
    maximum = entry.get("execution_max_output_tokens", client.settings.execution_max_output_tokens)
    reservation = output_reservation(account, maximum, native_bound)
    ident = worker.meta["budget_id"]
    try:
        scope = await client.usage.binding(worker.meta.get("budget_scope") or ident) or UsageScope(
            ident, worker.meta.get("budget_scope") or "foreground"
        )
        if not worker.meta.get("controlled_context") and (await client.usage.snapshot(ident)).get(
            "unknown_execution_requests"
        ):
            raise UsageError("usage_execution_unresolved", ident)
    except UsageError as exc:
        worker.usage_error = exc.code
        worker.model_failure = usage_failure(exc.code)
        return web.json_response({"error": {"message": "AI usage stopped: " + exc.code}}, status=400)
    timeout = (
        client.settings.compaction_timeout_seconds if worker.compacting else client.settings.timeout_seconds
    )
    from .attempts import AttemptMeter

    def admitted(ticket):
        worker.last_usage_call = ticket["id"]
        from .input_receipts import consumed

        consumed(client, worker, context, ticket["id"])
        if client.admission:
            client.admission.operation(
                worker.execution_session_id,
                "model",
                ticket["id"],
                compaction=worker.compacting,
                turn_id=worker.active_turn,
            )

    def settled(ticket, record, generation_settled):
        if client.admission:
            state = record["state"]
            if generation_settled and state in {"rejected", "unknown"}:
                state = "failed"
            client.admission.operation(worker.execution_session_id, "model", ticket["id"], state)

    meter = AttemptMeter(
        client.usage,
        scope,
        service="execution",
        provider=entry["provider"],
        model=entry["model"],
        compaction=worker.compacting,
        bounds={"input_tokens": entry.get("context_window", 32768), "output_tokens": reservation},
        body=payload,
        details={
            "model_alias": alias,
            "reasoning_effort": payload["effort"],
            "max_output_tokens": reservation,
        },
        on_ticket=admitted,
        on_settle=settled,
    )
    response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
    started = time.monotonic()
    try:
        transport.authenticate(request)
        if worker.meta.get("controlled_context"):
            client.admission.service.controlled.authorize_session(worker.execution_session_id, required=True)
        await response.prepare(request)
        args.update(
            context=payload["context"],
            max_tokens=maximum,
            timeout_ms=timeout * 1000,
            session_id=worker.execution_session_id,
            retry=client.settings.retry,
        )
        async with asyncio.timeout(timeout):
            async with aclosing(account.events("stream", args, on_attempt=meter.gate)) as events:
                async for item in events:
                    event = item.get("event")
                    if not event:
                        continue
                    meter.observe(event)
                    if event["type"] == "done":
                        worker.model_failure = None
                    elif event["type"] == "error":
                        worker.model_failure = event.get("failure") or event.get("error", {}).get("failure")
                        if worker.model_failure:
                            worker.model_failure.update(model=alias, provider=entry["provider"])
                    elif event["type"] == "retry":
                        await client.events.put(
                            {
                                "kind": "model.retry",
                                "data": {
                                    "session_id": worker.execution_session_id,
                                    "run_id": worker.active_turn,
                                    "attempt": event["attempt"],
                                    "category": event["category"],
                                },
                            }
                        )
                    await response.write((json.dumps(event) + "\n").encode())
        await response.write_eof()
        return response
    except UsageError as exc:
        worker.usage_error = exc.code
        worker.model_failure = usage_failure(exc.code)
        await response.write(
            (
                json.dumps(
                    {
                        "type": "error",
                        "error": {
                            "role": "assistant",
                            "content": [],
                            "stopReason": "error",
                            "errorMessage": exc.code,
                        },
                    }
                )
                + "\n"
            ).encode()
        )
        await response.write_eof()
        return response
    except (TimeoutError, RuntimeError, OSError, ValueError) as exc:
        deadline = isinstance(exc, TimeoutError)
        problem = {
            "code": "model_deadline" if deadline else "model_transport",
            "category": "deadline" if deadline else "transport",
            "message": "模型请求已达到总等待期限，未自动继续。"
            if deadline
            else "模型适配连接中断，无法确认本次请求结果。",
            "phase": "receiving",
            "submission": "possibly_submitted",
            "generation_settled": False,
            "retry_allowed": False,
            "model": alias,
            "provider": entry["provider"],
            "attempt_count": len(meter.attempts) + bool(meter.ticket),
        }
        worker.model_failure = problem
        logging.getLogger(__name__).warning(
            "model_request_stopped model=%s category=%s timeout_seconds=%s elapsed_seconds=%.3f",
            alias,
            problem["category"],
            timeout,
            time.monotonic() - started,
            exc_info=True,
            extra={
                "diagnostic": {
                    "model": alias,
                    "session_id": getattr(worker, "execution_session_id", None),
                    "turn_id": getattr(worker, "active_turn", None),
                    "category": problem["category"],
                }
            },
        )
        await response.write(
            (
                json.dumps(
                    {
                        "type": "error",
                        "failure": problem,
                        "error": {
                            "role": "assistant",
                            "content": [],
                            "stopReason": "error",
                            "failure": problem,
                            "errorMessage": problem["message"],
                        },
                    }
                )
                + "\n"
            ).encode()
        )
        await response.write_eof()
        return response
    finally:
        await meter.finish()
