"""Which assistants can answer, and how to get a key for each.

Every provider here has a **free tier that needs no card**. That is the whole selection
rule: the assistant is optional, so a provider that asks for payment details first turns an
optional convenience into a purchase decision.

All of them speak the OpenAI chat-completions protocol, Google through its compatible
endpoint, which is why one small client in :mod:`.openai_compat` serves every one.

**A key only ever answers for its own provider**, so rotation filters on provider before it
looks at budget.

**The limits below are transcribed, not measured**, read off documentation pages and kept
deliberately conservative: a budget guessed high spends someone's day before anyone notices
it was wrong, and a budget guessed low only costs a rotation.
"""

from dataclasses import dataclass

from ..schemas import Out
from .registry import ModelInfo


@dataclass(frozen=True, slots=True)
class Provider:
    """One assistant, and what it takes to start using it."""

    id: str
    label: str
    #: Where its chat-completions endpoint lives.
    base_url: str
    #: Where to get a key. Shown as a link, because "search for it" loses people.
    onboarding_url: str
    #: What a key from this provider looks like, so a pasted wrong one is caught early.
    key_hint: str
    #: True for a provider that bills the user. Kept apart everywhere it is shown, and its
    #: keys are refused without a daily cap — see :meth:`KeyStore.add`.
    paid: bool = False
    #: What a paid tier bills, in plain words; shown above its key field.
    tier_note: str = ""
    models: tuple[ModelInfo, ...] = ()


class LLMProvider(Out):
    """:class:`Provider` on the wire."""

    id: str
    label: str
    onboarding_url: str
    key_hint: str
    tier_note: str
    #: True for a provider that bills the user rather than offering a free tier.
    paid: bool
    #: Empty for google: its models are in the roster.
    models: list[ModelInfo]


class LLMProviders(Out):
    providers: list[LLMProvider]
    default: str
    paid_note: str


def _model(
    model_id: str,
    label: str,
    provider: str,
    *,
    rpm: int,
    rpd: int,
    tpm: int = 60_000,
    bulk: bool = False,
    recommended: bool = False,
) -> ModelInfo:
    return ModelInfo(
        id=model_id,
        label=label,
        kind="text",
        rpm=rpm,
        tpm=tpm,
        rpd=rpd,
        bulk=bulk,
        recommended=recommended,
        provider=provider,
    )


