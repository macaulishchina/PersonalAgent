"""TypeSafe HTTP adapter. Secrets, native question fields and errors stay here."""

import json
import math
import os
from pathlib import Path

import aiohttp
from dotenv import dotenv_values
from .errors import DecisionError


def credential(root):
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        path = Path(root) / ".env"
        if path.is_file():
            if path.stat().st_size > 65536:
                raise DecisionError("credential_file_too_large")
            key = dotenv_values(path, interpolate=False).get("TYPESAFE_API_KEY")
    if not isinstance(key, str) or not key.strip():
        raise DecisionError("credential_missing")
    key = key.strip()
    if len(key) > 4096 or any(c.isspace() for c in key):
        raise DecisionError("credential_invalid")
    return key


def answer(payload, criteria, requested):
    try:
        model = payload["model"]
        item = payload["answers"]["decision"]
        values = item["probabilities"]
        confidence = item["confidence"]
        choice = item["choice"]
        if (
            not isinstance(model, str)
            or not model.startswith("jev-")
            or len(model) > 80
            or requested not in {"jev-latest", "jev-preview"}
            and model != requested
            or item["type"] != "choice"
            or choice not in criteria
            or set(values) != set(criteria)
            or type(confidence) not in (int, float)
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
            or any(
                type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
                for v in values.values()
            )
            or abs(sum(values.values()) - 1) > 0.02
            or values[choice] < max(values.values()) - 0.02
        ):
            raise ValueError()
        usage = payload.get("usage", {})
        if not isinstance(usage, dict) or any(
            type(usage.get(k, 0)) is not int or not 0 <= usage.get(k, 0) <= 1000000
            for k in ("input_tokens", "output_tokens")
        ):
            raise ValueError()
        return {
            "model": model,
            "choice": choice,
            "confidence": confidence,
            "probabilities": values,
            "usage": {k: usage.get(k, 0) for k in ("input_tokens", "output_tokens")},
        }
    except (KeyError, TypeError, ValueError, AttributeError):
        raise DecisionError("invalid_response") from None


class JevProvider:
    endpoint = "https://api.typesafe.ai/v1/systemone"

    def __init__(self, root, settings):
        self.root, self.settings = root, settings
        self.key = None
        self.session = None

    def reload(self):
        self.key = None
        try:
            self.key = credential(self.root)
        except (OSError, UnicodeError):
            raise DecisionError("credential_unavailable") from None

    async def start(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.settings.timeout_seconds), trust_env=True
        )

    async def close(self):
        self.key = None
        if self.session:
            await self.session.close()

    async def evaluate(self, job):
        if not self.key:
            raise DecisionError("credential_missing")
        try:
            async with self.session.post(
                self.endpoint,
                headers={"Authorization": "Bearer " + self.key},
                json={
                    "model": self.settings.model,
                    "state": job["input"],
                    "questions": {"decision": job["question"]},
                },
                allow_redirects=False,
            ) as response:
                if response.status in {401, 403}:
                    raise DecisionError("authentication_failed")
                if response.status == 429:
                    raise DecisionError("rate_limited")
                if response.status != 200:
                    raise DecisionError("upstream_unavailable")
                body = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    body.extend(chunk)
                    if len(body) > 65536:
                        raise DecisionError("response_too_large")
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeError):
                    raise DecisionError("invalid_response") from None
                return answer(payload, job["question"]["criteria"], self.settings.model)
        except DecisionError:
            raise
        except TimeoutError:
            raise DecisionError("timeout") from None
        except Exception:
            # Provider bodies, URLs, exception strings and tokens never enter logs.
            raise DecisionError("transport_failed") from None
