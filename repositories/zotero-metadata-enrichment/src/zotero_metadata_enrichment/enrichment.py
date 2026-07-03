from __future__ import annotations

import urllib.error
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .diff import build_metadata_diff, merge_extra
from .identifiers import (
    extract_arxiv_id_from_text,
    extract_doi_from_text,
    extract_isbn_from_text,
    extract_pmcid_from_text,
    extract_pmid_from_text,
    normalize_doi,
    normalize_pmcid,
    normalize_pmid,
)
from .models import EnrichmentResult, LocalAttachment, LocalItemMetadata, MetadataCandidate
from .provider_http import register_retry_after_from_http_error
from .providers import (
    ArxivClient,
    BioRxivClient,
    CoreClient,
    CrossrefClient,
    DataCiteClient,
    DoajClient,
    EuropePmcClient,
    OpenAireClient,
    OpenAlexClient,
    PubMedClient,
    SemanticScholarClient,
    TranslationServerClient,
    UnpaywallClient,
)
from .text import normalize_space


@dataclass(frozen=True)
class EnricherConfig:
    translation_server_url: str = ""
    translation_server_timeout_seconds: int = 60
    crossref_mailto: str = ""
    unpaywall_email: str = ""
    openalex_api_key: str = ""
    semantic_scholar_api_key: str = ""
    core_api_key: str = ""
    request_timeout_seconds: int = 60
    user_agent: str = "zotero-metadata-enrichment/0.1"
    metadata_title_min_score: float = 0.86
    arxiv_search_min_score: float = 0.88
    policy: str = "emptyFieldsOnly"
    extended_providers_enabled: bool = True


