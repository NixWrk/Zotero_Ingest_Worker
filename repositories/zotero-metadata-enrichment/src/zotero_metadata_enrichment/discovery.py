from __future__ import annotations

import urllib.error
from dataclasses import dataclass, field
from typing import Any

from .enrichment import (
    EnricherConfig,
    metadata_haystack,
    metadata_http_url,
    title_for_lookup,
    translation_server_identifiers,
)
from .identifiers import (
    extract_arxiv_id_from_text,
    extract_doi_from_text,
    extract_pmcid_from_text,
    extract_pmid_from_text,
    normalize_doi,
    normalize_pmcid,
    normalize_pmid,
)
from .models import FullTextLocation, LocalAttachment, LocalItemMetadata, MetadataCandidate
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
    OpenCitationsClient,
    PubMedClient,
    SemanticScholarClient,
    TranslationServerClient,
    UnpaywallClient,
)
from .providers.common import CandidateLookup, bind_candidate_lookup


@dataclass(frozen=True)
class SourceDiscoveryResult:
    candidates: list[MetadataCandidate] = field(default_factory=list)
    locations: list[FullTextLocation] = field(default_factory=list)
    auxiliary_metadata: dict[str, Any] = field(default_factory=dict)
    provider_events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "locations": [location.to_dict() for location in self.locations],
            "auxiliary_metadata": self.auxiliary_metadata,
            "provider_events": self.provider_events,
        }


