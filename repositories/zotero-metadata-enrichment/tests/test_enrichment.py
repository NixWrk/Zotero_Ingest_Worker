from __future__ import annotations

import urllib.error
from pathlib import Path

from zotero_metadata_enrichment.enrichment import EnricherConfig, MetadataEnricher
from zotero_metadata_enrichment.models import LocalAttachment, LocalItemMetadata, MetadataCandidate


class FakeCrossref:
    def by_doi(self, doi: str) -> MetadataCandidate:
        return MetadataCandidate(
            source="crossref",
            identifier=doi,
            score=1.0,
            fields={"DOI": doi, "title": "Resolved title"},
        )

    def by_title(self, title: str) -> None:
        return None


class FakeArxiv:
    def by_id(self, arxiv_id: str) -> None:
        return None

    def by_title(self, title: str) -> None:
        return None


def test_enricher_returns_diff_from_provider_candidate(tmp_path: Path) -> None:
    metadata = LocalItemMetadata(
        library_id="LIB",
        data_dir=tmp_path,
        key="PARENT",
        item_id=10,
        version=1,
        item_type="journalArticle",
        date_modified=None,
        fields={"title": "Existing", "DOI": "10.1000/example"},
    )
    attachment = LocalAttachment(
        library_id="LIB",
        data_dir=tmp_path,
        storage_dir=tmp_path,
        key="PDF",
        item_id=20,
        parent_item_id=10,
        parent_key="PARENT",
        file_path=Path("paper.pdf"),
    )

    result = MetadataEnricher(
        EnricherConfig(translation_server_url="", extended_providers_enabled=False),
        crossref=FakeCrossref(),  # type: ignore[arg-type]
        arxiv=FakeArxiv(),  # type: ignore[arg-type]
    ).enrich(metadata=metadata, attachment=attachment)

    assert result.candidate is not None
    assert result.reason == "matched"
    assert result.diff is not None
    assert result.diff["patch"] == {}
    assert result.diff["skipped_fields"]["title"] == "current_field_not_empty"


def test_enricher_falls_back_to_arxiv_when_crossref_doi_404(tmp_path: Path) -> None:
    class Crossref404:
        def by_doi(self, doi: str) -> None:
            raise urllib.error.HTTPError(
                url="https://api.crossref.org/works/10.48550%2FarXiv.2401.01234",
                code=404,
                msg="Resource not found.",
                hdrs={},
                fp=None,
            )

        def by_title(self, title: str) -> None:
            return None

    class ArxivMatch:
        def by_id(self, arxiv_id: str) -> MetadataCandidate:
            return MetadataCandidate(
                source="arxiv",
                identifier=arxiv_id,
                score=1.0,
                fields={"archive": "arXiv", "archiveLocation": arxiv_id},
            )

        def by_title(self, title: str) -> None:
            return None

    metadata = LocalItemMetadata(
        library_id="LIB",
        data_dir=tmp_path,
        key="PARENT",
        item_id=10,
        version=1,
        item_type="journalArticle",
        date_modified=None,
        fields={
            "title": "A Careful Metadata Pipeline",
            "DOI": "10.48550/arXiv.2401.01234",
        },
    )
    attachment = LocalAttachment(
        library_id="LIB",
        data_dir=tmp_path,
        storage_dir=tmp_path,
        key="PDF",
        item_id=20,
        parent_item_id=10,
        parent_key="PARENT",
        file_path=Path("paper.pdf"),
    )

    enricher = MetadataEnricher(
        EnricherConfig(translation_server_url="", extended_providers_enabled=False),
        crossref=Crossref404(),  # type: ignore[arg-type]
        arxiv=ArxivMatch(),  # type: ignore[arg-type]
    )
    result = enricher.enrich(metadata=metadata, attachment=attachment)

    assert result.candidate is not None
    assert result.candidate.source == "merged"
    assert result.candidate.identifier == "2401.01234"
    assert result.candidate.raw["merged_from"][0]["source"] == "arxiv"
    assert result.provider_events[1]["provider"] == "crossref"
    assert result.provider_events[1]["status"] == "no_match"
    assert result.provider_events[1]["http_status"] == 404


def test_safe_lookup_records_retry_after_on_rate_limit() -> None:
    enricher = MetadataEnricher(EnricherConfig(translation_server_url="", extended_providers_enabled=False))

    def lookup() -> None:
        raise urllib.error.HTTPError(
            url="https://ratelimit.example/works",
            code=429,
            msg="Too Many Requests",
            hdrs={"Retry-After": "11"},
            fp=None,
        )

    assert enricher.safe_lookup(provider="crossref", identifier="10.1000/example", lookup=lookup) is None

    assert enricher.provider_events[0]["status"] == "rate_limited"
    assert enricher.provider_events[0]["retryable"] is True
    assert enricher.provider_events[0]["retry_after_seconds"] == 11.0


def test_merge_candidate_combines_metadata_and_arxiv_fields(tmp_path: Path) -> None:
    class CrossrefMatch:
        def by_doi(self, doi: str) -> MetadataCandidate:
            return MetadataCandidate(
                source="crossref",
                identifier=doi,
                score=1.0,
                fields={"DOI": doi, "publicationTitle": "Journal of Examples"},
            )

        def by_title(self, title: str) -> None:
            return None

    class ArxivMatch:
        def by_id(self, arxiv_id: str) -> MetadataCandidate:
            return MetadataCandidate(
                source="arxiv",
                identifier=arxiv_id,
                score=1.0,
                fields={"archive": "arXiv", "archiveLocation": arxiv_id, "extra": f"arXiv:{arxiv_id}"},
            )

        def by_title(self, title: str) -> None:
            return None

    metadata = LocalItemMetadata(
        library_id="LIB",
        data_dir=tmp_path,
        key="PARENT",
        item_id=10,
        version=1,
        item_type="journalArticle",
        date_modified=None,
        fields={"title": "Existing", "DOI": "10.48550/arXiv.2401.01234"},
    )
    attachment = LocalAttachment(
        library_id="LIB",
        data_dir=tmp_path,
        storage_dir=tmp_path,
        key="PDF",
        item_id=20,
        parent_item_id=10,
        parent_key="PARENT",
        file_path=Path("paper.pdf"),
    )

    result = MetadataEnricher(
        EnricherConfig(translation_server_url="", extended_providers_enabled=False),
        crossref=CrossrefMatch(),  # type: ignore[arg-type]
        arxiv=ArxivMatch(),  # type: ignore[arg-type]
    ).enrich(metadata=metadata, attachment=attachment)

    assert result.candidate is not None
    assert result.candidate.source == "merged"
    assert result.patch["publicationTitle"] == "Journal of Examples"
    assert result.patch["archiveLocation"] == "2401.01234"
    assert result.patch["archive"] == "arXiv"
