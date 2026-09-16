"""One client for every provider that speaks chat-completions.

Seven of the eight providers in :mod:`.providers` accept the same request shape, so this
is all it takes to support them: a POST to ``/chat/completions`` and a GET of ``/models``
for the health check. ``httpx`` is already a dependency — the whole BRAIN client is built
on it — so this adds nothing to install.

**The response is shaped like Google's on purpose.** :meth:`LLMService.generate` reads
``response.text`` and ``response.usage_metadata``, and giving this the same surface means
the rotation, budget accounting and error handling around it stay one code path rather
than two that drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts, named as the Google client names them."""

    prompt_token_count: int = 0
    candidates_token_count: int = 0
    thoughts_token_count: int = 0
    total_token_count: int = 0


@dataclass(frozen=True, slots=True)
class Reply:
    """What came back, with the same two attributes the Google path reads."""

    text: str
    usage_metadata: Usage


class OpenAICompatible:
    """A minimal chat-completions client."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._timeout = timeout

    async def generate(
        self,
        *,
        model: str,
        system: str,
        user: str,
        temperature: float = 0.7,
        json_mode: bool = False,
    ) -> Reply:
        """One completion.

        ``json_mode`` asks for a JSON object rather than prose. Not every provider
        honours it, which is why the callers that need structured output also say so in
        the prompt itself and parse defensively.
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions", headers=self._headers, json=payload
            )

        if response.status_code >= 400:
            # The status code is carried in the message because that is what the rate
            # limit detection reads. A 429 that does not say "429" would be retried as
            # though it were a permanent failure.
            raise RuntimeError(f"{response.status_code} {_detail(response)}")

        body = response.json()
        choices = body.get("choices") or []
        text = ""
        if choices:
            text = str((choices[0].get("message") or {}).get("content") or "")

        raw = body.get("usage") or {}
        usage = Usage(
            prompt_token_count=int(raw.get("prompt_tokens") or 0),
            candidates_token_count=int(raw.get("completion_tokens") or 0),
            total_token_count=int(raw.get("total_tokens") or 0),
        )
        return Reply(text=text, usage_metadata=usage)

    async def models(self) -> list[str]:
        """Model ids this key can reach. Not billed against the generation quota."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(f"{self.base_url}/models", headers=self._headers)

        if response.status_code >= 400:
            raise RuntimeError(f"{response.status_code} {_detail(response)}")

        body = response.json()
        rows = body.get("data") if isinstance(body, dict) else body
        if not isinstance(rows, list):
            return []
        return [str(row.get("id")) for row in rows if isinstance(row, dict) and row.get("id")]


def _detail(response: httpx.Response) -> str:
    """The provider's own words about what went wrong, kept short."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)[:200]
        if error:
            return str(error)[:200]
    return str(body)[:200]