class SourceDiscovery:
    def __init__(
        self,
        config: EnricherConfig | None = None,
        *,
        unpaywall: UnpaywallClient | None = None,
        crossref: CrossrefClient | None = None,
        openalex: OpenAlexClient | None = None,
        europe_pmc: EuropePmcClient | None = None,
        pubmed: PubMedClient | None = None,
        semantic_scholar: SemanticScholarClient | None = None,
        arxiv: ArxivClient | None = None,
        datacite: DataCiteClient | None = None,
        biorxiv: BioRxivClient | None = None,
        core: CoreClient | None = None,
        openaire: OpenAireClient | None = None,
        doaj: DoajClient | None = None,
        opencitations: OpenCitationsClient | None = None,
        translation_server: TranslationServerClient | None = None,
    ) -> None:
        self.config = config or EnricherConfig()
        timeout = self.config.request_timeout_seconds
        user_agent = self.config.user_agent
        self.translation_server = translation_server
        if self.translation_server is None and self.config.translation_server_url:
            self.translation_server = TranslationServerClient(
                self.config.translation_server_url,
                timeout_seconds=self.config.translation_server_timeout_seconds,
                user_agent=user_agent,
            )
        self.crossref = crossref or CrossrefClient(
            mailto=self.config.crossref_mailto,
            timeout_seconds=timeout,
            user_agent=user_agent,
        )
        self.unpaywall = unpaywall or UnpaywallClient(email=self.config.unpaywall_email or self.config.crossref_mailto, timeout_seconds=timeout, user_agent=user_agent)
        self.openalex = openalex or OpenAlexClient(
            mailto=self.config.crossref_mailto,
            api_key=self.config.openalex_api_key,
            timeout_seconds=timeout,
            user_agent=user_agent,
        )
        self.europe_pmc = europe_pmc or EuropePmcClient(timeout_seconds=timeout, user_agent=user_agent)
        self.pubmed = pubmed or PubMedClient(timeout_seconds=timeout, user_agent=user_agent)
        self.semantic_scholar = semantic_scholar or SemanticScholarClient(api_key=self.config.semantic_scholar_api_key, timeout_seconds=timeout, user_agent=user_agent)
        self.arxiv = arxiv or ArxivClient(timeout_seconds=timeout, user_agent=user_agent)
        self.datacite = datacite or DataCiteClient(timeout_seconds=timeout, user_agent=user_agent)
        self.biorxiv = biorxiv or BioRxivClient(timeout_seconds=timeout, user_agent=user_agent)
        self.core = core or CoreClient(api_key=self.config.core_api_key, timeout_seconds=timeout, user_agent=user_agent)
        self.openaire = openaire or OpenAireClient(timeout_seconds=timeout, user_agent=user_agent)
        self.doaj = doaj or DoajClient(timeout_seconds=timeout, user_agent=user_agent)
        self.opencitations = opencitations or OpenCitationsClient(timeout_seconds=timeout, user_agent=user_agent)

    def discover(self, *, metadata: LocalItemMetadata, attachment: LocalAttachment) -> SourceDiscoveryResult:
        haystack = metadata_haystack(metadata, attachment)
        doi = normalize_doi(metadata.fields.get("DOI") or extract_doi_from_text(haystack) or "")
        pmid = normalize_pmid(metadata.fields.get("PMID") or extract_pmid_from_text(haystack) or "")
        pmcid = normalize_pmcid(metadata.fields.get("PMCID") or extract_pmcid_from_text(haystack) or "")
        arxiv_id = extract_arxiv_id_from_text(haystack) or ""
        title = title_for_lookup(metadata, attachment)

        events: list[dict[str, Any]] = []
        candidates: list[MetadataCandidate] = []
        auxiliary_metadata: dict[str, Any] = {}
        calls: list[tuple[str, str, CandidateLookup, float]] = []
        extended_enabled = self.config.extended_providers_enabled
        candidates.extend(
            self.translation_server_candidates(
                metadata=metadata,
                attachment=attachment,
                doi=doi,
                arxiv_id=arxiv_id,
                pmid=pmid,
                pmcid=pmcid,
                title=title,
                events=events,
            )
        )
        doi, pmid, pmcid = expand_identifiers_from_candidates(
            doi=doi,
            pmid=pmid,
            pmcid=pmcid,
            candidates=candidates,
        )
        if not extended_enabled:
            events.append({"provider": "extended_providers", "identifier": "", "status": "disabled"})
        else:
            pubmed_candidates = self.pubmed_identifier_candidates(
                pmid=pmid,
                pmcid=pmcid,
                events=events,
            )
            candidates.extend(pubmed_candidates)
            doi, pmid, pmcid = expand_identifiers_from_candidates(
                doi=doi,
                pmid=pmid,
                pmcid=pmcid,
                candidates=pubmed_candidates,
            )
        if doi:
            if extended_enabled:
                citation_count = self.opencitations_citation_count(doi=doi, events=events)
                if citation_count is not None:
                    auxiliary_metadata["opencitations"] = {"citation_count": citation_count}
            calls.append(("crossref", doi, bind_candidate_lookup(self.crossref.by_doi, doi), 0.0))
            if extended_enabled:
                calls.extend(
                    [
                        ("unpaywall", doi, bind_candidate_lookup(self.unpaywall.by_doi, doi), 0.0),
                        ("openalex", doi, bind_candidate_lookup(self.openalex.by_doi, doi), 0.0),
                        ("europe_pmc", doi, bind_candidate_lookup(self.europe_pmc.by_doi, doi), 0.0),
                        ("semantic_scholar", doi, bind_candidate_lookup(self.semantic_scholar.by_doi, doi), 0.0),
                        ("datacite", doi, bind_candidate_lookup(self.datacite.by_doi, doi), 0.0),
                        ("biorxiv_medrxiv", doi, bind_candidate_lookup(self.biorxiv.by_doi, doi), 0.0),
                        ("core", doi, bind_candidate_lookup(self.core.by_doi, doi), 0.0),
                        ("openaire", doi, bind_candidate_lookup(self.openaire.by_doi, doi), 0.0),
                        ("doaj", doi, bind_candidate_lookup(self.doaj.by_doi, doi), 0.0),
                    ]
                )
        if extended_enabled and pmid:
            calls.extend(
                [
                    ("europe_pmc", pmid, bind_candidate_lookup(self.europe_pmc.by_pmid, pmid), 0.0),
                    ("openalex", pmid, bind_candidate_lookup(self.openalex.by_pmid, pmid), 0.0),
                    ("semantic_scholar", pmid, bind_candidate_lookup(self.semantic_scholar.by_pmid, pmid), 0.0),
                ]
            )
        if extended_enabled and pmcid:
            calls.append(("europe_pmc", pmcid, bind_candidate_lookup(self.europe_pmc.by_pmcid, pmcid), 0.0))
        if arxiv_id:
            calls.append(("arxiv", arxiv_id, bind_candidate_lookup(self.arxiv.by_id, arxiv_id), 0.0))
            if extended_enabled:
                calls.append(("semantic_scholar", arxiv_id, bind_candidate_lookup(self.semantic_scholar.by_arxiv_id, arxiv_id), 0.0))
        if title:
            calls.append(("crossref", title, bind_candidate_lookup(self.crossref.by_title, title), self.config.metadata_title_min_score))
            if extended_enabled:
                calls.extend(
                    [
                        ("openalex", title, bind_candidate_lookup(self.openalex.by_title, title), self.config.metadata_title_min_score),
                        ("semantic_scholar", title, bind_candidate_lookup(self.semantic_scholar.by_title, title), self.config.metadata_title_min_score),
                        ("core", title, bind_candidate_lookup(self.core.by_title, title), self.config.metadata_title_min_score),
                        ("doaj", title, bind_candidate_lookup(self.doaj.by_title, title), self.config.metadata_title_min_score),
                    ]
                )
            calls.append(("arxiv", title, bind_candidate_lookup(self.arxiv.by_title, title), self.config.arxiv_search_min_score))

        for provider, identifier, call, min_score in calls:
            try:
                candidate = call()
            except urllib.error.HTTPError as exc:
                events.append(provider_http_event(provider=provider, identifier=identifier, exc=exc))
                continue
            except Exception as exc:
                events.append({"provider": provider, "identifier": identifier, "status": "provider_unavailable", "error": str(exc)})
                continue
            if candidate is not None and min_score > 0 and candidate.score < min_score:
                events.append({"provider": provider, "identifier": identifier, "status": "low_confidence", "score": candidate.score, "min_score": min_score})
                continue
            events.append({"provider": provider, "identifier": identifier, "status": "matched" if candidate else "no_match", "score": candidate.score if candidate else None})
            if candidate is not None:
                candidates.append(candidate)

        return SourceDiscoveryResult(
            candidates=candidates,
            locations=dedupe_locations(candidates),
            auxiliary_metadata=auxiliary_metadata,
            provider_events=events,
        )

    def translation_server_candidates(
        self,
        *,
        metadata: LocalItemMetadata,
        attachment: LocalAttachment,
        doi: str,
        arxiv_id: str,
        pmid: str,
        pmcid: str,
        title: str,
        events: list[dict[str, Any]],
    ) -> list[MetadataCandidate]:
        if self.translation_server is None:
            return []
        identifiers = translation_server_identifiers(
            metadata,
            attachment=attachment,
            doi=doi,
            arxiv_id=arxiv_id,
            pmid=pmid,
            pmcid=pmcid,
        )
        results: list[MetadataCandidate] = []
        for identifier in identifiers:
            try:
                candidates = self.translation_server.search(identifier, expected_title=title)
            except urllib.error.HTTPError as exc:
                events.append(provider_http_event(provider="zotero_translation_server_search", identifier=identifier, exc=exc))
                continue
            except Exception as exc:
                events.append({"provider": "zotero_translation_server_search", "identifier": identifier, "status": "provider_unavailable", "error": str(exc)})
                continue
            accepted = self.accept_translation_candidates(
                candidates,
                title=title,
                events=events,
                provider="zotero_translation_server_search",
                identifier=identifier,
            )
            results.extend(accepted)

        url = metadata_http_url(metadata)
        if url:
            try:
                candidates = self.translation_server.web(url, expected_title=title)
            except urllib.error.HTTPError as exc:
                events.append(provider_http_event(provider="zotero_translation_server_web", identifier=url, exc=exc))
            except Exception as exc:
                events.append({"provider": "zotero_translation_server_web", "identifier": url, "status": "provider_unavailable", "error": str(exc)})
            else:
                results.extend(
                    self.accept_translation_candidates(
                        candidates,
                        title=title,
                        events=events,
                        provider="zotero_translation_server_web",
                        identifier=url,
                    )
                )
        return results

    def accept_translation_candidates(
        self,
        candidates: list[MetadataCandidate],
        *,
        title: str,
        events: list[dict[str, Any]],
        provider: str,
        identifier: str,
    ) -> list[MetadataCandidate]:
        if not candidates:
            events.append({"provider": provider, "identifier": identifier, "status": "no_match", "score": None})
            return []
        accepted: list[MetadataCandidate] = []
        min_score = self.config.metadata_title_min_score if title else 0.0
        for candidate in candidates:
            if min_score and candidate.score < min_score:
                events.append({"provider": provider, "identifier": identifier, "status": "low_confidence", "score": candidate.score, "min_score": min_score})
                continue
            events.append({"provider": provider, "identifier": identifier, "status": "matched", "score": candidate.score})
            accepted.append(candidate)
        return accepted

    def opencitations_citation_count(self, *, doi: str, events: list[dict[str, Any]]) -> int | None:
        try:
            citation_count = self.opencitations.citation_count(doi)
        except urllib.error.HTTPError as exc:
            events.append(provider_http_event(provider="opencitations", identifier=doi, exc=exc))
            return None
        except Exception as exc:
            events.append({"provider": "opencitations", "identifier": doi, "status": "provider_unavailable", "error": str(exc)})
            return None
        events.append(
            {
                "provider": "opencitations",
                "identifier": doi,
                "status": "matched" if citation_count is not None else "no_match",
                "citation_count": citation_count,
            }
        )
        return citation_count

    def pubmed_identifier_candidates(
        self,
        *,
        pmid: str,
        pmcid: str,
        events: list[dict[str, Any]],
    ) -> list[MetadataCandidate]:
        calls: list[tuple[str, CandidateLookup]] = []
        if pmid:
            calls.append((pmid, bind_candidate_lookup(self.pubmed.by_pmid, pmid)))
        if pmcid:
            calls.append((pmcid, bind_candidate_lookup(self.pubmed.by_pmcid, pmcid)))

        candidates: list[MetadataCandidate] = []
        for identifier, call in calls:
            try:
                candidate = call()
            except urllib.error.HTTPError as exc:
                events.append(provider_http_event(provider="pubmed", identifier=identifier, exc=exc))
                continue
            except Exception as exc:
                events.append({"provider": "pubmed", "identifier": identifier, "status": "provider_unavailable", "error": str(exc)})
                continue
            events.append({"provider": "pubmed", "identifier": identifier, "status": "matched" if candidate else "no_match", "score": candidate.score if candidate else None})
            if candidate is not None:
                candidates.append(candidate)
        return candidates


