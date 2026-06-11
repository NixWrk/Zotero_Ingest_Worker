from .diff import build_metadata_diff, build_metadata_patch
from .discovery import SourceDiscovery, SourceDiscoveryResult
from .enrichment import EnricherConfig, MetadataEnricher
from .fulltext import FullTextDownloadResult, discover_and_download_full_text
from .identifiers import (
    extract_arxiv_id_from_text,
    extract_doi_from_text,
    extract_isbn_from_text,
    extract_pmcid_from_text,
    extract_pmid_from_text,
    normalize_arxiv_id,
    normalize_doi,
    normalize_isbn,
    normalize_pmcid,
    normalize_pmid,
)
from .models import FullTextLocation, LocalAttachment, LocalItemMetadata, MetadataCandidate

__all__ = [
    "FullTextLocation",
    "LocalAttachment",
    "LocalItemMetadata",
    "MetadataCandidate",
    "MetadataEnricher",
    "EnricherConfig",
    "SourceDiscovery",
    "SourceDiscoveryResult",
    "FullTextDownloadResult",
    "discover_and_download_full_text",
    "build_metadata_diff",
    "build_metadata_patch",
    "extract_arxiv_id_from_text",
    "extract_doi_from_text",
    "extract_isbn_from_text",
    "extract_pmcid_from_text",
    "extract_pmid_from_text",
    "normalize_arxiv_id",
    "normalize_doi",
    "normalize_isbn",
    "normalize_pmcid",
    "normalize_pmid",
]
