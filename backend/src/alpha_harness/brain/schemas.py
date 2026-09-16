"""Pydantic models of BRAIN API objects.

Shapes are taken from ``docs/wqb-api``. The wire format is camelCase; models accept either
spelling and serialise back to camelCase.

Deliberately permissive — ``extra="allow"`` everywhere — because the platform adds fields
over time and unknown ones should survive into the UI rather than be silently dropped.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Self

import msgspec
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel


class BrainModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="allow",
    )


# --- enumerations ---------------------------------------------------------
# Resolve the authoritative values at runtime from OPTIONS /simulations. These exist for
# ergonomics and for the few places a literal is genuinely fixed.


class SimulationType(StrEnum):
    REGULAR = "REGULAR"
    SUPER = "SUPER"


class SimulationStatus(StrEnum):
    """Every simulation ``status`` (``docs/wqb-documentation/brain-api/brain-api.md``).

    ``WAITING`` and ``SIMULATING`` are still running; the rest are final.
    """

    WAITING = "WAITING"
    SIMULATING = "SIMULATING"
    CANCELLED = "CANCELLED"
    COMPLETE = "COMPLETE"
    WARNING = "WARNING"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    FAIL = "FAIL"


class CheckResult(StrEnum):
    PASS = "PASS"  # noqa: S105 - check outcome, not a password
    FAIL = "FAIL"
    PENDING = "PENDING"
    WARNING = "WARNING"
    ERROR = "ERROR"


# --- authentication -------------------------------------------------------


class TokenInfo(BrainModel):
    expiry: float | None = None


class AuthUser(BrainModel):
    id: str


class AuthState(BrainModel):
    """Response of ``GET``/``POST /authentication``."""

    user: AuthUser | None = None
    token: TokenInfo | None = None
    permissions: list[str] = Field(default_factory=list)

    @property
    def user_id(self) -> str | None:
        return self.user.id if self.user else None

    @property
    def is_consultant(self) -> bool:
        return "CONSULTANT" in self.permissions

    @property
    def can_multi_simulate(self) -> bool:
        """Gates batching."""
        return "MULTI_SIMULATION" in self.permissions


# --- simulation -----------------------------------------------------------


class SimulationSettings(BrainModel):
    """The settings block of a simulation request.

    Only ``instrument_type``/``region``/``universe``/``delay`` are universally required;
    the rest carry platform defaults. Validate combinations against
    ``OPTIONS /simulations`` rather than trusting these types.
    """

    instrument_type: str = "EQUITY"
    region: str
    universe: str
    delay: int
    decay: int = 0
    neutralization: str = "NONE"
    truncation: float = 0.08
    pasteurization: str = "ON"
    unit_handling: str = "VERIFY"
    #: Always ``ON`` in what this application sends; see :class:`SimulationRequest`.
    nan_handling: str = "ON"
    language: str = "FASTEXPR"
    visualization: bool = False
    test_period: str | None = None
    max_trade: str | None = None
    max_position: str | None = None

    @property
    def batch_key(self) -> tuple[str, str, int, str]:
        """The fields a multi-simulation's children must agree on.

        ``type`` is held on the request rather than the settings, so the packer combines
        this with it (``docs/wqb-documentation/consultant-information/
        multi-alpha-simulation.md``).
        """
        return (self.instrument_type, self.region, self.delay, self.language)


#: BRAIN's test period: the last two of the ten years are held out.
TEST_PERIOD = "P2Y0M0D"


class SimulationRequest(BrainModel):
    """The body of ``POST /simulations``.

    Note the asymmetry with :class:`Alpha`: on submission the expression key is a bare
    string; on retrieval the same key expands into an object.
    """

    type: SimulationType = SimulationType.REGULAR
    settings: SimulationSettings
    regular: str | None = None
    combo: str | None = None
    selection: str | None = None

    @model_validator(mode="after")
    def _handle_nans(self) -> Self:
        """Force NaN Handling on, and hold out a test period unless one was named.

        Enforced here because every request — from a lab, a template or the simulations
        API — becomes this model before it is hashed or sent. The held-out years let the
        Pool hide Alphas that collapse out of sample; BRAIN still runs the submission
        checks on the whole period.
        """
        update: dict[str, Any] = {}
        if self.settings.nan_handling != "ON":
            update["nan_handling"] = "ON"
        if self.settings.test_period is None:
            update["test_period"] = TEST_PERIOD
        if update:
            self.settings = self.settings.model_copy(update=update)
        return self

    def to_wire(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)

    @property
    def batch_key(self) -> tuple[str, str, str, int, str]:
        """5-tuple that all children of one multi-simulation must share."""
        instrument, region, delay, language = self.settings.batch_key
        return (str(self.type), instrument, region, delay, language)


# --- alpha ----------------------------------------------------------------


class Check(BrainModel):
    """One entry of the submission-check array."""

    name: str
    result: CheckResult | None = None
    limit: float | None = None
    value: float | None = None
    # MATCHES_COMPETITION carries arrays instead of a numeric value.
    matched: list[Any] | None = None
    unmatched: list[Any] | None = None


class SampleStats(BrainModel):
    """The ``is`` / ``os`` / ``prod`` statistics block."""

    pnl: float | None = None
    book_size: float | None = None
    long_count: int | None = None
    short_count: int | None = None
    turnover: float | None = None
    returns: float | None = None
    drawdown: float | None = None
    margin: float | None = None
    fitness: float | None = None
    sharpe: float | None = None
    start_date: str | None = None
    checks: list[Check] = Field(default_factory=list)


class AlphaCode(BrainModel):
    """The expanded expression object returned on retrieval."""

    code: str | None = None
    description: str | None = None
    operator_count: int | None = None


class Alpha(BrainModel):
    """Response of ``GET /alphas/{id}``."""

    id: str
    type: SimulationType | None = None
    author: str | None = None
    settings: SimulationSettings | None = None
    regular: AlphaCode | None = None
    combo: AlphaCode | None = None
    selection: AlphaCode | None = None
    date_created: datetime | None = None
    date_submitted: datetime | None = None
    date_modified: datetime | None = None
    name: str | None = None
    favorite: bool = False
    hidden: bool = False
    color: str | None = None
    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    grade: str | None = None
    stage: str | None = None
    status: str | None = None
    # `is` is a Python keyword; the wire name is restored on serialisation.
    in_sample: SampleStats | None = Field(default=None, alias="is")
    os: SampleStats | None = None
    #: Undocumented: the years before a test period.
    train: SampleStats | None = None
    test: SampleStats | None = None
    prod: SampleStats | None = None

    @property
    def expression(self) -> str | None:
        for code in (self.regular, self.combo, self.selection):
            if code and code.code:
                return code.code
        return None


# --- recordsets -----------------------------------------------------------


class RecordProperty(BrainModel):
    name: str
    title: str | None = None
    type: str | None = None


class RecordSchema(BrainModel):
    name: str | None = None
    title: str | None = None
    properties: list[RecordProperty] = Field(default_factory=list)


class RecordSet(BrainModel):
    """A column-oriented time series.

    The wire format is a ``schema`` describing columns plus ``records`` as positional
    arrays — you must zip them yourself. :meth:`rows` does that.
    """

    schema_: RecordSchema = Field(default_factory=RecordSchema, alias="schema")
    records: list[list[Any]] = Field(default_factory=list)

    def rows(self) -> list[dict[str, Any]]:
        """Zip ``records`` against the column names.

        Rows shorter than the schema are padded with ``None`` rather than raising — a
        truncated series should still render.
        """
        names = [p.name for p in self.schema_.properties]
        if not names:
            return []
        out: list[dict[str, Any]] = []
        for record in self.records:
            padded = list(record) + [None] * (len(names) - len(record))
            out.append(dict(zip(names, padded, strict=False)))
        return out


# --- data catalog ---------------------------------------------------------


class DataCategoryRef(BrainModel):
    id: str
    name: str | None = None


class DataSet(BrainModel):
    """An entry of ``GET /data-sets``."""

    id: str
    name: str | None = None
    description: str | None = None
    category: DataCategoryRef | None = None
    subcategory: DataCategoryRef | None = None
    region: str | None = None
    delay: int | None = None
    universe: str | None = None
    coverage: float | None = None
    value_score: float | None = None
    user_count: int | None = None
    alpha_count: int | None = None
    field_count: int | None = None
    themes: list[Any] = Field(default_factory=list)
    pyramid_multiplier: float | None = None
    research_papers: list[Any] = Field(default_factory=list)


class FieldRef(msgspec.Struct):
    """A dataset, category or subcategory named inside a data field."""

    id: str | None = None
    name: str | None = None


class BulkField(msgspec.Struct, rename="camel"):
    """One entry of ``GET /data-fields``, decoded straight from the response bytes.

    A whole market is ~85k of these, where validating Pydantic models cost three times as
    much and blocked the event loop while it did. Only the columns the catalog stores are
    declared; msgspec skips the rest of each object.
    """

    #: Required: a field without one cannot be keyed, and a null key corrupts DuckDB's index.
    id: str
    description: str | None = None
    type: str | None = None
    coverage: float | None = None
    user_count: int | None = None
    alpha_count: int | None = None
    pyramid_multiplier: float | None = None
    themes: list[str] | None = None
    dataset: FieldRef | None = None
    category: FieldRef | None = None
    subcategory: FieldRef | None = None


class BulkFields(msgspec.Struct):
    """The enveloped shape of ``GET /data-fields``, when it answers with one."""

    results: list[BulkField] = []


#: Reused: building the decoders once is most of their cost.
BULK_FIELDS = msgspec.json.Decoder(list[BulkField])
BULK_FIELDS_ENVELOPE = msgspec.json.Decoder(BulkFields)


class DataField(BrainModel):
    """An entry of ``GET /data-fields``."""

    id: str
    description: str | None = None
    dataset: DataCategoryRef | None = None
    category: DataCategoryRef | None = None
    subcategory: DataCategoryRef | None = None
    region: str | None = None
    delay: int | None = None
    universe: str | None = None
    type: str | None = None
    coverage: float | None = None
    user_count: int | None = None
    alpha_count: int | None = None
    themes: list[Any] = Field(default_factory=list)
    pyramid_multiplier: float | None = None


class DataCategory(BrainModel):
    """An entry of ``GET /data-categories``. Categories nest one level."""

    id: str
    name: str | None = None
    subcategories: list[DataCategory] = Field(default_factory=list)
    dataset_count: int | None = None
    field_count: int | None = None
    value_score: float | None = None


class Operator(BrainModel):
    """An entry of ``GET /operators`` — the language reference."""

    name: str
    category: str | None = None
    scope: list[str] = Field(default_factory=list)
    definition: str | None = None
    description: str | None = None
    documentation: str | None = None
    level: str | None = None


class Page[T](BrainModel):
    """DRF-standard ``limit``/``offset`` envelope."""

    count: int = 0
    next: str | None = None
    previous: str | None = None
    results: list[T] = Field(default_factory=list)

    @model_validator(mode="after")
    def _count_defaults_to_len(self) -> Self:
        if not self.count and self.results:
            object.__setattr__(self, "count", len(self.results))
        return self
