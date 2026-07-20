from __future__ import annotations

import hashlib
import re
import urllib.error
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .package_paths import ensure_local_package_paths

ensure_local_package_paths()

from zotero_metadata_enrichment.html_sources import (
    assess_article_html as package_assess_article_html,
    write_html_snapshot as package_write_html_snapshot,
)
from zotero_metadata_enrichment.pdf_sources import (
    download_pdf_sources as package_download_pdf_sources,
)

from .article_standard import standardize_native_html_download
from .full_text_article import (
    annotate_html_download_article_verdicts,
    arxiv_abs_ids_from_html_downloads,
)
from .full_text_attachment import _best_successful_html_download
from .filename_safety import safe_filename_component
from .full_text_inventory import inventory_has_pdf, pdf_download_limit
from .local_zotero import LocalAttachment, LocalItemMetadata


DiscoverFullText = Callable[..., Any]
FetchArxivHtml = Callable[[str], str]


@dataclass(frozen=True)
class FullTextPayloadSummary:
    worker_status: str
    output_path: str | None
    accepted_html: tuple[dict[str, Any], ...]
    rejected_html: tuple[dict[str, Any], ...]
    successful_pdf: tuple[dict[str, Any], ...]
    ocr_candidates: tuple[str, ...]
    browser_fallbacks: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class FullTextDiscoveryOrchestrator:
    config: Any
    metadata_config: Any
    discover_full_text: DiscoverFullText
    fetch_arxiv_html: FetchArxivHtml

    def discover_payload(
        self,
        *,
        attachment: LocalAttachment,
        metadata: LocalItemMetadata,
        inventory: dict[str, object],
        output_dir: Path,
        source_context: str,
    ) -> dict[str, Any]:
        result = self.discover_full_text(
            metadata=metadata,
            attachment=attachment,
            output_dir=output_dir,
            config=self.metadata_config,
            max_html_downloads=12,
            max_pdf_downloads=pdf_download_limit(inventory),
            max_assets=80,
            save_assets=True,
            require_pdf_text_identity=True,
            stop_after_first_html=False,
        )
        raw_payload = result.to_dict()
        if not isinstance(raw_payload, dict):
            raise TypeError(
                "Full-text discovery result must serialize to a dictionary."
            )
        payload: dict[str, Any] = raw_payload
        self._append_arxiv_html_candidates_from_abs_landings(
            payload=payload,
            output_dir=output_dir,
            metadata=metadata,
        )
        annotate_html_download_article_verdicts(payload.get("html_downloads"))
        self._fallback_to_pdf_after_legacy_rejected_html(
            payload=payload,
            output_dir=output_dir,
            metadata=metadata,
            inventory=inventory,
            discovery=getattr(result, "discovery", None),
        )
        payload["source_context"] = source_context
        payload["existing_full_text_inventory"] = inventory
        payload["browser_fallbacks"] = researchgate_browser_fallbacks(
            payload, inventory=inventory
        )
        standardize_accepted_html_downloads(
            payload,
            metadata=metadata,
            output_dir=output_dir,
            source_context=source_context,
        )
        summary = summarize_full_text_payload(payload)
        payload["ocr_candidates"] = list(summary.ocr_candidates)
        payload["ocr_required"] = bool(payload["ocr_candidates"])
        payload["worker_status"] = summary.worker_status
        return payload

    def _append_arxiv_html_candidates_from_abs_landings(
        self,
        *,
        payload: dict[str, Any],
        output_dir: Path,
        metadata: LocalItemMetadata,
    ) -> None:
        downloads = payload.setdefault("html_downloads", [])
        if not isinstance(downloads, list):
            return
        if _best_successful_html_download(downloads) is not None:
            return

        existing_urls: set[str] = set()
        for item in downloads:
            if not isinstance(item, dict):
                continue
            for key in ("url", "final_url"):
                existing_url = _nonempty_string(item.get(key))
                if existing_url is not None:
                    existing_urls.add(existing_url)
        for arxiv_id in arxiv_abs_ids_from_html_downloads(downloads):
            url = f"https://arxiv.org/html/{urllib.parse.quote(arxiv_id, safe='/')}"
            if url in existing_urls:
                continue
            try:
                html_text = self.fetch_arxiv_html(arxiv_id)
                body = html_text.encode("utf-8")
                article = package_assess_article_html(
                    body,
                    expected_title=metadata.title,
                    profile="arxiv",
                ).to_dict()
                if article.get("ok") is not True:
                    downloads.append(
                        {
                            "source": "arxiv",
                            "url": url,
                            "kind": "html",
                            "ok": False,
                            "status": str(
                                article.get("reason") or "article_validator_rejected"
                            ),
                            "final_url": url,
                            "content_type": "text/html; charset=utf-8",
                            "size": len(body),
                            "article": article,
                            "generated_by": "worker_arxiv_html_rescue",
                        }
                    )
                    continue
                html_dir = output_dir / "html"
                html_dir.mkdir(parents=True, exist_ok=True)
                output_path = _arxiv_rescue_html_path(
                    html_dir, arxiv_id, index=len(downloads) + 1
                )
                assets = package_write_html_snapshot(
                    body,
                    base_url=url,
                    output_path=output_path,
                    timeout_seconds=self.config.arxiv_html_fetch_timeout_seconds,
                    user_agent=self.config.metadata_user_agent,
                    save_assets=True,
                    max_assets=80,
                )
                downloads.append(
                    {
                        "source": "arxiv",
                        "url": url,
                        "kind": "html",
                        "ok": True,
                        "status": "downloaded",
                        "final_url": url,
                        "content_type": "text/html; charset=utf-8",
                        "size": len(body),
                        "output_path": str(output_path),
                        "article": article,
                        "assets": assets,
                        "generated_by": "worker_arxiv_html_rescue",
                    }
                )
            except urllib.error.HTTPError as exc:
                downloads.append(
                    {
                        "source": "arxiv",
                        "url": url,
                        "kind": "html",
                        "ok": False,
                        "status": "http_error",
                        "error": f"HTTP {exc.code}",
                        "generated_by": "worker_arxiv_html_rescue",
                    }
                )
            except Exception as exc:
                downloads.append(
                    {
                        "source": "arxiv",
                        "url": url,
                        "kind": "html",
                        "ok": False,
                        "status": "fetch_error",
                        "error": str(exc),
                        "generated_by": "worker_arxiv_html_rescue",
                    }
                )

    def _fallback_to_pdf_after_legacy_rejected_html(
        self,
        *,
        payload: dict[str, Any],
        output_dir: Path,
        metadata: LocalItemMetadata,
        inventory: dict[str, object],
        discovery: Any,
    ) -> None:
        if isinstance(payload.get("pdf_downloads"), list):
            return
        if _best_successful_html_download(payload.get("html_downloads")) is not None:
            return
        if inventory_has_pdf(inventory):
            return
        if discovery is None or not hasattr(discovery, "locations"):
            return

        pdf_downloads = package_download_pdf_sources(
            discovery.locations,
            output_dir=output_dir / "pdf",
            limit=3,
            timeout_seconds=self.metadata_config.request_timeout_seconds,
            user_agent=self.config.metadata_user_agent,
            expected_title=metadata.title,
            require_text_identity=True,
        )
        payload["pdf_downloads"] = [item.to_dict() for item in pdf_downloads]