class MetadataEnricher:
    def __init__(
        self,
        config: EnricherConfig | None = None,
        *,
        translation_server: TranslationServerClient | None = None,
        crossref: CrossrefClient | None = None,
        arxiv: ArxivClient | None = None,
        pubmed: PubMedClient | None = None,
        unpaywall: UnpaywallClient | None = None,
        openalex: OpenAlexClient | None = None,
        europe_pmc: EuropePmcClient | None = None,
        semantic_scholar: SemanticScholarClient | None = None,
        datacite: DataCiteClient | None = None,
        biorxiv: BioRxivClient | None = None,
        core: CoreClient | None = None,
        openaire: OpenAireClient | None = None,
        doaj: DoajClient | None = None,
    ) -> None:
        self.config = config or EnricherConfig()
        self.translation_server = translation_server
        if self.translation_server is None and self.config.translation_server_url:
            self.translation_server = TranslationServerClient(
                self.config.translation_server_url,
                timeout_seconds=self.config.translation_server_timeout_seconds,
                user_agent=self.config.user_agent,
            )
        self.crossref = crossref or CrossrefClient(
            mailto=self.config.crossref_mailto,
            timeout_seconds=self.config.request_timeout_seconds,
            user_agent=self.config.user_agent,
        )
        self.pubmed = pubmed or PubMedClient(
            timeout_seconds=self.config.request_timeout_seconds,
            user_agent=self.config.user_agent,
        )
        self.arxiv = arxiv or ArxivClient(
            timeout_seconds=self.config.request_timeout_seconds,
            user_agent=self.config.user_agent,
        )
        self.unpaywall = unpaywall or UnpaywallClient(
            email=self.config.unpaywall_email or self.config.crossref_mailto,
            timeout_seconds=self.config.request_timeout_seconds,
            user_agent=self.config.user_agent,
        )
        self.openalex = openalex or OpenAlexClient(
            mailto=self.config.crossref_mailto,
            api_key=self.config.openalex_api_key,
            timeout_seconds=self.config.request_timeout_seconds,
            user_agent=self.config.user_agent,
        )
        self.europe_pmc = europe_pmc or EuropePmcClient(
            timeout_seconds=self.config.request_timeout_seconds,
            user_agent=self.config.user_agent,
        )
        self.semantic_scholar = semantic_scholar or SemanticScholarClient(
            api_key=self.config.semantic_scholar_api_key,
            timeout_seconds=self.config.request_timeout_seconds,
            user_agent=self.config.user_agent,
        )
        self.datacite = datacite or DataCiteClient(
            timeout_seconds=self.config.request_timeout_seconds,
            user_agent=self.config.user_agent,
        )
        self.biorxiv = biorxiv or BioRxivClient(
            timeout_seconds=self.config.request_timeout_seconds,
            user_agent=self.config.user_agent,
        )
        self.core = core or CoreClient(
            api_key=self.config.core_api_key,
            timeout_seconds=self.config.request_timeout_seconds,
            user_agent=self.config.user_agent,
        )
        self.openaire = openaire or OpenAireClient(
            timeout_seconds=self.config.request_timeout_seconds,
            user_agent=self.config.user_agent,
        )
        self.doaj = doaj or DoajClient(
            timeout_seconds=self.config.request_timeout_seconds,
            user_agent=self.config.user_agent,
        )
        self.provider_events: list[dict[str, Any]] = []

    def enrich(
        self,
        *,
        metadata: LocalItemMetadata,
        attachment: LocalAttachment,
        policy: str | None = None,
    ) -> EnrichmentResult:
        self.provider_events = []
        candidate = self.lookup_candidate(metadata=metadata, attachment=attachment)
        if candidate is None:
            return EnrichmentResult(
                candidate=None,
                diff=None,
                provider_events=list(self.provider_events),
                reason="no_confident_candidate",
            )
        diff = build_metadata_diff(
            candidate,
            current_fields=metadata.fields,
            policy=policy or self.config.policy,
        )
        return EnrichmentResult(
            candidate=candidate,
            diff=diff,
            provider_events=list(self.provider_events),
            reason="matched",
        )

    def lookup_candidate(
        self,
        *,
        metadata: LocalItemMetadata,
        attachment: LocalAttachment,
    ) -> MetadataCandidate | None:
        haystack = metadata_haystack(metadata, attachment)
        doi = metadata.fields.get("DOI") or extract_doi_from_text(haystack)
        arxiv_id = extract_arxiv_id_from_text(haystack)
        pmid = normalize_pmid(metadata.fields.get("PMID") or extract_pmid_from_text(haystack) or "") or None
        pmcid = normalize_pmcid(metadata.fields.get("PMCID") or extract_pmcid_from_text(haystack) or "") or None
        title = title_for_lookup(metadata, attachment)
        candidates: list[MetadataCandidate] = []

        candidate = self.lookup_via_translation_server(
            metadata=metadata,
            attachment=attachment,
            doi=doi,
            arxiv_id=arxiv_id,
            pmid=pmid,
            pmcid=pmcid,
        )
        if candidate is not None:
            candidates.append(candidate)

        if doi:
            normalized_doi = normalize_doi(doi)
            candidate = self.safe_lookup(provider="crossref", identifier=normalized_doi, lookup=lambda doi=normalized_doi: self.crossref.by_doi(doi))
            if candidate is not None:
                candidates.append(candidate)

        if pmid:
            normalized_pmid = normalize_pmid(pmid)
            candidate = self.safe_lookup(provider="pubmed", identifier=normalized_pmid, lookup=lambda pmid=normalized_pmid: self.pubmed.by_pmid(pmid))
            if candidate is not None:
                candidates.append(candidate)

        if pmcid:
            normalized_pmcid = normalize_pmcid(pmcid)
            candidate = self.safe_lookup(provider="pubmed", identifier=normalized_pmcid, lookup=lambda pmcid=normalized_pmcid: self.pubmed.by_pmcid(pmcid))
            if candidate is not None:
                candidates.append(candidate)

        if arxiv_id:
            candidate = self.safe_lookup(provider="arxiv", identifier=arxiv_id, lookup=lambda arxiv_id=arxiv_id: self.arxiv.by_id(arxiv_id))
            if candidate is not None:
                candidates.append(candidate)

        if self.config.extended_providers_enabled:
            candidates.extend(
                self.lookup_via_extended_identifier_providers(
                    metadata=metadata,
                    doi=doi,
                    arxiv_id=arxiv_id,
                    pmid=pmid,
                    pmcid=pmcid,
                )
            )

        if not title:
            self.record_provider_event(provider="lookup", status="no_title")
            return merge_metadata_candidates(candidates)

        candidate = self.safe_lookup(provider="crossref", identifier=title, lookup=lambda title=title: self.crossref.by_title(title))
        if candidate is not None:
            if candidate.score >= self.config.metadata_title_min_score:
                candidates.append(candidate)
            else:
                self.record_provider_event(provider="crossref", status="low_confidence", identifier=title, score=candidate.score, min_score=self.config.metadata_title_min_score)

        if self.config.extended_providers_enabled:
            for provider, lookup in (
                ("openalex", lambda: self.openalex.by_title(title)),
                ("semantic_scholar", lambda: self.semantic_scholar.by_title(title)),
                ("core", lambda: self.core.by_title(title)),
                ("doaj", lambda: self.doaj.by_title(title)),
            ):
                candidate = self.safe_lookup(provider=provider, identifier=title, lookup=lookup)
                if candidate is not None and candidate.score >= self.config.metadata_title_min_score:
                    candidates.append(candidate)
                elif candidate is not None:
                    self.record_provider_event(provider=provider, status="low_confidence", identifier=title, score=candidate.score, min_score=self.config.metadata_title_min_score)

        candidate = self.safe_lookup(provider="arxiv", identifier=title, lookup=lambda title=title: self.arxiv.by_title(title))
        if candidate is not None and candidate.score >= self.config.arxiv_search_min_score:
            candidates.append(candidate)
        elif candidate is not None:
            self.record_provider_event(provider="arxiv", status="low_confidence", identifier=title, score=candidate.score, min_score=self.config.arxiv_search_min_score)
        return merge_metadata_candidates(candidates)

    def lookup_via_extended_identifier_providers(
        self,
        *,
        metadata: LocalItemMetadata,
        doi: str | None,
        arxiv_id: str | None,
        pmid: str | None,
        pmcid: str | None,
    ) -> list[MetadataCandidate]:
        lookups: list[tuple[str, str, Any]] = []
        if doi:
            normalized_doi = normalize_doi(doi)
            lookups.extend(
                [
                    ("datacite", normalized_doi, lambda doi=normalized_doi: self.datacite.by_doi(doi)),
                    ("europe_pmc", normalized_doi, lambda doi=normalized_doi: self.europe_pmc.by_doi(doi)),
                    ("openalex", normalized_doi, lambda doi=normalized_doi: self.openalex.by_doi(doi)),
                    ("semantic_scholar", normalized_doi, lambda doi=normalized_doi: self.semantic_scholar.by_doi(doi)),
                    ("unpaywall", normalized_doi, lambda doi=normalized_doi: self.unpaywall.by_doi(doi)),
                    ("biorxiv_medrxiv", normalized_doi, lambda doi=normalized_doi: self.biorxiv.by_doi(doi)),
                    ("core", normalized_doi, lambda doi=normalized_doi: self.core.by_doi(doi)),
                    ("openaire", normalized_doi, lambda doi=normalized_doi: self.openaire.by_doi(doi)),
                    ("doaj", normalized_doi, lambda doi=normalized_doi: self.doaj.by_doi(doi)),
                ]
            )
        if pmid:
            normalized_pmid = normalize_pmid(pmid)
            lookups.extend(
                [
                    ("europe_pmc", normalized_pmid, lambda pmid=normalized_pmid: self.europe_pmc.by_pmid(pmid)),
                    ("openalex", normalized_pmid, lambda pmid=normalized_pmid: self.openalex.by_pmid(pmid)),
                    ("semantic_scholar", normalized_pmid, lambda pmid=normalized_pmid: self.semantic_scholar.by_pmid(pmid)),
                ]
            )
        if pmcid:
            normalized_pmcid = normalize_pmcid(pmcid)
            lookups.append(("europe_pmc", normalized_pmcid, lambda pmcid=normalized_pmcid: self.europe_pmc.by_pmcid(pmcid)))
        if arxiv_id:
            lookups.append(("semantic_scholar", arxiv_id, lambda arxiv_id=arxiv_id: self.semantic_scholar.by_arxiv_id(arxiv_id)))

        candidates: list[MetadataCandidate] = []
        for provider, identifier, lookup in lookups:
            candidate = self.safe_lookup(provider=provider, identifier=identifier, lookup=lookup)
            if candidate is None:
                continue
            candidates.append(candidate)
        return candidates

    def safe_lookup(self, *, provider: str, identifier: str, lookup: Any) -> MetadataCandidate | None:
        try:
            candidate = lookup()
        except ValueError as exc:
            self.record_provider_event(provider=provider, status="disabled", identifier=identifier, error=str(exc))
            return None
        except urllib.error.HTTPError as exc:
            retry_after = register_retry_after_from_http_error(exc)
            if exc.code == 429:
                event = {"provider": provider, "status": "rate_limited", "identifier": identifier, "http_status": exc.code, "retryable": True}
                if retry_after is not None:
                    event["retry_after_seconds"] = retry_after
                self.record_provider_event(**event)
                return None
            if exc.code in {408, 425, 500, 502, 503, 504}:
                event = {"provider": provider, "status": "provider_unavailable", "identifier": identifier, "http_status": exc.code, "retryable": True}
                if retry_after is not None:
                    event["retry_after_seconds"] = retry_after
                self.record_provider_event(**event)
                return None
            if exc.code not in {400, 404, 410, 501}:
                raise
            self.record_provider_event(provider=provider, status="no_match", identifier=identifier, http_status=exc.code, retryable=False)
            return None
        except Exception as exc:
            self.record_provider_event(provider=provider, status="provider_unavailable", identifier=identifier, error=str(exc))
            return None
        self.record_provider_event(
            provider=provider,
            status="matched" if candidate else "no_match",
            identifier=identifier,
            score=candidate.score if candidate else None,
        )
        return candidate

    def lookup_via_translation_server(
        self,
        *,
        metadata: LocalItemMetadata,
        attachment: LocalAttachment,
        doi: str | None,
        arxiv_id: str | None,
        pmid: str | None,
        pmcid: str | None,
    ) -> MetadataCandidate | None:
        if self.translation_server is None:
            self.record_provider_event(provider="zotero_translation_server", status="disabled")
            return None
        title = title_for_lookup(metadata, attachment)
        identifiers = translation_server_identifiers(
            metadata,
            attachment=attachment,
            doi=doi,
            arxiv_id=arxiv_id,
            pmid=pmid,
            pmcid=pmcid,
        )
        for identifier in identifiers:
            try:
                candidates = self.translation_server.search(identifier, expected_title=title)
            except Exception as exc:
                self.record_provider_event(
                    provider="zotero_translation_server_search",
                    status="provider_unavailable",
                    identifier=identifier,
                    error=str(exc),
                )
                return None
            best = best_candidate(candidates)
            self.record_provider_event(
                provider="zotero_translation_server_search",
                status="matched" if best else "no_match",
                identifier=identifier,
                score=best.score if best else None,
            )
            if best is not None:
                return best

        url = metadata_http_url(metadata)
        if url:
            try:
                candidates = self.translation_server.web(url, expected_title=title)
            except Exception as exc:
                self.record_provider_event(
                    provider="zotero_translation_server_web",
                    status="provider_unavailable",
                    identifier=url,
                    error=str(exc),
                )
                return None
            best = best_candidate(candidates)
            self.record_provider_event(
                provider="zotero_translation_server_web",
                status="matched" if best else "no_match",
                identifier=url,
                score=best.score if best else None,
            )
            if best is not None:
                return best

        if not identifiers and not url:
            self.record_provider_event(provider="zotero_translation_server", status="no_identifier")
        return None

    def record_provider_event(self, **event: Any) -> None:
        event.setdefault("created_at", datetime.now(UTC).isoformat())
        self.provider_events.append(event)


