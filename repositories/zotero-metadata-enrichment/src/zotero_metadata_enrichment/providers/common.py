from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..models import FullTextLocation, MetadataCandidate
from ..text import normalize_space, strip_html


CandidateLookup = Callable[[], MetadataCandidate | None]
CandidateLookupByIdentifier = Callable[[str], MetadataCandidate | None]


def bind_candidate_lookup(
    lookup: CandidateLookupByIdentifier,
    identifier: str,
) -> CandidateLookup:
    return lambda: lookup(identifier)


def as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def first_value(value: Any) -> str:
    if isinstance(value, list):
        for item in value:
            text = first_value(item)
            if text:
                return text
        return ""
    if isinstance(value, dict):
        for key in ("value", "title", "name", "text"):
            if key in value:
                return first_value(value.get(key))
        return ""
    return normalize_space(str(value or ""))


def first_text(value: Any) -> str:
    return normalize_space(strip_html(first_value(value)))


def date_from_parts(*parts: Any) -> str:
    values = [str(part).strip() for part in parts if str(part or "").strip()]
    if not values:
        return ""
    year = values[0]
    if not year:
        return ""
    result = year
    if len(values) > 1 and values[1].isdigit():
        result += f"-{int(values[1]):02d}"
    if len(values) > 2 and values[2].isdigit():
        result += f"-{int(values[2]):02d}"
    return result


def compact_pages(first_page: str, last_page: str) -> str:
    first_page = normalize_space(first_page)
    last_page = normalize_space(last_page)
    if first_page and last_page and first_page != last_page:
        return f"{first_page}-{last_page}"
    return first_page or last_page


def location_dicts(locations: list[FullTextLocation]) -> list[dict[str, Any]]:
    return [location.to_dict() for location in locations if location.url]


def candidate_with_locations(
    *,
    source: str,
    identifier: str,
    score: float,
    fields: dict[str, str],
    raw: dict[str, Any],
    locations: list[FullTextLocation],
) -> MetadataCandidate:
    payload = dict(raw)
    payload["full_text_locations"] = location_dicts(locations)
    return MetadataCandidate(
        source=source,
        identifier=identifier,
        score=score,
        fields={key: normalize_space(value) for key, value in fields.items() if normalize_space(value)},
        raw=payload,
    )
