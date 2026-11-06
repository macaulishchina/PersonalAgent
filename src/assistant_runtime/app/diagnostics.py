"""Bounded structured diagnostics; never serialize request bodies or configuration."""

import contextvars
from datetime import datetime, timezone
import json
import logging
import math
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
import traceback

context = contextvars.ContextVar("diagnostic_context", default={})
_SECRET = re.compile(
    r"(?i)(authorization|cookie|password|credential|api[_-]?key|secret|token|prompt|content|body|text)$"
)


def scrub(value, secrets=()):
    value = str(value)[:8192]
    for secret in secrets:
        if secret and len(str(secret)) >= 4:
            value = value.replace(str(secret), "[redacted]")
    value = re.sub(r"(?i)Bearer\s+[^\s\"']+|(?<![\w-])sk-[A-Za-z0-9_-]{8,}", "[redacted]", value)
    value = re.sub(
        r"(?i)([\"']?(?:password|api[_-]?key|authorization|cookie|secret|access_token|refresh_token|token)[\"']?\s*[:=]\s*)(?:[\"'][^\"']*[\"']|[^\s,;}]+)",
        r"\1[redacted]",
        value,
    )
    value = re.sub(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+", "[endpoint]", value)
    value = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[redacted]", value)
    return value[:1200]


def bounded(value, secrets=(), depth=0):
    if depth > 4:
        return "[bounded]"
    if isinstance(value, dict):
        return {
            str(k)[:80]: "[redacted]" if _SECRET.search(str(k)) else bounded(v, secrets, depth + 1)
            for k, v in list(value.items())[:40]
        }
    if isinstance(value, (list, tuple)):
        return [bounded(v, secrets, depth + 1) for v in value[:12]]
    if isinstance(value, float) and not math.isfinite(value):
        return "[nonfinite]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return scrub(value, secrets)


class DiagnosticFormatter(logging.Formatter):
    def __init__(self, service, secrets=()):
        super().__init__()
        self.service = service
        self.secrets = tuple(secrets)

    def format(self, record):
        data = {
            "schema_version": 1,
            "at": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "pid": os.getpid(),
            "logger": record.name,
            "event": scrub(record.getMessage(), self.secrets),
        }
        data["fields"] = bounded({**context.get(), **getattr(record, "diagnostic", {})}, self.secrets)
        if record.exc_info and record.exc_info[1]:
            exc = record.exc_info[1]
            causes = []
            seen = set()
            while exc is not None and id(exc) not in seen and len(causes) < 4:
                seen.add(id(exc))
                causes.append({"type": type(exc).__name__, "reason": scrub(str(exc), self.secrets)})
                exc = exc.__cause__ or (exc.__context__ if not exc.__suppress_context__ else None)
            data["errors"] = causes
            data["frames"] = [
                {"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
                for f in traceback.extract_tb(record.exc_info[2])[-12:]
            ]
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 16000:
            fields = data["fields"]
            data["fields"] = {
                k: fields[k]
                for k in [
                    "request_id",
                    "issue_id",
                    "command_id",
                    "task_id",
                    "run_id",
                    "execution_id",
                    "session_id",
                    "state",
                    "status",
                ]
                if k in fields
            }
            data["fields"]["bounded"] = True
            encoded = json.dumps(data, ensure_ascii=False)
        return encoded


class DiagnosticHandler(RotatingFileHandler):
    def handleError(self, record):
        # Never echo the unformatted record/credentials on logging I/O failure.
        if not getattr(self, "warned", False):
            self.warned = True
            try:
                sys.__stderr__.write("Diagnostic log write failed; inspect log directory permissions/disk.\n")
            except Exception:
                pass


class DiagnosticStreamHandler(logging.StreamHandler):
    handleError = DiagnosticHandler.handleError


def configure(service, *, directory=None, level=None, max_bytes=None, backups=None, prefix="PERSONAL_AGENT"):
    selected = (level or os.getenv(prefix + "_LOG_LEVEL", "INFO")).upper()
    if selected not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("Unsupported diagnostic log level")
    size = int(max_bytes or os.getenv(prefix + "_LOG_MAX_BYTES", 10485760))
    count = int(backups if backups is not None else os.getenv(prefix + "_LOG_BACKUPS", 5))
    if not 1024 <= size <= 1073741824 or not 1 <= count <= 20:
        raise ValueError("Invalid diagnostic retention limits")
    secrets = [
        v for k, v in os.environ.items() if re.search(r"(?i)(TOKEN|PASSWORD|SECRET|API_KEY)$", k) and v
    ]
    formatter = DiagnosticFormatter(service, secrets)
    directory = directory if directory is not None else os.getenv(prefix + "_LOG_DIRECTORY")
    try:
        if directory:
            Path(directory).mkdir(parents=True, exist_ok=True)
            handler = DiagnosticHandler(
                Path(directory) / (service + ".jsonl"), maxBytes=size, backupCount=count, encoding="utf-8"
            )
        else:
            handler = DiagnosticStreamHandler()
    except OSError:
        handler = DiagnosticStreamHandler()
        try:
            sys.__stderr__.write("Diagnostic file unavailable; using sanitized stderr.\n")
        except Exception:
            pass
    handler.setFormatter(formatter)
    root = logging.getLogger()
    for previous in list(root.handlers):
        root.removeHandler(previous)
        previous.close()
    root.addHandler(handler)
    root.setLevel(selected)
    for name in ["httpx", "httpcore", "aiohttp.access", "sqlalchemy.engine", "uvicorn.access", "fastmcp"]:
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger(__name__).info(
        "diagnostics.configured",
        extra={"diagnostic": {"level": selected, "max_bytes": size, "backups": count}},
    )
    return handler


def emit(logger, level, name, **fields):
    logger.log(level, name, extra={"diagnostic": fields})
