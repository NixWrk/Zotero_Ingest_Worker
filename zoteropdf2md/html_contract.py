"""Canonical HTML profile shared by source and PDF article outputs."""

from __future__ import annotations

import html as html_lib
import re
from collections import Counter
from html.parser import HTMLParser
from typing import Any


CANONICAL_HTML_PROFILE = "z2m-canonical-article/v1"
CANONICAL_HTML_PROFILE_VERSION = 1
DOCUMENT_ROOT_ATTR = "data-z2m-document-root"
DOCUMENT_KIND_ATTR = "data-z2m-document-kind"
PROFILE_ATTR = "data-z2m-profile"
PROFILE_VERSION_ATTR = "data-z2m-profile-version"
NODE_KIND_ATTR = "data-z2m-node-kind"

_OPEN_TAG_RE = re.compile(r"<(?P<tag>[A-Za-z][\w:-]*)\b(?P<attrs>[^<>]*?)>", re.DOTALL)
_REFERENCE_ID_RE = re.compile(
    r"^(?:(?:ref|bib|bibr|citation|cit|iopbib|en)(?:[-_.:].*|\d.*)|[br]\d+)$",
    re.IGNORECASE,
)
_PROTECTED_BLOCK_RE = re.compile(
    r"<!--[\s\S]*?-->|<(?P<tag>script|style)\b[^>]*>[\s\S]*?</(?P=tag)\s*>",
    re.IGNORECASE,
)


def canonical_document_root_count(html: str, *, document_kind: str) -> int:
    """Return the number of real legacy roots for one canonical document kind."""

    return len(_document_root_matches(html, document_kind=document_kind))


def _document_root_matches(html: str, *, document_kind: str) -> list[re.Match[str]]:
    if document_kind not in {"source", "pdf"}:
        raise ValueError(f"Unsupported canonical document kind: {document_kind}")
    expected_id = "web-doc" if document_kind == "source" else "marker-doc"
    root_pattern = re.compile(
        rf"<main\b(?P<attrs>[^<>]*(?<![\w:-])id\s*=\s*(['\"]){re.escape(expected_id)}\2[^<>]*)>",
        re.IGNORECASE | re.DOTALL,
    )
    return _unprotected_matches(root_pattern, html)


def normalize_canonical_html(
    html: str,
    *,
    document_kind: str,
    provenance_kind: str,
) -> str:
    """Stamp one document root and deterministic semantic node metadata."""

    if document_kind not in {"source", "pdf"}:
        raise ValueError(f"Unsupported canonical document kind: {document_kind}")
    provenance_kind = provenance_kind.strip()
    if not provenance_kind:
        raise ValueError("Canonical provenance kind must not be empty")

    roots = _document_root_matches(html, document_kind=document_kind)
    if len(roots) != 1:
        expected_id = "web-doc" if document_kind == "source" else "marker-doc"
        raise ValueError(
            f"Expected one {document_kind} document root #{expected_id}, found {len(roots)}"
        )
    root = roots[0]
    opening = root.group(0)
    opening = _set_attr(opening, DOCUMENT_ROOT_ATTR, "1")
    opening = _set_attr(opening, PROFILE_ATTR, CANONICAL_HTML_PROFILE)
    opening = _set_attr(opening, PROFILE_VERSION_ATTR, str(CANONICAL_HTML_PROFILE_VERSION))
    opening = _set_attr(opening, DOCUMENT_KIND_ATTR, document_kind)
    opening = _set_attr(opening, "data-z2m-provenance-kind", provenance_kind)
    normalized = html[: root.start()] + opening + html[root.end() :]
    return _normalize_semantic_nodes(normalized)


