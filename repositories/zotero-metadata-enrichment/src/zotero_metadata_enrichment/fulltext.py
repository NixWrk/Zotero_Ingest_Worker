from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .discovery import SourceDiscovery, SourceDiscoveryResult
from .enrichment import EnricherConfig
from .html_sources import HtmlSourceFetchResult, download_html_sources
from .models import FullTextLocation, LocalAttachment, LocalItemMetadata
from .pdf_sources import PdfSourceFetchResult, download_pdf_sources


@dataclass(frozen=True)
class FullTextDownloadResult:
    attachment: LocalAttachment
    metadata: LocalItemMetadata
    discovery: SourceDiscoveryResult
    html_downloads: list[HtmlSourceFetchResult]
    pdf_downloads: list[PdfSourceFetchResult]
    status: str

    @property
    def needs_ocr(self) -> bool:
        return any(
            bool(result.identity and result.identity.get("needs_ocr"))
            for result in self.pdf_downloads
            if result.ok
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["attachment"] = self.attachment.to_dict()
        payload["metadata"] = self.metadata.to_dict()
        payload["discovery"] = self.discovery.to_dict()
        payload["html_downloads"] = [result.to_dict() for result in self.html_downloads]
        payload["pdf_downloads"] = [result.to_dict() for result in self.pdf_downloads]
        payload["needs_ocr"] = self.needs_ocr
        return payload


def discover_and_download_full_text(
    *,
    metadata: LocalItemMetadata,
    attachment: LocalAttachment,
    output_dir: Path,
    config: EnricherConfig,
    max_html_downloads: int = 5,
    max_pdf_downloads: int = 3,
    max_assets: int = 240,
    save_assets: bool = True,
    require_pdf_text_identity: bool = True,
    stop_after_first_html: bool = True,
) -> FullTextDownloadResult:
    discovery = SourceDiscovery(config).discover(metadata=metadata, attachment=attachment)
    html_downloads = download_html_sources(
        discovery.locations,
        output_dir=output_dir / "html",
        limit=max_html_downloads,
        timeout_seconds=config.request_timeout_seconds,
        user_agent=config.user_agent,
        expected_title=metadata.title,
        save_assets=save_assets,
        max_assets=max_assets,
        stop_after_first_ok=stop_after_first_html,
    )
    pdf_downloads: list[PdfSourceFetchResult] = []
    if max_pdf_downloads > 0:
        pdf_downloads = download_pdf_sources(
            full_text_pdf_locations(discovery.locations, html_downloads),
            output_dir=output_dir / "pdf",
            limit=max_pdf_downloads,
            timeout_seconds=config.request_timeout_seconds,
            user_agent=config.user_agent,
            expected_title=metadata.title,
            require_text_identity=require_pdf_text_identity,
        )

    html_ok = any(result.ok for result in html_downloads)
    pdf_ok = any(result.ok for result in pdf_downloads)
    pdf_needs_ocr = any(
        result.ok and result.identity and result.identity.get("needs_ocr")
        for result in pdf_downloads
    )
    if html_ok and pdf_ok:
        status = "html_and_pdf_found_needs_ocr" if pdf_needs_ocr else "html_and_pdf_found"
    elif html_ok:
        status = "html_found"
    elif pdf_ok:
        status = "pdf_found"
    else:
        status = "unresolved"
    if status == "pdf_found" and pdf_needs_ocr:
        status = "pdf_found_needs_ocr"
    return FullTextDownloadResult(
        attachment=attachment,
        metadata=metadata,
        discovery=discovery,
        html_downloads=html_downloads,
        pdf_downloads=pdf_downloads,
        status=status,
    )


def full_text_pdf_locations(
    discovery_locations: list[FullTextLocation],
    html_downloads: list[HtmlSourceFetchResult],
) -> list[FullTextLocation]:
    locations = list(discovery_locations)
    for download in html_downloads:
        locations.extend(getattr(download, "derived_pdf_locations", []) or [])
    return dedupe_full_text_locations(locations)


def dedupe_full_text_locations(locations: list[FullTextLocation]) -> list[FullTextLocation]:
    result: list[FullTextLocation] = []
    seen: set[tuple[str, str]] = set()
    for location in locations:
        key = (location.kind.casefold(), location.url.strip())
        if not key[1] or key in seen:
            continue
        seen.add(key)
        result.append(location)
    return result
