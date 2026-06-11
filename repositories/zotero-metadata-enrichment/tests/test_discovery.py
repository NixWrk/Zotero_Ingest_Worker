from __future__ import annotations

from pathlib import Path

from zotero_metadata_enrichment.discovery import SourceDiscovery
from zotero_metadata_enrichment.enrichment import EnricherConfig
from zotero_metadata_enrichment.models import LocalAttachment, LocalItemMetadata, MetadataCandidate


class NoopDoiProvider:
    def by_doi(self, doi: str) -> None:
        return None

    def by_pmid(self, pmid: str) -> None:
        return None

    def by_pmcid(self, pmcid: str) -> None:
        return None

    def by_arxiv_id(self, arxiv_id: str) -> None:
        return None

    def by_id(self, arxiv_id: str) -> None:
        return None

    def by_title(self, title: str) -> None:
        return None


class FakeOpenCitations:
    def citation_count(self, doi: str) -> int:
        assert doi == "10.1000/example"
        return 7


class NoopOpenCitations:
    def citation_count(self, doi: str) -> None:
        return None


class ExplodingProvider(NoopDoiProvider):
    def by_doi(self, doi: str) -> None:
        raise AssertionError(f"extended provider unexpectedly called for DOI {doi}")

    def by_pmid(self, pmid: str) -> None:
        raise AssertionError(f"extended provider unexpectedly called for PMID {pmid}")

    def by_pmcid(self, pmcid: str) -> None:
        raise AssertionError(f"extended provider unexpectedly called for PMCID {pmcid}")

    def by_arxiv_id(self, arxiv_id: str) -> None:
        raise AssertionError(f"extended provider unexpectedly called for arXiv {arxiv_id}")

    def by_title(self, title: str) -> None:
        raise AssertionError(f"extended provider unexpectedly called for title {title}")


class ExplodingOpenCitations:
    def citation_count(self, doi: str) -> int:
        raise AssertionError(f"OpenCitations unexpectedly called for DOI {doi}")


def test_source_discovery_adds_opencitations_auxiliary_metadata() -> None:
    metadata = LocalItemMetadata(
        library_id="test",
        data_dir=Path("."),
        key="ITEM1",
        item_id=1,
        version=None,
        item_type="journalArticle",
        date_modified=None,
        fields={"title": "Example", "DOI": "10.1000/example"},
    )
    attachment = LocalAttachment(
        library_id="test",
        data_dir=Path("."),
        storage_dir=Path("."),
        key="ATTACH1",
        item_id=2,
        parent_item_id=1,
        parent_key="ITEM1",
        file_path=Path("Example.pdf"),
    )
    noop = NoopDoiProvider()

    result = SourceDiscovery(
        crossref=noop,
        unpaywall=noop,
        openalex=noop,
        europe_pmc=noop,
        semantic_scholar=noop,
        arxiv=noop,
        datacite=noop,
        biorxiv=noop,
        core=noop,
        openaire=noop,
        doaj=noop,
        opencitations=FakeOpenCitations(),
    ).discover(metadata=metadata, attachment=attachment)

    assert result.auxiliary_metadata["opencitations"] == {"citation_count": 7}
    assert result.provider_events[0]["provider"] == "opencitations"
    assert result.provider_events[0]["status"] == "matched"


def test_source_discovery_respects_disabled_extended_providers() -> None:
    metadata = LocalItemMetadata(
        library_id="test",
        data_dir=Path("."),
        key="ITEM1",
        item_id=1,
        version=None,
        item_type="journalArticle",
        date_modified=None,
        fields={
            "title": "Example",
            "DOI": "10.1000/example",
            "PMID": "12345",
            "PMCID": "PMC12345",
        },
    )
    attachment = LocalAttachment(
        library_id="test",
        data_dir=Path("."),
        storage_dir=Path("."),
        key="ATTACH1",
        item_id=2,
        parent_item_id=1,
        parent_key="ITEM1",
        file_path=Path("Example.pdf"),
    )
    noop = NoopDoiProvider()
    exploding = ExplodingProvider()

    result = SourceDiscovery(
        EnricherConfig(translation_server_url="", extended_providers_enabled=False),
        crossref=noop,
        unpaywall=exploding,
        openalex=exploding,
        europe_pmc=exploding,
        semantic_scholar=exploding,
        arxiv=noop,
        datacite=exploding,
        biorxiv=exploding,
        core=exploding,
        openaire=exploding,
        doaj=exploding,
        opencitations=ExplodingOpenCitations(),  # type: ignore[arg-type]
    ).discover(metadata=metadata, attachment=attachment)

    providers = [event["provider"] for event in result.provider_events]

    assert result.auxiliary_metadata == {}
    assert "extended_providers" in providers
    assert "opencitations" not in providers
    assert "crossref" in providers


