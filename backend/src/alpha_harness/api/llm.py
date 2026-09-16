"""The assistant: keys, models, prompts, and the context it is shown.

``GET /api/llm/prompts`` returns every system prompt in full. That is deliberate — the
prompt decides what an answer looks like and is otherwise invisible, so it is served
rather than hidden. Nothing here is a secret; the keys are, and those only ever leave as
a masked hint.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from ..catalog.queries import Tuple4
from ..llm.keys import serialise
from ..llm.prompts import PROMPTS
from ..llm.providers import catalogue
from ..llm.registry import LLMModel, LLMModels
from ..schemas import Out
from .deps import State

router = APIRouter(prefix="/api/llm", tags=["assistant"])


class Scope(BaseModel):
    instrument_type: str = "EQUITY"
    region: str
    delay: int
    universe: str

    def to_tuple(self) -> Tuple4:
        return Tuple4(
            instrument_type=self.instrument_type,
            region=self.region,
            delay=self.delay,
            universe=self.universe,
        )


class PromptInfo(Out):
    slug: str
    label: str
    purpose: str
    context: Literal["none", "catalog_tree", "dataset_fields"]
    model: str | None
    temperature: float
    body: str
    characters: int
    estimated_tokens: int


class PromptList(Out):
    prompts: list[PromptInfo]


class LLMKeyUsage(Out):
    key_id: int
    model: str
    #: Pacific day, YYYY-MM-DD.
    day: str
    requests: int
    tokens: int
    last_request_at: str | None


class LLMKey(Out):
    id: int
    label: str
    provider: str
    hint: str
    enabled: bool
    last_ok_at: str | None
    last_error: str | None
    created_at: str | None
    usage: list[LLMKeyUsage]


class LLMBudget(Out):
    model: str
    label: str
    provider: str
    per_key_per_day: int
    remaining_today: int
    bulk: bool


class LLMKeyStatus(Out):
    keys: list[LLMKey]
    enabled: int
    budget: list[LLMBudget]
    reset_in_seconds: int
    quota_timezone: str


class LLMProvider(Out):
    id: str
    label: str
    base_url: str
    onboarding_url: str
    key_hint: str
    free_note: str
    openai_compatible: bool
    #: Empty for google: its models are in the roster.
    models: list[LLMModel]


class LLMProviders(Out):
    providers: list[LLMProvider]
    default: str
    note: str


class KeyWorks(Out):
    key_id: int
    ok: Literal[True]
    #: How many models the key can reach, and those new to the roster.
    models: int
    new_models: list[str]


class KeyFailed(Out):
    key_id: int
    ok: Literal[False]
    error: str


def _check(result: dict[str, Any]) -> KeyWorks | KeyFailed:
    if result["ok"]:
        return KeyWorks.model_validate(result)
    return KeyFailed.model_validate(result)


class ContextCounts(Out):
    fields: int
    datasets: int
    categories: int
    subcategories: int


class LLMContextRendered(Out):
    text: str
    scope: str
    counts: ContextCounts
    characters: int
    estimated_tokens: int


class LLMContextTree(Out):
    scope: str
    counts: ContextCounts
    #: category -> subcategory -> dataset, with metadata and no fields.
    categories: list[dict[str, Any]]


# --- setup ----------------------------------------------------------------


@router.get("/models")
async def models(state: State) -> LLMModels:
    """The model roster with each one's daily budget.

    Requests-per-day varies twenty-five-fold across these models and is the limit that
    ends a session, so it is returned with every entry rather than hidden in a help page.
    """
    return LLMModels.model_validate(state.llm.registry.to_dict())


# --- prompts --------------------------------------------------------------


@router.get("/prompts")
async def list_prompts() -> PromptList:
    """Every prompt, in full.

    Served rather than hidden: a prompt decides what an answer looks like and is
    otherwise invisible in the output.
    """
    return PromptList.model_validate(
        {
            "prompts": [
                {
                    "slug": p.slug,
                    "label": p.label,
                    "purpose": p.purpose,
                    "context": p.context,
                    "model": p.model,
                    "temperature": p.temperature,
                    "body": p.body,
                    "characters": len(p.body),
                    # Roughly four characters to a token. Worth showing: prompt tokens come
                    # out of the same per-minute budget as the answer.
                    "estimatedTokens": max(1, len(p.body) // 4),
                }
                for p in PROMPTS.values()
            ],
        }
    )


@router.get("/keys")
async def list_keys(state: State) -> LLMKeyStatus:
    """Keys, today's usage, and how much budget is left across all of them."""
    return LLMKeyStatus.model_validate(await state.llm.keys.status(state.llm.registry))


@router.get("/providers")
async def providers() -> LLMProviders:
    """Every assistant that can answer, and how to get a free key for it.

    All of them are free and need no card. That is the selection rule: the assistant is
    optional here, and a provider asking for payment details turns an optional
    convenience into a purchase decision.
    """
    return LLMProviders.model_validate(catalogue())


class AddKey(BaseModel):
    key: str = Field(description="An assistant API key. Sealed at rest; never returned.")
    label: str | None = Field(default=None, description="Which account this key belongs to")
    provider: str = Field(default="google", description="Whose key this is")


@router.post("/keys", status_code=201)
async def add_key(body: AddKey, state: State) -> LLMKey:
    """Store a key.

    Quota is per account, so adding a key from a second account genuinely doubles the
    daily budget — which is why the same key cannot be added twice.
    """
    row = await state.llm.keys.add(body.key, body.label, provider=body.provider)
    return LLMKey.model_validate(serialise(row))


@router.post("/keys/{key_id}/check")
async def check_key(key_id: int, state: State) -> KeyWorks | KeyFailed:
    """Confirm a key works. Costs nothing against the generation quota."""
    return _check(await state.llm.check_key(key_id))


@router.post("/keys/check")
async def check_all_keys(state: State) -> list[KeyWorks | KeyFailed]:
    return [_check(c) for c in await state.llm.check_all()]


class KeyToggle(BaseModel):
    enabled: bool


@router.put("/keys/{key_id}")
async def toggle_key(key_id: int, body: KeyToggle, state: State) -> LLMKey:
    return LLMKey.model_validate(serialise(await state.llm.keys.set_enabled(key_id, body.enabled)))


@router.delete("/keys/{key_id}", status_code=204)
async def remove_key(key_id: int, state: State) -> None:
    await state.llm.keys.remove(key_id)
    state.llm.forget(key_id)


# --- context --------------------------------------------------------------


@router.get("/context")
async def context(
    state: State,
    region: str,
    delay: int,
    universe: str,
    instrument_type: str = "EQUITY",
    rendered: Annotated[
        bool, Query(description="Return the exact text the model receives")
    ] = False,
) -> LLMContextRendered | LLMContextTree:
    """Exactly what the model is shown about your data.

    The hierarchy with metadata, and no individual fields — tens of thousands of field
    names would fill the context window and leave no room to think. Set ``rendered`` to
    read the literal text, so nothing about the assistant is a black box.
    """
    scope = Scope(
        instrument_type=instrument_type, region=region, delay=delay, universe=universe
    ).to_tuple()
    if rendered:
        text, meta = await state.llm.context.render(scope)
        return LLMContextRendered.model_validate({"text": text, **meta})
    return LLMContextTree.model_validate(await state.llm.context.tree(scope))