PROVIDERS: dict[str, Provider] = {
    "google": Provider(
        id="google",
        label="Google AI Studio",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        onboarding_url="https://aistudio.google.com/apikey",
        key_hint="AIza…",
        # Google's roster is the built-in table in `registry`, which carries transcribed
        # per-model budgets. Repeating it here would give it two sources of truth.
        models=(),
    ),
    "groq": Provider(
        id="groq",
        label="Groq",
        base_url="https://api.groq.com/openai/v1",
        onboarding_url="https://console.groq.com/keys",
        key_hint="gsk_…",
        models=(
            _model(
                "llama-3.3-70b-versatile",
                "Llama 3.3 70B",
                "groq",
                rpm=30,
                rpd=1_000,
                recommended=True,
            ),
            _model(
                "llama-3.1-8b-instant",
                "Llama 3.1 8B Instant",
                "groq",
                rpm=30,
                rpd=14_400,
                bulk=True,
            ),
        ),
    ),
    "cerebras": Provider(
        id="cerebras",
        label="Cerebras",
        base_url="https://api.cerebras.ai/v1",
        onboarding_url="https://cloud.cerebras.ai/",
        key_hint="csk-…",
        models=(
            _model(
                "llama-3.3-70b",
                "Llama 3.3 70B",
                "cerebras",
                rpm=30,
                rpd=14_400,
                bulk=True,
                recommended=True,
            ),
            _model(
                "qwen-3-32b",
                "Qwen 3 32B",
                "cerebras",
                rpm=30,
                rpd=14_400,
                bulk=True,
            ),
        ),
    ),
    "openrouter": Provider(
        id="openrouter",
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        onboarding_url="https://openrouter.ai/keys",
        key_hint="sk-or-…",
        models=(
            _model(
                "deepseek/deepseek-r1:free",
                "DeepSeek R1 (free)",
                "openrouter",
                rpm=20,
                rpd=50,
            ),
            _model(
                "meta-llama/llama-3.3-70b-instruct:free",
                "Llama 3.3 70B (free)",
                "openrouter",
                rpm=20,
                rpd=50,
                recommended=True,
            ),
        ),
    ),
    "nvidia": Provider(
        id="nvidia",
        label="NVIDIA NIM",
        base_url="https://integrate.api.nvidia.com/v1",
        onboarding_url="https://build.nvidia.com/",
        key_hint="nvapi-…",
        models=(
            _model(
                "meta/llama-3.3-70b-instruct",
                "Llama 3.3 70B",
                "nvidia",
                rpm=40,
                rpd=1_000,
                recommended=True,
            ),
        ),
    ),
    "mistral": Provider(
        id="mistral",
        label="Mistral",
        base_url="https://api.mistral.ai/v1",
        onboarding_url="https://console.mistral.ai/api-keys/",
        key_hint="…",
        models=(
            _model(
                "mistral-small-latest",
                "Mistral Small",
                "mistral",
                rpm=60,
                rpd=1_000,
                recommended=True,
            ),
            _model(
                "open-mistral-nemo",
                "Mistral Nemo",
                "mistral",
                rpm=60,
                rpd=1_000,
                bulk=True,
            ),
        ),
    ),
    "github": Provider(
        id="github",
        label="GitHub Models",
        base_url="https://models.github.ai/inference",
        onboarding_url="https://github.com/settings/tokens",
        key_hint="ghp_… or github_pat_…",
        models=(
            _model(
                "openai/gpt-4o-mini",
                "GPT-4o mini",
                "github",
                rpm=15,
                rpd=150,
                recommended=True,
            ),
        ),
    ),
    "huggingface": Provider(
        id="huggingface",
        label="Hugging Face",
        base_url="https://router.huggingface.co/v1",
        onboarding_url="https://huggingface.co/settings/tokens",
        key_hint="hf_…",
        models=(
            _model(
                "meta-llama/Llama-3.3-70B-Instruct",
                "Llama 3.3 70B",
                "huggingface",
                rpm=10,
                rpd=200,
                recommended=True,
            ),
        ),
    ),
    # --- bring your own, and your own bill ---------------------------------------------
    #
    # Everything above is free because the assistant is optional and a card turns an
    # optional convenience into a purchase decision. These two are for the user who already
    # pays for one of them and would rather spend that than a free tier that runs out by
    # lunchtime. They are kept behind their own heading in the UI so the default stays
    # "no card", and a key for either is refused without a daily request cap: the free
    # providers stop on their own, and these stop only when told to.
    "openai": Provider(
        id="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        onboarding_url="https://platform.openai.com/api-keys",
        key_hint="sk-…",
        tier_note=(
            "Your own OpenAI account — billed to you, not free. The models appear once the "
            "key has been checked, because which ones an account can reach depends on it."
        ),
        paid=True,
        # Discovered from the key's own ``GET /models`` rather than transcribed: OpenAI's
        # roster moves, and a stale id here would fail with "model not found" on a key that
        # is perfectly good.
        models=(),
    ),
    "anthropic": Provider(
        id="anthropic",
        # Anthropic's OpenAI-compatible endpoint, which takes the same Bearer token and the
        # same ``/chat/completions`` shape, so no second client is needed. Anthropic calls
        # it a compatibility layer for evaluation rather than a production path; the ways it
        # differs that matter here are that ``response_format`` is ignored — callers already
        # ask for JSON in the prompt and parse defensively — and that thinking output is not
        # returned, which nothing here reads.
        label="Anthropic Claude",
        base_url="https://api.anthropic.com/v1",
        onboarding_url="https://platform.claude.com/settings/keys",
        key_hint="sk-ant-…",
        tier_note="Your own Anthropic account — billed to you, not free.",
        paid=True,
        models=(
            _model(
                "claude-sonnet-5",
                "Claude Sonnet 5",
                "anthropic",
                rpm=50,
                rpd=0,
                recommended=True,
            ),
            _model(
                "claude-opus-5",
                "Claude Opus 5",
                "anthropic",
                rpm=50,
                rpd=0,
            ),
            _model(
                "claude-haiku-4-5-20251001",
                "Claude Haiku 4.5",
                "anthropic",
                rpm=50,
                rpd=0,
                bulk=True,
            ),
        ),
    ),
}

DEFAULT_PROVIDER = "google"

#: Said once, under the paid heading. The cap is the whole point: a free tier stops by
#: itself and a paid account does not, so the app will not hold one of these keys until it
#: has been told where to stop.
PAID_NOTE = (
    "These bill your own account. Nothing here is needed — every provider above is free — "
    "but a key you already pay for does not run out at lunchtime. You set a daily request "
    "cap when you add one, and Alpha Harness stops at it."
)


def provider_models() -> list[ModelInfo]:
    """Every model reachable through a provider other than Google."""
    return [model for provider in PROVIDERS.values() for model in provider.models]


def get(provider_id: str | None) -> Provider:
    """One provider, or Google when nothing was said."""
    return PROVIDERS.get(provider_id or DEFAULT_PROVIDER, PROVIDERS[DEFAULT_PROVIDER])


def catalogue() -> LLMProviders:
    """Every provider, Google first and the paid ones last, for the screen where a key is
    added. The order is the argument: free is the default because it is first and because
    nothing below the fold is needed to use the app."""
    return LLMProviders(
        providers=[
            LLMProvider.model_validate(p, from_attributes=True)
            for p in sorted(PROVIDERS.values(), key=lambda p: p.paid)
        ],
        default=DEFAULT_PROVIDER,
        paid_note=PAID_NOTE,
    )
