"""Reading the catalog: filters, facets, stats and field availability.

Every field is stored once per ``(instrumentType, region, delay, universe)`` tuple, so
browsing a market is a filter on one table and availability is a group-by across tuples.

All values are parameterised; the only interpolated identifiers are whitelisted column
names, because a user-supplied sort key must never reach SQL directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from ..db.duck import Catalog

#: Sortable columns. Anything not in here is rejected rather than interpolated.
SORTABLE = {
    "field_id",
    "dataset_id",
    "category_id",
    "coverage",
    "user_count",
    "alpha_count",
    "pyramid_multiplier",
    "field_type",
}


class Tuple4(BaseModel):
    """A catalog scope."""

    #: Reused predicate; ``params`` supplies its four placeholders in order.
    WHERE: ClassVar[str] = "instrument_type = ? AND region = ? AND delay = ? AND universe = ?"

    instrument_type: str = "EQUITY"
    region: str
    delay: int
    universe: str

    @property
    def params(self) -> list[Any]:
        return [self.instrument_type, self.region, self.delay, self.universe]

    @property
    def label(self) -> str:
        return f"{self.instrument_type}/{self.region}/D{self.delay}/{self.universe}"


class FieldFilter(BaseModel):
    """Everything the Data Explorer can narrow on.

    Every numeric attribute the API exposes is filterable — hiding a column the platform
    returns would defeat the purpose of the tab.
    """

    search: str | None = None
    dataset_ids: list[str] = Field(default_factory=list)
    category_ids: list[str] = Field(default_factory=list)
    subcategory_ids: list[str] = Field(default_factory=list)
    field_types: list[str] = Field(default_factory=list)

    coverage_min: float | None = None
    coverage_max: float | None = None
    alpha_count_min: int | None = None
    alpha_count_max: int | None = None
    user_count_min: int | None = None
    user_count_max: int | None = None
    pyramid_multiplier_min: float | None = None

    has_theme: bool | None = None

    sort_by: str = "alpha_count"
    sort_desc: bool = True
    limit: int = 100
    offset: int = 0

    def order_clause(self) -> str:
        column = self.sort_by if self.sort_by in SORTABLE else "alpha_count"
        direction = "DESC" if self.sort_desc else "ASC"
        # NULLS LAST keeps unpopulated metrics from crowding the top of a descending sort.
        return f"ORDER BY {column} {direction} NULLS LAST, field_id ASC"

    def where(self, scope: Tuple4) -> tuple[str, list[Any]]:
        clauses = [Tuple4.WHERE]
        params: list[Any] = list(scope.params)

        if self.search:
            needle = f"%{self.search.lower()}%"
            clauses.append("(lower(field_id) LIKE ? OR lower(description) LIKE ?)")
            params.extend([needle, needle])

        for column, values in (
            ("dataset_id", self.dataset_ids),
            ("category_id", self.category_ids),
            ("subcategory_id", self.subcategory_ids),
            ("field_type", self.field_types),
        ):
            if values:
                placeholders = ", ".join("?" for _ in values)
                clauses.append(f"{column} IN ({placeholders})")
                params.extend(values)

        for column, value, op in (
            ("coverage", self.coverage_min, ">="),
            ("coverage", self.coverage_max, "<="),
            ("alpha_count", self.alpha_count_min, ">="),
            ("alpha_count", self.alpha_count_max, "<="),
            ("user_count", self.user_count_min, ">="),
            ("user_count", self.user_count_max, "<="),
            ("pyramid_multiplier", self.pyramid_multiplier_min, ">="),
        ):
            if value is not None:
                clauses.append(f"{column} {op} ?")
                params.append(value)

        if self.has_theme is not None:
            clauses.append("themes IS NOT NULL" if self.has_theme else "themes IS NULL")

        return " AND ".join(clauses), params


class CatalogQueries:
    """Read-side of the catalog."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    # -- overview --------------------------------------------------------

    async def counts(self, scope: Tuple4) -> dict[str, int]:
        """Headline numbers for the Data Explorer's counts strip."""
        rows = await self.catalog.query(
            f"""
            SELECT
                (SELECT count(*) FROM data_field WHERE {Tuple4.WHERE})            AS fields,
                (SELECT count(DISTINCT dataset_id) FROM data_field WHERE {Tuple4.WHERE})
                                                                                  AS datasets,
                (SELECT count(DISTINCT category_id) FROM data_field
                    WHERE {Tuple4.WHERE} AND category_id IS NOT NULL)             AS categories,
                (SELECT count(DISTINCT subcategory_id) FROM data_field
                    WHERE {Tuple4.WHERE} AND subcategory_id IS NOT NULL)          AS subcategories
            """,  # noqa: S608
            scope.params * 4,
        )
        row = rows[0] if rows else {}
        return {
            key: int(row.get(key) or 0)
            for key in ("fields", "datasets", "categories", "subcategories")
        }

    async def synced_tuples(self) -> list[dict[str, Any]]:
        """Which scopes have data, and how much."""
        return await self.catalog.query(
            """
            SELECT instrument_type, region, delay, universe,
                   count(*) AS fields, max(synced_at) AS synced_at
            FROM data_field
            GROUP BY 1, 2, 3, 4
            ORDER BY region, delay, universe
            """
        )

    # -- fields ----------------------------------------------------------

    async def fields(self, scope: Tuple4, filters: FieldFilter) -> dict[str, Any]:
        """A filtered, sorted, paginated page of fields plus the total match count."""
        where, params = filters.where(scope)

        total = await self.catalog.scalar(f"SELECT count(*) FROM data_field WHERE {where}", params)  # noqa: S608
        rows = await self.catalog.query(
            f"""
            SELECT field_id, dataset_id, category_id, category_name,
                   subcategory_id, subcategory_name, description, field_type,
                   coverage, user_count, alpha_count, pyramid_multiplier, themes
            FROM data_field
            WHERE {where}
            {filters.order_clause()}
            LIMIT ? OFFSET ?
            """,  # noqa: S608
            [*params, filters.limit, filters.offset],
        )
        return {
            "total": int(total or 0),
            "limit": filters.limit,
            "offset": filters.offset,
            "results": rows,
        }

    async def field(self, scope: Tuple4, field_id: str) -> dict[str, Any] | None:
        rows = await self.catalog.query(
            f"SELECT * FROM data_field WHERE {Tuple4.WHERE} AND field_id = ?",  # noqa: S608
            [*scope.params, field_id],
        )
        return rows[0] if rows else None

    async def field_availability(self, field_id: str) -> list[dict[str, Any]]:
        """Every scope this field appears in.

        The check the Template Studio needs before expanding a template: a field that
        exists in USA/D1 may simply not exist in EUR/D0, and simulating it there fails.
        """
        return await self.catalog.query(
            """
            SELECT instrument_type, region, delay, universe, coverage, alpha_count, field_type
            FROM data_field WHERE field_id = ?
            ORDER BY region, delay, universe
            """,
            [field_id],
        )

    # -- datasets & facets -----------------------------------------------

    async def datasets(self, scope: Tuple4, search: str | None = None) -> list[dict[str, Any]]:
        clauses = [Tuple4.WHERE]
        params: list[Any] = list(scope.params)
        if search:
            needle = f"%{search.lower()}%"
            clauses.append(
                "(lower(dataset_id) LIKE ? OR lower(name) LIKE ? OR lower(description) LIKE ?)"
            )
            params.extend([needle, needle, needle])
        return await self.catalog.query(
            f"""
            SELECT dataset_id, name, description, category_id, category_name,
                   subcategory_id, subcategory_name, coverage, value_score,
                   user_count, alpha_count, field_count, pyramid_multiplier
            FROM data_set WHERE {" AND ".join(clauses)}
            ORDER BY value_score DESC NULLS LAST, dataset_id
            """,  # noqa: S608
            params,
        )

    async def category_tree(self, scope: Tuple4) -> list[dict[str, Any]]:
        """Category -> subcategory -> dataset counts.

        This is also the payload handed to the LLM: the full organisational hierarchy
        with metadata but *without* individual fields, which would blow the context
        window for no benefit.
        """
        rows = await self.catalog.query(
            f"""
            SELECT
                f.category_id,
                any_value(f.category_name)      AS category_name,
                f.subcategory_id,
                any_value(f.subcategory_name)   AS subcategory_name,
                count(DISTINCT f.dataset_id)    AS datasets,
                count(*)                        AS fields
            FROM data_field f
            WHERE {Tuple4.WHERE}
            GROUP BY f.category_id, f.subcategory_id
            ORDER BY f.category_id, f.subcategory_id
            """,  # noqa: S608
            scope.params,
        )

        tree: dict[str, dict[str, Any]] = {}
        for row in rows:
            cid = row["category_id"] or "uncategorised"
            node = tree.setdefault(
                cid,
                {
                    "id": cid,
                    "name": row["category_name"] or cid,
                    "datasets": 0,
                    "fields": 0,
                    "subcategories": [],
                },
            )
            node["fields"] += int(row["fields"] or 0)
            node["datasets"] += int(row["datasets"] or 0)
            if row["subcategory_id"]:
                node["subcategories"].append(
                    {
                        "id": row["subcategory_id"],
                        "name": row["subcategory_name"] or row["subcategory_id"],
                        "datasets": int(row["datasets"] or 0),
                        "fields": int(row["fields"] or 0),
                    }
                )
        return sorted(tree.values(), key=lambda n: -n["fields"])

    async def facets(
        self, scope: Tuple4, filters: FieldFilter | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        """Distinct values with counts under the other active filters, for the filter controls.

        A facet ignores its own selection, and a level of the Category → Subcategory →
        Dataset hierarchy also ignores the levels below it, so choosing MATRIX still shows
        how many VECTOR fields the rest of the filter would match.
        """
        active = filters or FieldFilter()

        async def group(
            column: str, ignore: tuple[str, ...], *extra: tuple[str, str]
        ) -> list[dict[str, Any]]:
            """Distinct ``column`` values with counts, plus ``any_value`` of each extra column."""
            where, params = active.model_copy(update={name: [] for name in ignore}).where(scope)
            select_extra = "".join(f", any_value({source}) AS {alias}" for source, alias in extra)
            return await self.catalog.query(
                f"""
                SELECT {column} AS id{select_extra}, count(*) AS n
                FROM data_field
                WHERE {where} AND {column} IS NOT NULL
                GROUP BY {column} ORDER BY n DESC
                """,  # noqa: S608
                params,
            )

        levels = ("category_ids", "subcategory_ids", "dataset_ids")
        return {
            "categories": await group("category_id", levels, ("category_name", "name")),
            # Each level names its parent, so the filters can be picked top-down:
            # Category → Subcategory → Dataset.
            "subcategories": await group(
                "subcategory_id",
                levels[1:],
                ("subcategory_name", "name"),
                ("category_id", "category_id"),
            ),
            "datasets": await group(
                "dataset_id",
                levels[2:],
                ("category_id", "category_id"),
                ("subcategory_id", "subcategory_id"),
            ),
            "types": await group("field_type", ("field_types",)),
        }

    async def stats(self, scope: Tuple4) -> dict[str, Any]:
        """Distribution summary — lets the UI set sensible filter ranges."""
        rows = await self.catalog.query(
            f"""
            SELECT
                min(coverage) AS coverage_min, max(coverage) AS coverage_max,
                median(coverage) AS coverage_median,
                max(alpha_count) AS alpha_count_max,
                max(user_count) AS user_count_max,
                max(pyramid_multiplier) AS pyramid_multiplier_max
            FROM data_field WHERE {Tuple4.WHERE}
            """,  # noqa: S608
            scope.params,
        )
        return rows[0] if rows else {}