def canonical_contract_report(html: str) -> dict[str, Any]:
    parsed = _parse_document(html)
    roots = parsed.roots
    failures: list[str] = []
    warnings: list[str] = []
    document_kind = ""
    provenance_kind = ""
    if len(roots) != 1:
        failures.append("document_root_count")
    else:
        attrs = roots[0]
        if attrs.get(PROFILE_ATTR, "") != CANONICAL_HTML_PROFILE:
            failures.append("profile_mismatch")
        if attrs.get(PROFILE_VERSION_ATTR, "") != str(CANONICAL_HTML_PROFILE_VERSION):
            failures.append("profile_version_mismatch")
        document_kind = attrs.get(DOCUMENT_KIND_ATTR, "")
        if document_kind not in {"source", "pdf"}:
            failures.append("document_kind_invalid")
        expected_id = "web-doc" if document_kind == "source" else "marker-doc"
        if document_kind in {"source", "pdf"} and attrs.get("id", "") != expected_id:
            failures.append("document_root_id_mismatch")
        provenance_kind = attrs.get("data-z2m-provenance-kind", "")
        if not provenance_kind:
            failures.append("provenance_missing")

    ids = parsed.ids
    duplicate_ids = sorted(key for key, count in Counter(ids).items() if key and count > 1)
    if duplicate_ids:
        failures.append("duplicate_ids")
    duplicate_attributes = sorted(set(parsed.duplicate_attributes))
    if duplicate_attributes:
        failures.append("duplicate_attributes")
    id_set = {value for value in ids if value}
    missing_targets = sorted(parsed.internal_targets - id_set)
    if missing_targets:
        warnings.append("missing_internal_link_targets")

    semantics = _semantic_counts(parsed.tags)
    for kind in ("section", "figure", "reference"):
        if semantics[f"{kind}_annotated"] != semantics[f"{kind}_total"]:
            failures.append(f"{kind}_semantics_missing")

    return {
        "schema_version": 1,
        "profile": CANONICAL_HTML_PROFILE,
        "profile_version": CANONICAL_HTML_PROFILE_VERSION,
        "document_kind": document_kind,
        "provenance_kind": provenance_kind,
        "status": "failed" if failures else "warning" if warnings else "passed",
        "failures": failures,
        "warnings": warnings,
        "duplicate_ids": duplicate_ids[:50],
        "duplicate_attributes": duplicate_attributes[:50],
        "missing_internal_link_targets": missing_targets[:50],
        "semantics": semantics,
    }


def _normalize_semantic_nodes(html: str) -> str:
    used_ids = {identifier for identifier in _parse_document(html).ids if identifier}
    counters = {"section": 0, "figure": 0}

    def annotate(match: re.Match[str]) -> str:
        tag = match.group("tag").lower()
        if tag not in counters:
            return match.group(0)
        opening = match.group(0)
        opening = _append_attr_token(opening, NODE_KIND_ATTR, tag)
        if _attr_value(match.group("attrs"), "id"):
            return opening
        counters[tag] += 1
        candidate = f"{tag[:3]}-{counters[tag]}"
        while candidate in used_ids:
            counters[tag] += 1
            candidate = f"{tag[:3]}-{counters[tag]}"
        used_ids.add(candidate)
        return _set_attr(opening, "id", candidate)

    def annotate_reference(match: re.Match[str]) -> str:
        opening = match.group(0)
        attrs = match.group("attrs")
        if not _is_reference_node(attrs):
            return opening
        return _append_attr_token(opening, NODE_KIND_ATTR, "reference")

    def normalize_markup(fragment: str) -> str:
        normalized = _OPEN_TAG_RE.sub(annotate, fragment)
        return _OPEN_TAG_RE.sub(annotate_reference, normalized)

    return _transform_unprotected(html, normalize_markup)


def _semantic_counts(tags: list[tuple[str, dict[str, str]]]) -> dict[str, int]:
    totals = {"section": 0, "figure": 0, "reference": 0}
    annotated = {"section": 0, "figure": 0, "reference": 0}
    for tag, attrs in tags:
        identifier = attrs.get("id", "")
        node_kinds = set(attrs.get(NODE_KIND_ATTR, "").split())
        if tag in {"section", "figure"}:
            totals[tag] += 1
            if tag in node_kinds and identifier:
                annotated[tag] += 1
        if _is_reference_attrs(attrs):
            totals["reference"] += 1
            if "reference" in node_kinds:
                annotated["reference"] += 1
    return {
        "section_total": totals["section"],
        "section_annotated": annotated["section"],
        "figure_total": totals["figure"],
        "figure_annotated": annotated["figure"],
        "reference_total": totals["reference"],
        "reference_annotated": annotated["reference"],
    }