def summarize_full_text_payload(payload: dict[str, Any]) -> FullTextPayloadSummary:
    accepted_html = tuple(_accepted_html_downloads(payload))
    rejected_html = tuple(_rejected_html_downloads(payload))
    successful_pdf = tuple(_successful_pdf_downloads(payload))
    ocr_candidates = tuple(_ocr_candidates_from_pdf_downloads(successful_pdf))
    browser_fallbacks = tuple(_browser_fallback_entries(payload))
    worker_status = _full_text_worker_status_from_summary(
        payload=payload,
        accepted_html=accepted_html,
        rejected_html=rejected_html,
        successful_pdf=successful_pdf,
        browser_fallbacks=browser_fallbacks,
    )
    return FullTextPayloadSummary(
        worker_status=worker_status,
        output_path=_first_full_text_output_path_from_summary(
            payload=payload,
            accepted_html=accepted_html,
            successful_pdf=successful_pdf,
        ),
        accepted_html=accepted_html,
        rejected_html=rejected_html,
        successful_pdf=successful_pdf,
        ocr_candidates=ocr_candidates,
        browser_fallbacks=browser_fallbacks,
    )


def standardize_accepted_html_downloads(
    payload: dict[str, Any],
    *,
    metadata: LocalItemMetadata,
    output_dir: Path,
    source_context: str,
) -> list[dict[str, Any]]:
    downloads = payload.get("html_downloads")
    if not isinstance(downloads, list):
        return []
    annotate_html_download_article_verdicts(downloads)
    standardized: list[dict[str, Any]] = []
    package_root = output_dir / "article_packages"
    for item in downloads:
        if not isinstance(item, dict):
            continue
        verdict = item.get("article_verdict")
        if not isinstance(verdict, dict) or verdict.get("ok") is not True:
            continue
        package = standardize_native_html_download(
            item,
            metadata=metadata,
            package_root=package_root,
            source_context=source_context,
        )
        item["standard_package"] = package
        if package.get("ok") is True:
            item["standard_article_html_path"] = package.get("article_html_path")
        standardized.append(package)
    payload["article_standard"] = {
        "standardized": len(standardized),
        "packages": standardized,
    }
    return standardized