def test_source_discovery_uses_zotero_translator_full_text_locations() -> None:
    class FakeTranslationServer:
        def search(self, identifier: str, *, expected_title: str = "") -> list[MetadataCandidate]:
            assert identifier == "10.1000/example"
            return [
                MetadataCandidate(
                    source="zotero_translation_server_search",
                    identifier=identifier,
                    score=0.98,
                    fields={"title": expected_title, "DOI": identifier},
                    raw={
                        "full_text_locations": [
                            {
                                "source": "zotero_translation_server_attachment",
                                "url": "https://journal.example/article/full",
                                "kind": "html",
                            }
                        ]
                    },
                )
            ]

        def web(self, url: str, *, expected_title: str = "") -> list[MetadataCandidate]:
            return []

    metadata = LocalItemMetadata(
        library_id="test",
        data_dir=Path("."),
        key="ITEM1",
        item_id=1,
        version=None,
        item_type="journalArticle",
        date_modified=None,
        fields={"title": "Example", "DOI": "10.1000/example"},
    )
    attachment = LocalAttachment(
        library_id="test",
        data_dir=Path("."),
        storage_dir=Path("."),
        key="ATTACH1",
        item_id=2,
        parent_item_id=1,
        parent_key="ITEM1",
        file_path=Path("Example.pdf"),
    )
    noop = NoopDoiProvider()

    result = SourceDiscovery(
        translation_server=FakeTranslationServer(),  # type: ignore[arg-type]
        crossref=noop,
        unpaywall=noop,
        openalex=noop,
        europe_pmc=noop,
        semantic_scholar=noop,
        arxiv=noop,
        datacite=noop,
        biorxiv=noop,
        core=noop,
        openaire=noop,
        doaj=noop,
        opencitations=noop,  # type: ignore[arg-type]
    ).discover(metadata=metadata, attachment=attachment)

    assert result.locations[0].url == "https://journal.example/article/full"
    assert result.provider_events[0]["provider"] == "zotero_translation_server_search"
    assert result.provider_events[0]["status"] == "matched"


def test_source_discovery_expands_pmid_to_doi_before_doi_providers() -> None:
    class FakePubMed:
        def by_pmid(self, pmid: str) -> MetadataCandidate:
            assert pmid == "31044789"
            return MetadataCandidate(
                source="pubmed",
                identifier=pmid,
                score=1.0,
                fields={
                    "title": "Example",
                    "PMID": pmid,
                    "PMCID": "PMC123456",
                    "DOI": "10.1234/example",
                },
            )

        def by_pmcid(self, pmcid: str) -> None:
            raise AssertionError(f"PMCID lookup should not be needed for {pmcid}")

    class FakeUnpaywall:
        def __init__(self) -> None:
            self.dois: list[str] = []

        def by_doi(self, doi: str) -> MetadataCandidate:
            self.dois.append(doi)
            return MetadataCandidate(
                source="unpaywall",
                identifier=doi,
                score=1.0,
                fields={"DOI": doi, "title": "Example"},
                raw={
                    "full_text_locations": [
                        {
                            "source": "unpaywall",
                            "url": "https://repo.example/example.pdf",
                            "kind": "pdf",
                        }
                    ]
                },
            )

    metadata = LocalItemMetadata(
        library_id="test",
        data_dir=Path("."),
        key="ITEM1",
        item_id=1,
        version=None,
        item_type="journalArticle",
        date_modified=None,
        fields={"title": "Example", "PMID": "31044789"},
    )
    attachment = LocalAttachment(
        library_id="test",
        data_dir=Path("."),
        storage_dir=Path("."),
        key="ATTACH1",
        item_id=2,
        parent_item_id=1,
        parent_key="ITEM1",
        file_path=Path("Example.pdf"),
    )
    noop = NoopDoiProvider()
    unpaywall = FakeUnpaywall()

    result = SourceDiscovery(
        EnricherConfig(translation_server_url=""),
        crossref=noop,
        unpaywall=unpaywall,  # type: ignore[arg-type]
        openalex=noop,
        europe_pmc=noop,
        pubmed=FakePubMed(),  # type: ignore[arg-type]
        semantic_scholar=noop,
        arxiv=noop,
        datacite=noop,
        biorxiv=noop,
        core=noop,
        openaire=noop,
        doaj=noop,
        opencitations=NoopOpenCitations(),  # type: ignore[arg-type]
    ).discover(metadata=metadata, attachment=attachment)

    assert unpaywall.dois == ["10.1234/example"]
    assert result.locations[0].url == "https://repo.example/example.pdf"
    assert any(event["provider"] == "pubmed" and event["status"] == "matched" for event in result.provider_events)