def _attr_value(attrs: str, name: str) -> str:
    match = re.search(
        rf"(?<![\w:-]){re.escape(name)}\s*=\s*(['\"])(?P<value>.*?)\1",
        attrs,
        re.IGNORECASE | re.DOTALL,
    )
    return html_lib.unescape(match.group("value")).strip() if match else ""


def _is_reference_node(attrs: str) -> bool:
    return _is_reference_attrs(
        {
            name: _attr_value(attrs, name)
            for name in ("id", "href", "role", "epub:type")
        }
    )


def _is_reference_attrs(attrs: dict[str, str]) -> bool:
    identifier = attrs.get("id", "")
    href = attrs.get("href", "")
    target = href[1:] if href.startswith("#") else ""
    roles = set(attrs.get("role", "").lower().split())
    epub_types = set(attrs.get("epub:type", "").lower().split())
    return bool(
        _REFERENCE_ID_RE.match(identifier)
        or _REFERENCE_ID_RE.match(target)
        or roles.intersection({"doc-biblioref", "doc-biblioentry", "doc-endnote"})
        or epub_types.intersection({"biblioref", "biblioentry", "endnote"})
    )


class _ContractParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str]]] = []
        self.roots: list[dict[str, str]] = []
        self.ids: list[str] = []
        self.internal_targets: set[str] = set()
        self.duplicate_attributes: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self._record(tag, attrs)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self._record(tag, attrs)

    def _record(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        names = [name.lower() for name, _value in attrs]
        for name, count in Counter(names).items():
            if count > 1:
                self.duplicate_attributes.append(f"{tag}:{name}")
        values = {
            name.lower(): html_lib.unescape(value or "").strip()
            for name, value in attrs
        }
        self.tags.append((tag, values))
        identifier = values.get("id", "")
        if identifier:
            self.ids.append(identifier)
        href = values.get("href", "")
        if href.startswith("#") and len(href) > 1:
            self.internal_targets.add(href[1:])
        if tag == "main" and values.get(DOCUMENT_ROOT_ATTR) == "1":
            self.roots.append(values)


def _parse_document(html: str) -> _ContractParser:
    parser = _ContractParser()
    parser.feed(html)
    parser.close()
    return parser


def _unprotected_matches(pattern: re.Pattern[str], html: str) -> list[re.Match[str]]:
    spans = [match.span() for match in _PROTECTED_BLOCK_RE.finditer(html)]
    return [
        match
        for match in pattern.finditer(html)
        if not any(start <= match.start() < end for start, end in spans)
    ]


def _transform_unprotected(html: str, transform: Any) -> str:
    parts: list[str] = []
    cursor = 0
    for match in _PROTECTED_BLOCK_RE.finditer(html):
        parts.append(transform(html[cursor : match.start()]))
        parts.append(match.group(0))
        cursor = match.end()
    parts.append(transform(html[cursor:]))
    return "".join(parts)


def _append_attr_token(tag: str, name: str, token: str) -> str:
    current = _attr_value(tag, name)
    tokens = current.split()
    if token in tokens:
        return tag
    tokens.append(token)
    return _set_attr(tag, name, " ".join(tokens))


def _set_attr(tag: str, name: str, value: str) -> str:
    escaped = html_lib.escape(value, quote=True)
    pattern = re.compile(
        rf"(?P<prefix>\s{re.escape(name)}\s*=\s*)(?P<quote>['\"])(?P<value>.*?)(?P=quote)",
        re.IGNORECASE | re.DOTALL,
    )
    if pattern.search(tag):
        return pattern.sub(lambda match: f'{match.group("prefix")}"{escaped}"', tag, count=1)
    close = "/>" if tag.endswith("/>") else ">"
    return f'{tag[: -len(close)]} {name}="{escaped}"{close}'
