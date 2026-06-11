from __future__ import annotations

from dataclasses import replace

from zotero_ingest_worker.config import from_env
from zotero_ingest_worker.metadata_jobs import (
    METADATA_JOB_ENRICH,
    METADATA_JOB_FULL_TEXT,
    METADATA_JOB_RESEARCHGATE_PDF,
    METADATA_JOB_SCIHUB_PDF,
    metadata_enricher_config_kwargs,
    metadata_queue_key,
)


def test_metadata_enricher_config_kwargs_falls_back_to_crossref_email() -> None:
    config = replace(
        from_env(load_file=False),
        metadata_crossref_email="owner@example.com",
        metadata_unpaywall_email="",
        metadata_openalex_api_key="openalex-key",
    )

    kwargs = metadata_enricher_config_kwargs(config)

    assert kwargs["crossref_mailto"] == "owner@example.com"
    assert kwargs["unpaywall_email"] == "owner@example.com"
    assert kwargs["openalex_api_key"] == "openalex-key"


def test_metadata_queue_key_tracks_provider_settings() -> None:
    base = from_env(load_file=False)
    with_key = replace(base, metadata_openalex_api_key="openalex-key")
    without_extended = replace(base, metadata_extended_providers_enabled=False)

    assert metadata_queue_key(base, METADATA_JOB_ENRICH) != metadata_queue_key(
        with_key,
        METADATA_JOB_ENRICH,
    )
    assert "full-text-discovery-3" in metadata_queue_key(base, METADATA_JOB_FULL_TEXT)
    assert metadata_queue_key(base, METADATA_JOB_FULL_TEXT) != metadata_queue_key(
        without_extended,
        METADATA_JOB_FULL_TEXT,
    )
    assert "researchgate-pdf-browser" in metadata_queue_key(base, METADATA_JOB_RESEARCHGATE_PDF)
    assert "scihub-pdf-2" in metadata_queue_key(base, METADATA_JOB_SCIHUB_PDF)
    assert "queries=doi-pmid-pmcid-arxiv-url" in metadata_queue_key(base, METADATA_JOB_SCIHUB_PDF)
