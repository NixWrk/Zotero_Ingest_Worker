from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, cast

from .package_paths import ensure_local_package_paths

ensure_local_package_paths()

from zotero_arxiv_html_ingest import (
    arxiv_html_filename as package_arxiv_html_filename,
    validate_arxiv_html as package_validate_arxiv_html,
)
from zotero_metadata_enrichment import (
    MetadataCandidate,
    extract_arxiv_id_from_text,
    normalize_arxiv_id,
)
from zotero_metadata_enrichment.providers.arxiv import (
    parse_arxiv_atom as package_parse_arxiv_atom,
)
from zotero_metadata_enrichment.provider_http import (
    read_response_bytes,
    throttled_urlopen,
)
from zotero_metadata_enrichment.safe_http import (
    host_suffix_redirect,
)
from zotero_metadata_enrichment.text import (
    normalize_space,
    title_match_score,
)

from .local_zotero import LocalAttachment, LocalItemMetadata
from .config import WorkerConfig
from .metadata_jobs import METADATA_JOB_ARXIV_HTML
from .state import FileSignature


HttpText = Callable[[str], str]


class ArxivHtmlValidationError(RuntimeError):
    def __init__(self, *, arxiv_id: str, reason: str):
        self.arxiv_id = arxiv_id
        self.reason = reason
        super().__init__(f"arXiv HTML validation failed for {arxiv_id}: {reason}")


