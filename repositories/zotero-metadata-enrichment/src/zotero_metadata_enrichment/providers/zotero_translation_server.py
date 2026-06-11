from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..diff import merge_extra
from ..identifiers import extract_arxiv_id_from_text, extract_pmcid_from_text, extract_pmid_from_text, normalize_doi
from ..models import FullTextLocation, MetadataCandidate
from ..text import join_values, normalize_space, strip_html, title_match_score


class TranslationServerClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: int = 60,
        user_agent: str = "zotero-metadata-enrichment/0.1",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

    def search(self, identifier: str, *, expected_title: str = "") -> list[MetadataCandidate]:
        payload = self._post_text("/search", identifier)
        items = payload if isinstance(payload, list) else []
        return [
            candidate
            for item in items
            if isinstance(item, dict)
            for candidate in [
                zotero_translator_item_to_candidate(
                    item,
                    source="zotero_translation_server_search",
                    identifier=identifier,
                    default_score=1.0,
                    expected_title=expected_title,
                )
            ]
            if candidate is not None
        ]

    def web(self, url: str, *, expected_title: str = "") -> list[MetadataCandidate]:
        payload = self._post_text("/web", url)
        if isinstance(payload, dict) and payload.get("items") and payload.get("session"):
            return []
        items = payload if isinstance(payload, list) else []
        return [
            candidate
            for item in items
            if isinstance(item, dict)
            for candidate in [
                zotero_translator_item_to_candidate(
                    item,
                    source="zotero_translation_server_web",
                    identifier=url,
                    default_score=0.95,
                    expected_title=expected_title,
                )
            ]
            if candidate is not None
        ]

    def _post_text(self, path: str, text: str) -> Any:
        url = f"{self.base_url}{path}"
        request = urllib.request.Request(
            url,
            data=text.encode("utf-8"),
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Accept": "application/json",
                "User-Agent": self.user_agent,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 300:
                return json.loads(exc.read().decode("utf-8"))
            if exc.code in {404, 501}:
                return []
            raise


def zotero_translator_item_to_candidate(
    item: dict[str, Any],
    *,
    source: str,
    identifier: str,
    default_score: float,
    expected_title: str = "",
) -> MetadataCandidate | None:
    data = item.get("data") if isinstance(item.get("data"), dict) else item
    title = normalize_space(str(data.get("title") or ""))
    doi = normalize_doi(str(data.get("DOI") or ""))
    if not title and not doi:
        return None

    fields: dict[str, str] = {
        "title": title,
        "abstractNote": strip_html(str(data.get("abstractNote") or "")),
        "date": str(data.get("date") or ""),
        "language": str(data.get("language") or ""),
        "shortTitle": str(data.get("shortTitle") or ""),
        "archive": str(data.get("archive") or ""),
        "archiveLocation": str(data.get("archiveLocation") or data.get("archiveID") or ""),
        "libraryCatalog": str(data.get("libraryCatalog") or "Zotero translators"),
        "rights": str(data.get("rights") or ""),
        "extra": str(data.get("extra") or ""),
        "publicationTitle": str(data.get("publicationTitle") or ""),
        "journalAbbreviation": str(data.get("journalAbbreviation") or ""),
        "DOI": doi,
        "ISSN": join_values(data.get("ISSN")),
        "PMID": str(data.get("PMID") or ""),
        "PMCID": str(data.get("PMCID") or ""),
        "volume": str(data.get("volume") or ""),
        "issue": str(data.get("issue") or ""),
        "pages": str(data.get("pages") or ""),
        "series": str(data.get("series") or ""),
        "seriesTitle": str(data.get("seriesTitle") or ""),
        "publisher": str(data.get("publisher") or ""),
        "place": str(data.get("place") or ""),
        "ISBN": join_values(data.get("ISBN")),
        "edition": str(data.get("edition") or ""),
        "numPages": str(data.get("numPages") or ""),
        "numberOfVolumes": str(data.get("numberOfVolumes") or ""),
        "bookTitle": str(data.get("bookTitle") or ""),
        "url": str(data.get("url") or ""),
        "accessDate": str(data.get("accessDate") or ""),
        "institution": str(data.get("institution") or ""),
        "reportType": str(data.get("reportType") or ""),
        "reportNumber": str(data.get("reportNumber") or ""),
        "conferenceName": str(data.get("conferenceName") or ""),
        "proceedingsTitle": str(data.get("proceedingsTitle") or ""),
        "websiteTitle": str(data.get("websiteTitle") or ""),
        "websiteType": str(data.get("websiteType") or ""),
        "genre": str(data.get("genre") or ""),
    }
    if not fields["archiveLocation"]:
        arxiv_id = extract_arxiv_id_from_text(metadata_text_from_item(data))
        if arxiv_id:
            fields["archive"] = fields["archive"] or "arXiv"
            fields["archiveLocation"] = arxiv_id
            fields["extra"] = merge_extra(fields["extra"], f"arXiv:{arxiv_id}")
    metadata_text = metadata_text_from_item(data)
    if not fields["PMID"]:
        fields["PMID"] = extract_pmid_from_text(metadata_text) or ""
    if not fields["PMCID"]:
        fields["PMCID"] = extract_pmcid_from_text(metadata_text) or ""

    score = default_score
    if expected_title and title:
        title_score = title_match_score(expected_title, title)
        exact_identifier = bool(
            normalize_doi(identifier)
            and doi
            and normalize_doi(identifier) == doi
        ) or bool(
            extract_arxiv_id_from_text(identifier)
            and fields.get("archiveLocation")
            and extract_arxiv_id_from_text(identifier) == fields["archiveLocation"]
        )
        score = max(title_score, 0.95 if exact_identifier else 0.0)

    locations = zotero_translator_locations(data)
    raw = {
        "itemType": data.get("itemType"),
        "creators": data.get("creators") or [],
        "tags": data.get("tags") or [],
        "attachments": data.get("attachments") or [],
        "publicationTitle": data.get("publicationTitle"),
        "full_text_locations": [location.to_dict() for location in locations],
    }
    return MetadataCandidate(
        source=source,
        identifier=identifier,
        score=score,
        fields={key: normalize_space(value) for key, value in fields.items() if normalize_space(value)},
        raw=raw,
    )


def zotero_translator_locations(item: dict[str, Any]) -> list[FullTextLocation]:
    locations: list[FullTextLocation] = []
    seen: set[str] = set()

    def add(
        *,
        url: str,
        source: str,
        kind: str,
        content_type: str = "",
        raw: dict[str, Any] | None = None,
    ) -> None:
        url = normalize_space(url)
        if not url.lower().startswith(("http://", "https://")):
            return
        key = url.casefold()
        if key in seen:
            return
        seen.add(key)
        locations.append(
            FullTextLocation(
                source=source,
                url=url,
                kind=kind,
                content_type=content_type,
                repository="Zotero translators",
                raw=raw or {},
            )
        )

    item_url = normalize_space(str(item.get("url") or ""))
    if item_url:
        add(url=item_url, source="zotero_translation_server", kind=guess_translator_url_kind(item_url), raw={"field": "url"})

    raw_attachments = item.get("attachments")
    if not isinstance(raw_attachments, list):
        return locations
    for attachment in raw_attachments:
        if not isinstance(attachment, dict):
            continue
        url = first_attachment_url(attachment)
        content_type = normalize_space(str(attachment.get("mimeType") or attachment.get("contentType") or ""))
        title = normalize_space(str(attachment.get("title") or ""))
        if not url:
            continue
        add(
            url=url,
            source="zotero_translation_server_attachment",
            kind=guess_translator_attachment_kind(url=url, title=title, content_type=content_type),
            content_type=content_type,
            raw=attachment,
        )
    return locations


def first_attachment_url(attachment: dict[str, Any]) -> str:
    for key in ("url", "openURL", "openUrl", "downloadURL", "downloadUrl"):
        value = normalize_space(str(attachment.get(key) or ""))
        if value:
            return value
    return ""


def guess_translator_attachment_kind(*, url: str, title: str, content_type: str) -> str:
    haystack = f"{url} {title} {content_type}".casefold()
    if "pdf" in haystack or url.lower().split("?", 1)[0].endswith(".pdf"):
        return "pdf"
    if "html" in haystack or "snapshot" in haystack:
        return "html"
    return "landing"


def guess_translator_url_kind(url: str) -> str:
    lowered = url.casefold()
    if lowered.split("?", 1)[0].endswith(".pdf") or "/pdf/" in lowered:
        return "pdf"
    return "landing"


def metadata_text_from_item(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for value in item.values():
        if isinstance(value, (str, int, float)):
            parts.append(str(value))
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, dict):
                    parts.extend(str(v) for v in child.values() if isinstance(v, (str, int, float)))
                elif isinstance(child, (str, int, float)):
                    parts.append(str(child))
    return "\n".join(parts)
