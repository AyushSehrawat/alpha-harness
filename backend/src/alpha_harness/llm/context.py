"""What the model is told about the data.

The payload is the organisational hierarchy — every category, its subcategories, and
every dataset with its metadata — and deliberately **not the individual data fields**: a
synced scope holds tens of thousands of them, and serialising those would fill the context
window and leave no room for the model to think. Fields are fetched on demand once the
model has narrowed to a dataset.

A dataset's **value score** (how underutilized BRAIN considers it) beside its **alpha
count** is where an edge is most likely to survive, so the payload says that outright
rather than hoping the model infers it.

Everything is rendered compactly, because tokens spent on JSON punctuation are tokens not
spent on the answer.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from ..catalog.queries import CatalogQueries, Tuple4

log = structlog.get_logger(__name__)

#: Roughly four characters per token. Only used to warn before a call, never to bill.
CHARS_PER_TOKEN = 4

#: Datasets per subcategory in the compact rendering. High enough to include everything
#: in practice; a guard rather than a policy.
MAX_DATASETS = 400

#: Descriptions are trimmed. The first sentence carries what a dataset is; the rest is
#: usually vendor boilerplate.
DESCRIPTION_CHARS = 180


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def _number(value: Any, digits: int = 2) -> str:
    if value is None:
        return "-"
    try:
        number = float(value)
    except TypeError, ValueError:
        return str(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.{digits}f}"


def loads_or(text: str, **default: Any) -> dict[str, Any]:
    """A model's JSON reply, or ``default`` when it is not usable.

    A reply the consultant can read is worth more than a parse error, so callers degrade
    rather than raise.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError, TypeError:
        return default
    return payload if isinstance(payload, dict) else default


def clip(text: Any, limit: int) -> str:
    """One line, at most ``limit`` characters, broken on a word.

    Whitespace is collapsed first: descriptions arrive from the platform with newlines
    in them, and a prompt or a table cell wants one line.
    """
    clean = " ".join(str(text or "").split())
    return clean if len(clean) <= limit else clean[:limit].rsplit(" ", 1)[0] + "…"


class ContextBuilder:
    """Turns the synced catalog into something a model can reason over."""

    def __init__(self, queries: CatalogQueries) -> None:
        self.queries = queries

    async def tree(self, scope: Tuple4) -> dict[str, Any]:
        """The category → subcategory → dataset hierarchy, with metadata, without fields."""
        counts = await self.queries.counts(scope)
        categories = await self.queries.category_tree(scope)
        datasets = await self.queries.datasets(scope)

        by_subcategory: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in datasets[:MAX_DATASETS]:
            key = (str(row.get("category_id") or ""), str(row.get("subcategory_id") or ""))
            by_subcategory.setdefault(key, []).append(row)

        nodes: list[dict[str, Any]] = []
        for category in categories:
            subcategories = []
            for sub in category["subcategories"]:
                rows = by_subcategory.get((category["id"], sub["id"]), [])
                subcategories.append(
                    {
                        "id": sub["id"],
                        "name": sub["name"],
                        "fields": sub["fields"],
                        "datasets": [
                            {
                                "id": r["dataset_id"],
                                "name": r.get("name") or r["dataset_id"],
                                "description": clip(r.get("description"), DESCRIPTION_CHARS),
                                "fields": r.get("field_count"),
                                "coverage": r.get("coverage"),
                                "valueScore": r.get("value_score"),
                                "userCount": r.get("user_count"),
                                "alphaCount": r.get("alpha_count"),
                                "pyramidMultiplier": r.get("pyramid_multiplier"),
                            }
                            for r in rows
                        ],
                    }
                )
            nodes.append(
                {
                    "id": category["id"],
                    "name": category["name"],
                    "fields": category["fields"],
                    "datasets": category["datasets"],
                    "subcategories": subcategories,
                }
            )

        return {"scope": scope.label, "counts": counts, "categories": nodes}

    async def render(self, scope: Tuple4) -> tuple[str, dict[str, Any]]:
        """The tree as compact text, plus what it cost.

        Text rather than JSON: the same information in half the tokens.
        """
        tree = await self.tree(scope)
        counts = tree["counts"]

        lines = [
            f"DATA CATALOG — {tree['scope']}",
            (
                f"{counts['categories']} categories · {counts['subcategories']} subcategories "
                f"· {counts['datasets']} datasets · {counts['fields']} fields"
            ),
            "",
            "Columns per dataset: id | name | fields | coverage | value score | alphas built",
            "A high value score with a low alpha count is an under-explored dataset.",
            "",
        ]

        for category in tree["categories"]:
            lines.append(
                f"## {category['name']} [{category['id']}] "
                f"— {category['datasets']} datasets, {category['fields']} fields"
            )
            for sub in category["subcategories"]:
                if not sub["datasets"]:
                    continue
                lines.append(f"  ### {sub['name']} [{sub['id']}]")
                for dataset in sub["datasets"]:
                    lines.append(
                        f"    - {dataset['id']} | {dataset['name']} | "
                        f"{_number(dataset['fields'])}f | "
                        f"cov {_number(dataset['coverage'])} | "
                        f"vs {_number(dataset['valueScore'])} | "
                        f"{_number(dataset['alphaCount'])} alphas"
                    )
                    if dataset["description"]:
                        lines.append(f"        {dataset['description']}")
            lines.append("")

        text = "\n".join(lines)
        meta = {
            "scope": tree["scope"],
            "counts": counts,
            "characters": len(text),
            "estimatedTokens": estimate_tokens(text),
        }
        log.info("llm.context.built", **meta)
        return text, meta