@dataclass
class ArxivHtmlJobService:
    config: WorkerConfig
    http_text: HttpText | None = None
    provider_events: list[dict[str, Any]] = field(default_factory=list)

    def lookup_candidate(
        self,
        *,
        metadata: LocalItemMetadata | None,
        attachment: LocalAttachment,
    ) -> MetadataCandidate | None:
        haystack = metadata_haystack(metadata, attachment)
        arxiv_id = extract_arxiv_id_from_text(haystack)
        if arxiv_id:
            return self._arxiv_by_id(arxiv_id) or MetadataCandidate(
                source="arxiv",
                identifier=arxiv_id,
                score=1.0,
                fields={
                    "url": f"https://arxiv.org/abs/{arxiv_id}",
                    "extra": f"arXiv:{arxiv_id}",
                    "archive": "arXiv",
                    "archiveLocation": arxiv_id,
                    "libraryCatalog": "arXiv.org",
                },
                raw={"arxiv_id": arxiv_id, "match": "identifier"},
            )

        title = title_for_lookup(metadata, attachment)
        if not title:
            return None
        candidate = self._arxiv_by_title(title)
        if candidate is None or candidate.score < self.config.arxiv_search_min_score:
            return None
        return candidate

    def fetch_html(self, arxiv_id: str) -> str:
        arxiv_id = normalize_arxiv_id(arxiv_id)
        if not arxiv_id:
            raise ValueError("arXiv id is empty.")
        url = f"https://arxiv.org/html/{urllib.parse.quote(arxiv_id, safe='/')}"
        text = self._http_text(url, timeout=self.config.arxiv_html_fetch_timeout_seconds)
        validation = validate_arxiv_html(
            text,
            min_text_chars=self.config.arxiv_html_min_text_chars,
        )
        if not validation["ok"]:
            raise ArxivHtmlValidationError(
                arxiv_id=arxiv_id,
                reason=str(validation["reason"]),
            )
        return text

    def write_html_file(
        self,
        *,
        attachment: LocalAttachment,
        source_pdf: Path,
        candidate: MetadataCandidate,
        html_text: str,
    ) -> Path:
        signature = FileSignature.from_path(source_pdf)
        stem = safe_filename(Path(attachment.filename).stem or candidate.identifier or "article")
        target_dir = (
            self.config.arxiv_html_root
            / attachment.library_id
            / attachment.key
            / f"{signature.size}_{signature.mtime_ns}"
            / stem
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        output_path = target_dir / arxiv_html_filename(attachment.filename)
        output_path.write_text(html_text, encoding="utf-8")
        manifest = {
            "job_kind": METADATA_JOB_ARXIV_HTML,
            "library_id": attachment.library_id,
            "attachment_key": attachment.key,
            "source_pdf": str(source_pdf),
            "arxiv_id": candidate.identifier,
            "candidate": candidate.to_dict(),
            "html_url": f"https://arxiv.org/html/{candidate.identifier}",
            "output": str(output_path),
            "created_at": datetime.now(UTC).isoformat(),
        }
        (target_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return output_path

    def _arxiv_by_id(self, arxiv_id: str) -> MetadataCandidate | None:
        arxiv_id = normalize_arxiv_id(arxiv_id)
        if not arxiv_id:
            return None
        params = {"id_list": arxiv_id, "max_results": "1"}
        url = f"https://arxiv.org/api/query?{urllib.parse.urlencode(params)}"
        candidates = parse_arxiv_atom(self._http_text(url))
        if not candidates:
            self._record_provider_event(provider="arxiv", status="no_match", identifier=arxiv_id)
            return None
        candidate = candidates[0]
        self._record_provider_event(
            provider="arxiv",
            status="matched",
            identifier=arxiv_id,
            score=1.0,
        )
        return MetadataCandidate(
            source=candidate.source,
            identifier=normalize_arxiv_id(candidate.identifier) or candidate.identifier,
            score=1.0,
            fields=candidate.fields,
            raw={**candidate.raw, "match": "identifier"},
        )

    def _arxiv_by_title(self, title: str) -> MetadataCandidate | None:
        query = f'ti:"{title}"'
        params = {
            "search_query": query,
            "start": "0",
            "max_results": "5",
        }
        url = f"https://arxiv.org/api/query?{urllib.parse.urlencode(params)}"
        candidates = parse_arxiv_atom(self._http_text(url))
        best: MetadataCandidate | None = None
        for candidate in candidates:
            candidate_title = candidate.fields.get("title", "")
            score = title_match_score(title, candidate_title)
            scored = MetadataCandidate(
                source=candidate.source,
                identifier=candidate.identifier,
                score=score,
                fields=candidate.fields,
                raw={**candidate.raw, "match": "title"},
            )
            if best is None or scored.score > best.score:
                best = scored
        self._record_provider_event(
            provider="arxiv",
            status="matched" if best else "no_match",
            identifier=title,
            score=best.score if best else None,
        )
        return best

    def _http_text(self, url: str, *, timeout: int | None = None) -> str:
        if self.http_text is not None:
            return self.http_text(url)
        headers = {
            "User-Agent": self.config.metadata_user_agent,
            "Accept": "application/json, application/atom+xml, text/html;q=0.9, */*;q=0.8",
        }
        request = urllib.request.Request(url, headers=headers, method="GET")
        with throttled_urlopen(
            request,
            timeout=timeout or self.config.request_timeout_seconds,
            redirect_validator=host_suffix_redirect("arxiv.org"),
        ) as response:
            content_type = response.headers.get_content_charset() or "utf-8"
            body = read_response_bytes(response, error_label=url)
            if not isinstance(body, bytes):
                raise TypeError("arXiv HTTP helper did not return bytes.")
            return body.decode(content_type, errors="replace")

    def _record_provider_event(self, **event: Any) -> None:
        event.setdefault("created_at", datetime.now(UTC).isoformat())
        self.provider_events.append(event)


def parse_arxiv_atom(xml_text: str) -> list[MetadataCandidate]:
    return package_parse_arxiv_atom(xml_text)


def arxiv_html_filename(pdf_filename: str) -> str:
    return package_arxiv_html_filename(pdf_filename)


def validate_arxiv_html(html_text: str, *, min_text_chars: int = 200) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        package_validate_arxiv_html(html_text, min_text_chars=min_text_chars),
    )


def metadata_haystack(
    metadata: LocalItemMetadata | None,
    attachment: LocalAttachment | None = None,
) -> str:
    parts: list[str] = []
    if metadata is not None:
        parts.extend(str(value) for value in metadata.fields.values() if value)
        parts.extend(str(tag) for tag in metadata.tags)
        for relation in metadata.relations:
            if isinstance(relation, dict):
                parts.extend(str(value) for value in relation.values() if value)
        for collection in metadata.collections:
            if isinstance(collection, dict):
                parts.extend(str(value) for value in collection.values() if value)
        for creator in metadata.creators:
            if isinstance(creator, dict):
                parts.extend(str(value) for value in creator.values() if value)
    if attachment is not None:
        parts.extend(
            [
                attachment.filename,
                attachment.zotero_path or "",
                str(attachment.file_path),
            ]
        )
    return "\n".join(parts)


def title_for_lookup(
    metadata: LocalItemMetadata | None,
    attachment: LocalAttachment,
) -> str:
    if metadata is not None and metadata.title:
        return normalize_space(metadata.title)
    return normalize_space(Path(attachment.filename).stem)


def safe_filename(value: str) -> str:
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", str(value or "document"))
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:160] or "document"