def best_candidate(candidates: list[MetadataCandidate]) -> MetadataCandidate | None:
    best: MetadataCandidate | None = None
    for candidate in candidates:
        if best is None or candidate.score > best.score:
            best = candidate
    return best


def candidate_has_patch(candidate: MetadataCandidate, *, metadata: LocalItemMetadata, policy: str) -> bool:
    return bool(build_metadata_diff(candidate, current_fields=metadata.fields, policy=policy).get("patch"))


def merge_metadata_candidates(candidates: list[MetadataCandidate]) -> MetadataCandidate | None:
    useful = [candidate for candidate in candidates if candidate is not None]
    if not useful:
        return None

    ordered = sorted(
        useful,
        key=lambda candidate: (candidate.score, provider_priority(candidate.source)),
        reverse=True,
    )
    fields: dict[str, str] = {}
    extra_values: list[str] = []
    locations: list[dict[str, Any]] = []
    seen_locations: set[str] = set()
    field_provenance: dict[str, str] = {}
    for candidate in ordered:
        for field, value in candidate.fields.items():
            text = normalize_space(str(value or ""))
            if not text:
                continue
            if field == "extra":
                extra_values.append(text)
                continue
            if field not in fields:
                fields[field] = text
                field_provenance[field] = candidate.source
        raw_locations = candidate.raw.get("full_text_locations") if isinstance(candidate.raw, dict) else None
        if isinstance(raw_locations, list):
            for location in raw_locations:
                if not isinstance(location, dict):
                    continue
                url = normalize_space(str(location.get("url") or ""))
                if not url or url in seen_locations:
                    continue
                seen_locations.add(url)
                locations.append(location)

    if extra_values:
        merged_extra = ""
        for value in extra_values:
            merged_extra = merge_extra(merged_extra, value)
        if merged_extra:
            fields["extra"] = merged_extra
            field_provenance.setdefault("extra", "merged")

    identifier = (
        fields.get("DOI")
        or fields.get("PMID")
        or fields.get("PMCID")
        or fields.get("archiveLocation")
        or fields.get("ISBN")
        or fields.get("title")
        or ordered[0].identifier
    )
    return MetadataCandidate(
        source="merged",
        identifier=identifier,
        score=max(candidate.score for candidate in ordered),
        fields=fields,
        raw={
            "merged_from": [candidate.to_dict() for candidate in ordered],
            "field_provenance": field_provenance,
            "full_text_locations": locations,
        },
    )