def full_text_ocr_candidates(payload: dict[str, Any]) -> list[str]:
    return list(summarize_full_text_payload(payload).ocr_candidates)


def full_text_worker_status(payload: dict[str, Any]) -> str:
    return summarize_full_text_payload(payload).worker_status


def first_full_text_output_path(payload: dict[str, Any]) -> str | None:
    return summarize_full_text_payload(payload).output_path


def researchgate_browser_fallbacks(
    payload: dict[str, Any],
    *,
    inventory: dict[str, object] | None = None,
) -> list[dict[str, Any]]:
    inventory = (
        inventory
        if isinstance(inventory, dict)
        else payload.get("existing_full_text_inventory")
    )
    if isinstance(inventory, dict) and inventory_has_pdf(inventory):
        return []
    if _successful_pdf_downloads(payload):
        return []

    fallbacks: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(*, source: object, url: object, kind: object) -> None:
        normalized_url = _nonempty_string(url)
        if (
            normalized_url is None
            or normalized_url in seen
            or not _is_researchgate_url(normalized_url)
        ):
            return
        seen.add(normalized_url)
        fallbacks.append(
            {
                "source": _nonempty_string(source) or "researchgate",
                "url": normalized_url,
                "kind": _nonempty_string(kind) or "landing",
                "status": "browser_pdf_fallback_available",
                "reason": "researchgate_requires_browser",
            }
        )

    discovery = payload.get("discovery")
    locations = discovery.get("locations") if isinstance(discovery, dict) else []
    if isinstance(locations, list):
        for item in locations:
            if not isinstance(item, dict):
                continue
            add(
                source=item.get("source"),
                url=item.get("url"),
                kind=item.get("kind"),
            )

    for download_key in ("html_downloads", "pdf_downloads"):
        downloads = payload.get(download_key)
        if not isinstance(downloads, list):
            continue
        for item in downloads:
            if not isinstance(item, dict):
                continue
            source = item.get("source")
            kind = item.get("kind")
            add(source=source, url=item.get("url"), kind=kind)
            add(source=source, url=item.get("final_url"), kind=kind)

    return fallbacks


def _first_full_text_output_path_from_summary(
    *,
    payload: dict[str, Any],
    accepted_html: tuple[dict[str, Any], ...],
    successful_pdf: tuple[dict[str, Any], ...],
) -> str | None:
    relay_attachment = payload.get("relay_attachment")
    if isinstance(relay_attachment, dict):
        relay_source = relay_attachment.get("source")
        if isinstance(relay_source, dict):
            output_path = _nonempty_string(relay_source.get("output_path"))
            if output_path is not None:
                return output_path
    html = _best_successful_html_download(list(accepted_html))
    if html is not None:
        output_path = _nonempty_string(html.get("output_path"))
        if output_path is not None:
            return output_path
    for item in successful_pdf:
        output_path = _nonempty_string(item.get("output_path"))
        if output_path is not None:
            return output_path
    existing_pdf_enqueue = payload.get("existing_pdf_enqueue")
    if isinstance(existing_pdf_enqueue, dict):
        attachment = existing_pdf_enqueue.get("attachment")
        if isinstance(attachment, dict):
            file_path = _nonempty_string(attachment.get("file_path"))
            if file_path is not None:
                return file_path
    return None


def _full_text_worker_status_from_summary(
    *,
    payload: dict[str, Any],
    accepted_html: tuple[dict[str, Any], ...],
    rejected_html: tuple[dict[str, Any], ...],
    successful_pdf: tuple[dict[str, Any], ...],
    browser_fallbacks: tuple[dict[str, Any], ...],
) -> str:
    has_html = bool(accepted_html)
    if has_html and successful_pdf:
        if any(_pdf_download_needs_ocr(item) for item in successful_pdf):
            return "html_and_pdf_found_needs_ocr"
        return "html_and_pdf_found"
    if has_html:
        return "html_found"
    if successful_pdf:
        if any(_pdf_download_needs_ocr(item) for item in successful_pdf):
            return "pdf_found_needs_ocr"
        return "pdf_found"
    existing_pdf_status = _existing_pdf_enqueue_status(payload)
    if existing_pdf_status:
        return existing_pdf_status
    if browser_fallbacks:
        return "browser_pdf_fallback_available"
    if rejected_html:
        return "html_rejected"
    status = _nonempty_string(payload.get("status"))
    return status or "unresolved"


