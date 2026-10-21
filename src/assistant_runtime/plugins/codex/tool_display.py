"""Bounded display-only projections of trusted built-in MCP results.

Never forward raw arguments, task specifications, webpage bodies or credentials.
Missing/malformed results leave the ordinary tool activity intact.
"""

import json
import math
import re
from urllib.parse import urlsplit, urlunsplit


def mapping(value):
    return value if isinstance(value, dict) else {}


def short(value, limit=160):
    return value.strip()[:limit] if isinstance(value, str) else ""


def payload(result):
    result = mapping(result)
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    content = result.get("content")
    if isinstance(content, list):
        for block in content[:8]:
            block = mapping(block)
            text = block.get("text")
            if block.get("type") == "text" and isinstance(text, str) and len(text) <= 1_000_000:
                try:
                    value = json.loads(text)
                except (ValueError, RecursionError):
                    continue
                if isinstance(value, dict):
                    return value
    return {}


def tool_display(item):
    server, tool = item.get("server"), item.get("tool")
    if not isinstance(server, str) or not isinstance(tool, str):
        return None
    if (server, tool) not in {("tasks", "create_task"), ("tasks", "get_task"), ("search", "fetch_webpage")}:
        return None
    result = mapping(item.get("result"))
    data = payload(result)
    failed = item.get("status") in ("failed", "declined") or result.get("isError") is True
    if failed:
        return {"kind": tool, "outcome": "failed"}
    if not data:
        return None
    if server == "tasks":
        task = data if tool == "create_task" else mapping(data.get("task"))
        ident = task.get("id")
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ident):
            return None
        spec = mapping(task.get("spec"))
        display = {"kind": tool, "outcome": "completed", "task_id": ident, "title": short(spec.get("title"))}
        if tool == "create_task":
            execution = mapping(spec.get("execution"))
            display.update(
                model=short(execution.get("model")), effort=short(execution.get("reasoning_effort"), 32)
            )
        else:
            active, runs = data.get("active_runs"), data.get("runs")
            rows = active if isinstance(active, list) and active else runs
            run = mapping(rows[0]) if isinstance(rows, list) and rows else {}
            details = mapping(run.get("data"))
            display.update(
                state=short(run.get("state"), 32),
                summary=short(details.get("progress"), 1200),
                summary_truncated=isinstance(details.get("progress"), str)
                and len(details["progress"].strip()) > 1200,
            )
        return display
    state = data.get("state")
    if state not in ("completed", "failed"):
        return None
    display = {"kind": tool, "outcome": state, "provider": short(data.get("provider"), 80)}
    elapsed = data.get("elapsed_ms")
    if type(elapsed) in (int, float) and math.isfinite(elapsed) and 0 <= elapsed <= 3_600_000:
        display["elapsed_ms"] = round(elapsed)
    if state == "completed":
        display.update(
            title=short(data.get("title")),
            truncated=data.get("truncated") is True,
            cached=data.get("cached") is True,
        )
        url = data.get("url")
        if isinstance(url, str) and len(url) <= 4096:
            try:
                parts = urlsplit(url)
                if (
                    parts.scheme in ("http", "https")
                    and parts.hostname
                    and not parts.username
                    and not parts.password
                ):
                    display["domain"] = parts.hostname
                    # Query strings may contain private tokens; do not persist them
                    # or fabricate a link to a different resource by stripping them.
                    if not parts.query:
                        display["url"] = urlunsplit(parts._replace(fragment=""))
            except ValueError:
                pass
    else:
        code = mapping(data.get("error")).get("code")
        if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,80}", code):
            display["error_code"] = code
    return display
