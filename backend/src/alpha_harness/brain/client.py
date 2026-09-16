"""Async HTTP client for the BRAIN API.

Two behaviours in here are the reason this file exists, and both break naive clients:

1. **A response carrying ``Retry-After`` means "not ready yet".** The header's presence —
   not the status code — is the signal (``docs/wqb-api/03-conventions.md``).
   :meth:`BrainClient.poll` re-issues the request until it is gone; recordsets,
   correlations and checks are read that way.
2. **``POST /simulations`` answers in headers.** The id is in ``Location``; the caller
   reads it from :class:`BrainResponse`.

Versioning lives in the ``Accept`` header (``application/json;version=N``), not the path.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import httpx
import structlog

from .errors import (
    DAILY_LIMIT_DETAIL,
    BrainAuthError,
    BrainDailyLimitReached,
    BrainError,
    BrainForbidden,
    BrainNotFound,
    BrainPollTimeout,
    BrainRateLimited,
    BrainServerError,
    BrainServiceUnavailable,
    BrainTransportError,
    BrainValidationError,
    BrainVerificationRequired,
)

log = structlog.get_logger(__name__)

Method = Literal["GET", "POST", "PATCH", "DELETE", "OPTIONS"]

DEFAULT_VERSION = "2.0"

#: Shortest wait between polls of a pending job, whatever ``Retry-After`` says.
MIN_POLL_DELAY = 0.25


@dataclass(frozen=True, slots=True)
class RateLimit:
    """Daily simulation quota from the ``x-ratelimit-*`` headers of ``POST /simulations``.

    Not in docs/wqb-api; verified live 2026-09-14 (probe P15: limit 5000, remaining,
    reset in seconds).
    """

    limit: int | None
    remaining: int | None
    reset_seconds: float | None
    observed_at: float

    @classmethod
    def from_headers(cls, headers: httpx.Headers) -> RateLimit | None:
        def _num(name: str, kind: type[int] | type[float]) -> Any:
            raw = headers.get(name)
            try:
                return kind(raw) if raw is not None else None
            except ValueError:
                return None

        limit = _num("x-ratelimit-limit", int)
        remaining = _num("x-ratelimit-remaining", int)
        reset = _num("x-ratelimit-reset", float)
        if limit is None and remaining is None and reset is None:
            return None
        return cls(limit=limit, remaining=remaining, reset_seconds=reset, observed_at=time.time())


@dataclass(slots=True)
class BrainResponse:
    """A completed BRAIN response with the bits callers actually need."""

    status: int
    headers: httpx.Headers
    body: Any
    retry_after: float | None
    rate_limit: RateLimit | None
    location: str | None

    @property
    def pending(self) -> bool:
        """True while the server is still working on an asynchronous job."""
        return self.retry_after is not None


def _parse_retry_after(headers: httpx.Headers) -> float | None:
    """Seconds to wait, or ``None`` when the header is absent and the result is ready.

    Exactly the documented parser (``docs/wqb-api/endpoints/osmosis.md``): numeric seconds,
    or an HTTP-date; zero, negative, a past date or anything unreadable means poll now.
    """
    raw = headers.get("retry-after")
    if raw is None:
        return None
    raw = raw.strip()
    try:
        seconds = float(raw)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(raw) - datetime.now(UTC)).total_seconds()
        except TypeError, ValueError:
            return 0.0
    return max(seconds, 0.0)


class BrainClient:
    """Cookie-authenticated async client.

    The instance owns a cookie jar; :meth:`export_cookies` / :meth:`load_cookies` persist
    it so a restart does not cost another proof-of-work solve.
    """

    def __init__(
        self,
        base_url: str = "https://api.worldquantbrain.com",
        *,
        timeout: float = 30.0,
        poll_timeout: float = 300.0,
        default_attempts: int = 5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self._default_attempts = max(1, default_attempts)
        self._poll_timeout = poll_timeout
        #: Monotonic time before which no request is sent: set by a ``429`` so every caller
        #: backs off together instead of each retrying into a server that said stop.
        self._resume_at = 0.0
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=10.0),
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": "alpha-harness/0.1 (local research studio)"},
        )

    # -- lifecycle -------------------------------------------------------

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- cookie jar ------------------------------------------------------

    def export_cookies(self) -> list[dict[str, Any]]:
        """Serialise the jar for storage."""
        return [
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path,
                "expires": c.expires,
                "secure": c.secure,
            }
            for c in self._client.cookies.jar
        ]

    def load_cookies(self, cookies: list[dict[str, Any]]) -> None:
        """Restore a jar produced by :meth:`export_cookies`."""
        for c in cookies:
            if not c.get("name") or c.get("value") is None:
                continue
            self._client.cookies.set(
                c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/")
            )

    def clear_cookies(self) -> None:
        self._client.cookies.clear()

    # -- core request ----------------------------------------------------

    async def request(
        self,
        method: Method,
        path: str,
        *,
        version: str = DEFAULT_VERSION,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        auth: tuple[str, str] | None = None,
        raise_for_status: bool = True,
    ) -> BrainResponse:
        """Issue one request and translate failures into typed exceptions.

        ``params`` values that are ``None`` are dropped.
        """
        merged = {"Accept": f"application/json;version={version}"}
        if headers:
            merged.update(headers)

        clean_params = {k: v for k, v in params.items() if v is not None} if params else None

        # Re-checked after each sleep: another 429 may have pushed the pause further out.
        # Jittered so every waiter does not re-send in the same tick and draw another 429.
        while (wait := self._resume_at - time.monotonic()) > 0:  # noqa: ASYNC110
            await asyncio.sleep(wait + random.uniform(0.05, 0.25))

        try:
            response = await self._client.request(
                method,
                path.lstrip("/"),
                params=clean_params,
                json=json_body,
                headers=merged,
                auth=auth or httpx.USE_CLIENT_DEFAULT,
            )
        # Not connected, or no pooled connection free: the request never left.
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise BrainTransportError(
                f"{method} {path} could not connect: {exc}", maybe_delivered=False
            ) from exc
        except httpx.TimeoutException as exc:
            raise BrainTransportError(f"{method} {path} timed out", maybe_delivered=True) from exc
        except httpx.HTTPError as exc:
            raise BrainTransportError(
                f"{method} {path} failed: {exc}", maybe_delivered=True
            ) from exc

        result = BrainResponse(
            status=response.status_code,
            headers=response.headers,
            body=_decode(response),
            retry_after=_parse_retry_after(response.headers),
            rate_limit=RateLimit.from_headers(response.headers),
            location=response.headers.get("location"),
        )
        if result.status == 429 and result.retry_after:
            # The server named its own wait: nobody sends before it is over.
            self._resume_at = max(self._resume_at, time.monotonic() + result.retry_after)

        if raise_for_status and response.status_code >= 400:
            raise self.to_error(method, path, result)
        return result

    def to_error(self, method: str, path: str, r: BrainResponse) -> BrainError:
        """Map a failed response to a typed error (``docs/wqb-api/04-error-handling.md``)."""
        where = f"{method} {path}"
        body = r.body
        detail = body.get("detail") if isinstance(body, dict) else None

        if r.status == 401:
            inquiry = body.get("inquiry") if isinstance(body, dict) else None
            if isinstance(inquiry, str) and inquiry:
                # Biometric step-up, not a credential failure (02-authentication.md).
                return BrainVerificationRequired(
                    "BRAIN requires identity verification before this session can be used.",
                    inquiry=inquiry,
                    body=body,
                )
            return BrainAuthError(
                f"{where}: not authenticated ({detail or 'session expired'})",
                status=401,
                body=body,
                detail=detail if isinstance(detail, str) else None,
            )

        if r.status == 403:
            return BrainForbidden(
                f"{where}: forbidden ({detail or 'account lacks permission for this request'})",
                status=403,
                body=body,
                detail=detail if isinstance(detail, str) else None,
            )

        if r.status == 404:
            return BrainNotFound(f"{where}: not found", status=404, body=body)

        if r.status == 400:
            fields = body if isinstance(body, dict) else {"detail": body}
            return BrainValidationError(
                f"{where}: rejected by the platform", fields=fields, body=body
            )

        if r.status == 429:
            if detail == DAILY_LIMIT_DETAIL:
                return BrainDailyLimitReached(
                    "Daily simulation limit reached. Please try tomorrow (EST time zone).",
                    body=body,
                )
            return BrainRateLimited(
                f"{where}: rate limited ({detail or 'too many requests'})",
                retry_after=r.retry_after,
                body=body,
            )

        if r.status == 503:
            return BrainServiceUnavailable(f"{where}: service unavailable", status=503, body=body)

        if r.status >= 500:
            return BrainServerError(f"{where}: server error {r.status}", status=r.status, body=body)

        return BrainError(f"{where}: unexpected status {r.status}", status=r.status, body=body)

    async def request_retrying(
        self,
        method: Method,
        path: str,
        *,
        attempts: int | None = None,
        base_backoff: float = 2.0,
        max_backoff: float = 60.0,
        **kwargs: Any,
    ) -> BrainResponse:
        """Issue a request, retrying ``429``, ``503``, other ``5xx`` and transport errors.

        Waits the server's ``Retry-After`` when it sends one, otherwise exponential backoff
        with jitter. The thresholds behind a ``429`` are server-side and undocumented, so
        nothing here guesses a request rate; a ``429`` instead pauses every caller of this
        client for the wait. Never retries the daily cap.
        """
        attempts = attempts or self._default_attempts
        last: BrainError | None = None

        for attempt in range(1, attempts + 1):
            try:
                return await self.request(method, path, **kwargs)
            except BrainError as exc:
                if not exc.retryable or attempt == attempts:
                    raise
                last = exc

                delay = getattr(exc, "retry_after", None)
                if delay is None:
                    delay = min(base_backoff * (2 ** (attempt - 1)), max_backoff)
                delay = min(float(delay), max_backoff)
                # Jitter so parallel callers do not resynchronise on the same instant.
                delay += random.uniform(0, max(delay, 1.0) * 0.25)
                if isinstance(exc, BrainRateLimited):
                    self._resume_at = max(self._resume_at, time.monotonic() + delay)

                log.warning(
                    "brain.retrying",
                    path=path,
                    attempt=attempt,
                    of=attempts,
                    delay=round(delay, 2),
                    reason=type(exc).__name__,
                )
                await asyncio.sleep(delay)

        if last is None:
            raise RuntimeError("retry loop ran zero attempts")
        raise last

    # -- the asynchronous job protocol -----------------------------------

    async def poll(self, path: str, *, version: str = DEFAULT_VERSION) -> BrainResponse:
        """Drive an asynchronous ``GET`` job to completion.

        Re-issues the request while the response carries ``Retry-After``, waiting what the
        server asks. Returns the first response *without* that header.
        """
        deadline = time.monotonic() + self._poll_timeout
        attempt = 0

        while True:
            # Retrying: a 429 or 503 between polls says nothing about the job.
            response = await self.request_retrying("GET", path, version=version)
            attempt += 1

            if not response.pending:
                return response

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrainPollTimeout(
                    f"{path} still pending after {attempt} polls; giving up. "
                    "The job may still complete server-side."
                )

            # The documented "poll now" (zero, past or unreadable) still waits a floor:
            # otherwise a job that stays pending re-requests as fast as the round trip.
            delay = min(max(response.retry_after or 0.0, MIN_POLL_DELAY), remaining)
            log.debug("brain.poll.waiting", path=path, attempt=attempt, delay=delay)
            await asyncio.sleep(delay)


def _decode(response: httpx.Response) -> Any:
    """Best-effort body decode. Some endpoints return empty bodies or non-JSON."""
    if not response.content:
        return None
    content_type = response.headers.get("content-type", "")
    if "json" not in content_type:
        return response.text
    try:
        return response.json()
    except ValueError:
        return response.text