def _existing_pdf_enqueue_status(payload: dict[str, Any]) -> str | None:
    existing_pdf_enqueue = payload.get("existing_pdf_enqueue")
    if not isinstance(existing_pdf_enqueue, dict):
        return None
    if existing_pdf_enqueue.get("ok") is False:
        return (
            _nonempty_string(existing_pdf_enqueue.get("status"))
            or "existing_pdf_missing"
        )
    if existing_pdf_enqueue.get("ok") is not True:
        return None
    ocr_enqueue = existing_pdf_enqueue.get("ocr_enqueue")
    if isinstance(ocr_enqueue, dict):
        ocr_job = ocr_enqueue.get("job")
        if isinstance(ocr_job, dict) and _nonempty_string(ocr_job.get("job_id")):
            return "existing_pdf_ocr_queued"
        classification = _safe_status_suffix(ocr_enqueue.get("classification"))
        return (
            f"existing_pdf_ocr_{classification}"
            if classification
            else "existing_pdf_ocr_not_queued"
        )
    html_enqueue = existing_pdf_enqueue.get("html_enqueue")
    if isinstance(html_enqueue, dict):
        html_job = html_enqueue.get("job")
        if isinstance(html_job, dict) and _nonempty_string(html_job.get("job_id")):
            return "existing_pdf_html_queued"
        classification = _safe_status_suffix(html_enqueue.get("classification"))
        return (
            f"existing_pdf_html_{classification}"
            if classification
            else "existing_pdf_html_not_queued"
        )
    return None


def _accepted_html_downloads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    downloads = payload.get("html_downloads")
    if not isinstance(downloads, list):
        return []
    annotate_html_download_article_verdicts(downloads)
    return [
        item
        for item in downloads
        if isinstance(item, dict)
        and item.get("ok") is True
        and _nonempty_string(item.get("output_path")) is not None
        and isinstance(item.get("article_verdict"), dict)
        and item["article_verdict"].get("ok") is True
    ]


def _rejected_html_downloads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    downloads = payload.get("html_downloads")
    if not isinstance(downloads, list):
        return []
    annotate_html_download_article_verdicts(downloads)
    return [
        item
        for item in downloads
        if isinstance(item, dict)
        and item.get("ok") is True
        and isinstance(item.get("article_verdict"), dict)
        and item["article_verdict"].get("ok") is False
    ]


def _successful_pdf_downloads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    downloads = payload.get("pdf_downloads")
    if not isinstance(downloads, list):
        return []
    return [
        item
        for item in downloads
        if isinstance(item, dict)
        and item.get("ok") is True
        and _nonempty_string(item.get("output_path")) is not None
    ]


def _ocr_candidates_from_pdf_downloads(
    downloads: tuple[dict[str, Any], ...],
) -> list[str]:
    result: list[str] = []
    for item in downloads:
        if not _pdf_download_needs_ocr(item):
            continue
        output_path = _nonempty_string(item.get("output_path"))
        if output_path is not None:
            result.append(output_path)
    return result


def _browser_fallback_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    value = payload.get("browser_fallbacks")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    inventory = payload.get("existing_full_text_inventory")
    inventory_data = inventory if isinstance(inventory, dict) else None
    return researchgate_browser_fallbacks(payload, inventory=inventory_data)


def _is_researchgate_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme.casefold() not in {"http", "https"}:
            return False
        host = (parsed.hostname or "").rstrip(".").casefold()
    except (TypeError, ValueError):
        return False
    return host == "researchgate.net" or host.endswith(".researchgate.net")


def _pdf_download_needs_ocr(item: dict[str, Any]) -> bool:
    if item.get("status") == "downloaded_needs_ocr":
        return True
    identity = item.get("identity")
    return isinstance(identity, dict) and identity.get("needs_ocr") is True


def _safe_status_suffix(value: Any) -> str:
    text = _nonempty_string(value)
    if text is None:
        return ""
    text = text.lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def _nonempty_string(value: object) -> str | None:
    text = value.strip() if isinstance(value, str) else ""
    return text or None


def _safe_filename(value: str) -> str:
    return safe_filename_component(value, default="document", max_chars=160)


def _arxiv_rescue_html_path(html_dir: Path, arxiv_id: str, *, index: int) -> Path:
    digest = hashlib.sha1(arxiv_id.encode("utf-8")).hexdigest()[:10]
    safe_id = _safe_filename(arxiv_id).replace("/", "_")
    return html_dir / f"{index:02d}.arxiv.arxiv.org.{safe_id}.{digest}.html"
