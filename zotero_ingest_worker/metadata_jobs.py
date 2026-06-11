from __future__ import annotations

import urllib.parse
from typing import Any


METADATA_JOB_ENRICH = "enrich"
METADATA_JOB_ARXIV_HTML = "arxiv_html"
METADATA_JOB_FULL_TEXT = "full_text"
METADATA_JOB_RESEARCHGATE_PDF = "researchgate_pdf"
METADATA_JOB_SCIHUB_PDF = "scihub_pdf"


def metadata_enricher_config_kwargs(config: Any) -> dict[str, Any]:
    return {
        "translation_server_url": config.zotero_translation_server_url,
        "translation_server_timeout_seconds": config.zotero_translation_server_timeout_seconds,
        "crossref_mailto": config.metadata_crossref_email,
        "unpaywall_email": config.metadata_unpaywall_email or config.metadata_crossref_email,
        "openalex_api_key": config.metadata_openalex_api_key,
        "semantic_scholar_api_key": config.metadata_semantic_scholar_api_key,
        "core_api_key": config.metadata_core_api_key,
        "request_timeout_seconds": config.metadata_request_timeout_seconds,
        "user_agent": config.metadata_user_agent,
        "metadata_title_min_score": config.metadata_title_min_score,
        "arxiv_search_min_score": config.arxiv_search_min_score,
        "policy": config.metadata_policy,
        "extended_providers_enabled": config.metadata_extended_providers_enabled,
    }


def metadata_queue_key(config: Any, job_type: str) -> str:
    if job_type == METADATA_JOB_ENRICH:
        providers = (
            "zotero-translators,crossref,pubmed,arxiv"
            ",unpaywall,openalex,europe-pmc,semantic-scholar"
            ",datacite,biorxiv-medrxiv,core,openaire,doaj,opencitations"
        )
        return (
            f"v=metadata-merge-2|providers={providers}|"
            f"extended={int(config.metadata_extended_providers_enabled)}|"
            f"policy={config.metadata_policy}|"
            f"title_score={config.metadata_title_min_score:.3f}|"
            f"arxiv_score={config.arxiv_search_min_score:.3f}|"
            f"openalex_key={int(bool(config.metadata_openalex_api_key))}|"
            f"semantic_key={int(bool(config.metadata_semantic_scholar_api_key))}|"
            f"core_key={int(bool(config.metadata_core_api_key))}"
        )
    if job_type == METADATA_JOB_ARXIV_HTML:
        return (
            f"provider=arxiv-html|attach={int(config.arxiv_html_attach)}|"
            f"score={config.arxiv_search_min_score:.3f}"
        )
    if job_type == METADATA_JOB_FULL_TEXT:
        return (
            "v=full-text-discovery-3|scope=parent-items|providers=metadata-discovery,zotero-translators|"
            f"extended={int(config.metadata_extended_providers_enabled)}|"
            f"title_score={config.metadata_title_min_score:.3f}|"
            f"arxiv_score={config.arxiv_search_min_score:.3f}|"
            f"translation_server={int(bool(config.zotero_translation_server_url))}|"
            f"openalex_key={int(bool(config.metadata_openalex_api_key))}|"
            f"semantic_key={int(bool(config.metadata_semantic_scholar_api_key))}|"
            f"core_key={int(bool(config.metadata_core_api_key))}"
        )
    if job_type == METADATA_JOB_RESEARCHGATE_PDF:
        return "v=researchgate-pdf-browser-1|attach=parent|skip_existing_pdf=1"
    if job_type == METADATA_JOB_SCIHUB_PDF:
        mirrors = getattr(config, "scihub_mirrors", ()) or ()
        hosts = ",".join(
            urllib.parse.urlparse(mirror).netloc.casefold()
            for mirror in mirrors
            if str(mirror).strip()
        )
        return (
            "v=scihub-pdf-2|attach=parent|skip_existing_pdf=1|"
            f"queries=doi-pmid-pmcid-arxiv-url|mirrors={hosts}"
        )
    return "default"