def dedupe_locations(candidates: list[MetadataCandidate]) -> list[FullTextLocation]:
    locations: list[FullTextLocation] = []
    seen: set[str] = set()
    for candidate in candidates:
        raw_locations = candidate.raw.get("full_text_locations") if isinstance(candidate.raw, dict) else None
        if not isinstance(raw_locations, list):
            continue
        for item in raw_locations:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            raw_value = item.get("raw")
            locations.append(
                FullTextLocation(
                    source=str(item.get("source") or candidate.source),
                    url=url,
                    kind=str(item.get("kind") or "landing"),
                    is_oa=item.get("is_oa") if isinstance(item.get("is_oa"), bool) or item.get("is_oa") is None else None,
                    license=str(item.get("license") or ""),
                    version=str(item.get("version") or ""),
                    content_type=str(item.get("content_type") or ""),
                    repository=str(item.get("repository") or ""),
                    raw=raw_value if isinstance(raw_value, dict) else {},
                )
            )
    return locations


def expand_identifiers_from_candidates(
    *,
    doi: str,
    pmid: str,
    pmcid: str,
    candidates: list[MetadataCandidate],
) -> tuple[str, str, str]:
    for candidate in candidates:
        fields = candidate.fields
        if not doi:
            doi = normalize_doi(fields.get("DOI") or "")
        if not pmid:
            pmid = normalize_pmid(fields.get("PMID") or "")
        if not pmcid:
            pmcid = normalize_pmcid(fields.get("PMCID") or "")
    return doi, pmid, pmcid


def provider_http_event(*, provider: str, identifier: str, exc: urllib.error.HTTPError) -> dict[str, Any]:
    retry_after = register_retry_after_from_http_error(exc)
    if exc.code == 429:
        status = "rate_limited"
        retryable = True
    elif exc.code in {408, 425, 500, 502, 503, 504}:
        status = "provider_unavailable"
        retryable = True
    elif exc.code in {400, 404, 410, 501}:
        status = "no_match"
        retryable = False
    else:
        status = "http_error"
        retryable = False
    event = {
        "provider": provider,
        "identifier": identifier,
        "status": status,
        "http_status": exc.code,
        "retryable": retryable,
    }
    if retry_after is not None:
        event["retry_after_seconds"] = retry_after
    return event
