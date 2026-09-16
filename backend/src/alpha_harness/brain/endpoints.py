"""Typed wrappers over the BRAIN endpoint surface.

One place that knows which endpoint needs which ``Accept`` version, which ones are
asynchronous jobs, and how pagination works.

Structured platform entities come back as Pydantic models. Two kinds of response stay raw
dicts: bulk reads where validating every row costs too much (``list_data_fields_all``), and
open-ended or undocumented blobs the callers read selectively.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from .altcha import Challenge, Solution, solve_async
from .errors import BrainError, BrainVerificationRequired
from .schemas import (
    Alpha,
    AuthState,
    DataCategory,
    DataSet,
    Operator,
    RecordSet,
    SimulationRequest,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from .client import BrainClient, BrainResponse
    from .filters import AlphaQuery

log = structlog.get_logger(__name__)

# Endpoints pinned to a non-default Accept version (docs/wqb-api/03-conventions.md).
V_SETTINGS_SCHEMA = "4.0"  # OPTIONS /simulations
#: GET /data-fields with all four scope params. Unpaginated in practice: tens of thousands
#: of rows arrive in one response.
V_FIELDS_ALL = "3.0"
V_ALPHA_LIST = "4.0"  # GET /users/{id}/alphas
#: GET /users/self/alphas/summary. Undocumented; 4.0 returns {unsubmitted, active,
#: decommissioned} where 2.0 returns {is, os, prod}.
V_ALPHA_SUMMARY = "4.0"

#: The simulation type this application sends; its per-type settings tree is merged in.
SIMULATION_TYPE = "REGULAR"

PAGE_SIZE = 50


class BrainEndpoints:
    """The BRAIN API, typed."""

    def __init__(self, client: BrainClient) -> None:
        self.client = client

    # -- authentication ------------------------------------------------

    async def get_captcha(self) -> Challenge:
        """Fetch an ALTCHA proof-of-work challenge."""
        r = await self.client.request("GET", "/captcha")
        if not isinstance(r.body, dict):
            raise ValueError(f"Unexpected captcha payload: {r.body!r}")
        return Challenge.from_payload(r.body)

    async def solve_captcha(self) -> Solution:
        return await solve_async(await self.get_captcha())

    async def authenticate(self, email: str, password: str, *, captcha: str) -> AuthState:
        """Exchange Basic auth plus the solved captcha for a session cookie."""
        r = await self.client.request(
            "POST",
            "/authentication",
            json_body={"captcha": captcha},
            auth=(email, password),
        )
        return AuthState.model_validate(r.body or {})

    async def get_auth(self) -> AuthState | None:
        """Cheapest liveness check. ``None`` means no active session."""
        r = await self.client.request("GET", "/authentication", raise_for_status=False)
        if r.status == 401:
            error = self.client.to_error("GET", "/authentication", r)
            if isinstance(error, BrainVerificationRequired):
                # Not "no session": the session needs a browser check first.
                raise error
            return None
        if r.status == 204:
            return None
        if r.status == 429 or r.status >= 500:
            # Throttled or down, not signed out: callers must not drop the session for it.
            raise self.client.to_error("GET", "/authentication", r)
        if r.status >= 400 or not isinstance(r.body, dict):
            return None
        return AuthState.model_validate(r.body)

    async def logout(self) -> None:
        await self.client.request("DELETE", "/authentication", raise_for_status=False)
        self.client.clear_cookies()

    async def get_user(self, user_id: str = "self") -> dict[str, Any]:
        """Fetch user profile details from /users/{user_id}."""
        try:
            r = await self.client.request("GET", f"/users/{user_id}", raise_for_status=False)
            if r.status >= 400 or not isinstance(r.body, dict):
                return {}
            return r.body
        except BrainError:
            return {}

    # -- platform metadata ----------------------------------------------

    async def settings_schema(self) -> dict[str, Any]:
        """``OPTIONS /simulations`` at 4.0, resolved for ``REGULAR`` simulations.

        ``actions.POST.settings.children`` is the common tree; the ``REGULAR`` choice's
        ``settings.children`` overrides it, and region, universe, delay and neutralization
        live only there (``docs/wqb-api/schemas/simulation.md``, "Merging rule").
        """
        r = await self.client.request("OPTIONS", "/simulations", version=V_SETTINGS_SCHEMA)
        body = r.body if isinstance(r.body, dict) else {}
        post = body.get("actions", {}).get("POST", {})
        common = post.get("settings", {}).get("children", {})
        merged: dict[str, Any] = dict(common) if isinstance(common, dict) else {}
        for choice in post.get("type", {}).get("choices") or []:
            if isinstance(choice, dict) and choice.get("value") == SIMULATION_TYPE:
                overrides = (choice.get("settings") or {}).get("children")
                if isinstance(overrides, dict):
                    merged.update(overrides)
        return merged

    async def list_operators(self) -> list[Operator]:
        """Every operator with signature and category — the language reference."""
        r = await self.client.request("GET", "/operators")
        raw = r.body if isinstance(r.body, list) else []
        return [Operator.model_validate(o) for o in raw]

    # -- simulations -----------------------------------------------------

    async def create_simulation(
        self, payload: SimulationRequest | list[SimulationRequest]
    ) -> BrainResponse:
        """Start a simulation. Returns the raw response — the caller needs ``Location``.

        A ``201`` carries the simulation id **only** in the ``Location`` header, and for a
        multi-simulation that is the *parent* id, the only handle that can cancel the
        batch — so persist it before doing anything else. Parsing is left to
        :mod:`alpha_harness.engine.tracker`, which owns the ordering that keeps cancellation
        safe.
        """
        if isinstance(payload, list):
            body: Any = [p.to_wire() for p in payload]
        else:
            body = payload.to_wire()
        return await self.client.request("POST", "/simulations", json_body=body)

    async def read_simulation(self, simulation_id: str) -> BrainResponse:
        """One raw status read, errors included: the tracker decides what each one means."""
        return await self.client.request(
            "GET", f"/simulations/{simulation_id}", raise_for_status=False
        )

    async def cancel_simulation(self, simulation_id: str) -> bool:
        """Cancel a queued or running simulation.

        Returns ``False`` if the platform no longer knows the id, or refused because it
        already finished: that refusal is a ``200`` whose body is a list of messages
        (``["Can not delete complete simulations"]``), not an error status.
        """
        r = await self.client.request(
            "DELETE", f"/simulations/{simulation_id}", raise_for_status=False
        )
        return r.status < 400 and not (isinstance(r.body, list) and r.body)

    # -- alphas ----------------------------------------------------------

    async def get_alpha(self, alpha_id: str) -> Alpha:
        r = await self.client.request("GET", f"/alphas/{alpha_id}")
        return Alpha.model_validate(r.body or {"id": alpha_id})

    async def get_recordset(self, alpha_id: str, name: str) -> RecordSet:
        """Fetch a time series. Asynchronous — goes through the Retry-After protocol."""
        r = await self.client.poll(f"/alphas/{alpha_id}/recordsets/{name}")
        return RecordSet.model_validate(r.body or {})

    async def check_alpha(self, alpha_id: str) -> dict[str, Any]:
        """Re-run submission checks without submitting. Asynchronous."""
        r = await self.client.poll(f"/alphas/{alpha_id}/check")
        return r.body if isinstance(r.body, dict) else {}

    async def correlations(self, alpha_id: str, kind: str = "self") -> dict[str, Any]:
        """``self`` or ``prod`` correlation. Asynchronous.

        ``power-pool`` is undocumented but works.
        """
        r = await self.client.poll(f"/alphas/{alpha_id}/correlations/{kind}")
        return r.body if isinstance(r.body, dict) else {}

    async def alpha_body(self, alpha_id: str) -> dict[str, Any]:
        """``GET /alphas/{id}`` exactly as sent: the page reads blocks the model leaves out
        (``is.investabilityConstrained``, ``classifications``)."""
        r = await self.client.request("GET", f"/alphas/{alpha_id}")
        return r.body if isinstance(r.body, dict) else {}

    async def recordset_body(self, alpha_id: str, name: str) -> dict[str, Any]:
        r = await self.client.poll(f"/alphas/{alpha_id}/recordsets/{name}")
        return r.body if isinstance(r.body, dict) else {}

    async def before_and_after(self, alpha_id: str) -> dict[str, Any]:
        """The pool's stats before and after adding this Alpha.

        Asynchronous: answers with ``Retry-After`` first.
        """
        r = await self.client.poll(f"/users/self/alphas/{alpha_id}/before-and-after-performance")
        return r.body if isinstance(r.body, dict) else {}

    async def update_alpha(self, alpha_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        """``PATCH /alphas/{id}``: name, category, color, tags, description. Never submits."""
        r = await self.client.request("PATCH", f"/alphas/{alpha_id}", json_body=properties)
        return r.body if isinstance(r.body, dict) else {}

    # -- the alpha pool --------------------------------------------------
    #
    # Listing needs ``version=4.0`` and the filter DSL of
    # :mod:`alpha_harness.brain.filters`, appended to the path rather than passed as a
    # params dict — a dict helper encodes the operator into the value and the server then
    # matches nothing.
    #
    # There is deliberately no ``submit`` here: submission is irreversible, and the absence
    # of the method is the guarantee that no code path reaches it by mistake.

    async def list_alphas(self, query: AlphaQuery, user_id: str = "self") -> dict[str, Any]:
        """One page of your alphas, with the total match count."""
        r = await self.client.request("GET", query.path(user_id), version=V_ALPHA_LIST)
        body = r.body if isinstance(r.body, dict) else {}
        return {
            "count": int(body.get("count") or 0),
            "results": body.get("results") or [],
            "limit": query.limit,
            "offset": query.offset,
        }

    async def alphas_summary(self) -> dict[str, Any]:
        """Aggregate counts: ``{unsubmitted, active, decommissioned}``."""
        r = await self.client.request("GET", "/users/self/alphas/summary", version=V_ALPHA_SUMMARY)
        return r.body if isinstance(r.body, dict) else {}

    # -- data catalog ----------------------------------------------------

    async def list_data_categories(self) -> list[DataCategory]:
        """``GET /data-categories``: the whole taxonomy. It takes no parameters."""
        r = await self.client.request_retrying("GET", "/data-categories")
        raw = r.body if isinstance(r.body, list) else (r.body or {}).get("results", [])
        return [DataCategory.model_validate(c) for c in raw]

    async def list_data_fields_all(self, **params: Any) -> list[dict[str, Any]]:
        """Every field in one scope, in one request.

        Needs all four scope parameters and ``version=3.0`` (``docs/wqb-api/endpoints/data.md``).
        """
        r = await self.client.request_retrying(
            "GET", "/data-fields", version=V_FIELDS_ALL, params=params
        )
        body = r.body
        rows = (
            body
            if isinstance(body, list)
            else (body.get("results") if isinstance(body, dict) else None)
        )
        return rows if isinstance(rows, list) else []

    async def pyramid_multipliers(self) -> list[dict[str, Any]]:
        """``{category, region, delay, multiplier}`` for every pyramid on this account.

        The response shape is undocumented: ``{pyramids: [...]}``.
        """
        r = await self.client.request_retrying("GET", "/users/self/activities/pyramid-multipliers")
        items = r.body.get("pyramids") if isinstance(r.body, dict) else None
        return items if isinstance(items, list) else []

    async def pyramid_alphas(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        """``{category, region, delay, alphaCount}`` submitted between two ISO dates."""
        r = await self.client.request_retrying(
            "GET",
            "/users/self/activities/pyramid-alphas",
            params={"startDate": start_date, "endDate": end_date},
        )
        items = r.body.get("pyramids") if isinstance(r.body, dict) else None
        return items if isinstance(items, list) else []

    async def iter_data_sets(self, **params: Any) -> AsyncIterator[DataSet]:
        async for item in self._paginate("/data-sets", params):
            yield DataSet.model_validate(item)

    async def _paginate(
        self, path: str, params: dict[str, Any], *, page_size: int = PAGE_SIZE
    ) -> AsyncIterator[dict[str, Any]]:
        """Walk a DRF ``limit``/``offset`` list endpoint.

        Stops when a page comes back short or when ``count`` is reached, and guards
        against a server that keeps returning full pages forever.
        """
        offset = 0
        seen = 0
        total: int | None = None

        while True:
            # Retrying: abandoning a partially-walked list would under-report the catalog.
            r = await self.client.request_retrying(
                "GET", path, params={**params, "limit": page_size, "offset": offset}
            )
            body = r.body if isinstance(r.body, dict) else {}
            results = body.get("results") or []
            if total is None:
                total = body.get("count")

            for item in results:
                yield item
            seen += len(results)

            if len(results) < page_size:
                return
            if total is not None and seen >= total:
                return
            offset += page_size