def provider_priority(source: str) -> int:
    priorities = {
        "zotero_translation_server_search": 100,
        "pubmed": 95,
        "europe_pmc": 90,
        "arxiv": 88,
        "crossref": 85,
        "openalex": 80,
        "semantic_scholar": 75,
        "unpaywall": 70,
        "datacite": 65,
        "doaj": 60,
        "core": 55,
        "openaire": 50,
    }
    return priorities.get(source, 0)


def metadata_haystack(metadata: LocalItemMetadata, attachment: LocalAttachment | None = None) -> str:
    parts: list[str] = []
    parts.extend(str(value) for value in metadata.fields.values() if value)
    parts.extend(str(tag) for tag in metadata.tags)
    for relation in metadata.relations:
        parts.extend(str(value) for value in relation.values() if value)
    if attachment is not None:
        parts.extend([attachment.filename, str(attachment.zotero_path or ""), str(attachment.file_path)])
    return "\n".join(parts)


def title_for_lookup(metadata: LocalItemMetadata, attachment: LocalAttachment) -> str:
    if metadata.title:
        return normalize_space(metadata.title)
    return normalize_space(attachment.file_path.stem)


def translation_server_identifiers(
    metadata: LocalItemMetadata,
    *,
    attachment: LocalAttachment | None = None,
    doi: str | None,
    arxiv_id: str | None,
    pmid: str | None = None,
    pmcid: str | None = None,
) -> list[str]:
    identifiers: list[str] = []
    haystack = metadata_haystack(metadata, attachment)
    extracted_values = (
        doi,
        arxiv_id,
        pmid,
        pmcid,
        metadata.fields.get("ISBN"),
        metadata.fields.get("PMID"),
        metadata.fields.get("PMCID"),
        extract_pmid_from_text(haystack),
        extract_pmcid_from_text(haystack),
        extract_isbn_from_text(haystack),
    )
    for value in extracted_values:
        if value:
            identifiers.extend(split_identifier_values(value))
    for text in (metadata.fields.get("extra", ""), metadata.fields.get("url", "")):
        extracted_doi = extract_doi_from_text(text)
        extracted_arxiv = extract_arxiv_id_from_text(text)
        extracted_pmid = extract_pmid_from_text(text)
        extracted_pmcid = extract_pmcid_from_text(text)
        extracted_isbn = extract_isbn_from_text(text)
        for value in (extracted_doi, extracted_arxiv, extracted_pmid, extracted_pmcid, extracted_isbn):
            if value:
                identifiers.append(value)
    result: list[str] = []
    seen: set[str] = set()
    for identifier in identifiers:
        normalized = normalize_space(identifier)
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def split_identifier_values(value: str) -> list[str]:
    parts = str(value or "").replace("\n", ";").split(";")
    return [normalize_space(part) for part in parts if normalize_space(part)]


def metadata_http_url(metadata: LocalItemMetadata) -> str | None:
    for field in ("url", "DOI"):
        raw = str(metadata.fields.get(field) or "").strip()
        if not raw:
            continue
        if field == "DOI":
            doi = normalize_doi(raw)
            raw = f"https://doi.org/{doi}" if doi else ""
        if raw.lower().startswith(("http://", "https://")):
            return raw
    return None
