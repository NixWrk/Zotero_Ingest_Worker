from __future__ import annotations

import hashlib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .html_sources import is_probable_pdf_url
from .models import FullTextLocation
from .provider_http import throttled_urlopen
from .safe_http import UnsafeUrlError
from .text import title_match_score
from .url_safety import validate_fetch_url


@dataclass(frozen=True)
class PdfSourceFetchResult:
    source: str
    url: str
    kind: str
    ok: bool
    status: str
    final_url: str = ""
    content_type: str = ""
    size: int = 0
    output_path: str = ""
    error: str = ""
    identity: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def download_pdf_sources(
    locations: list[FullTextLocation],
    *,
    output_dir: Path,
    limit: int = 3,
    timeout_seconds: int = 30,
    user_agent: str = "zotero-metadata-enrichment/0.1",
    max_bytes: int = 120_000_000,
    expected_title: str = "",
    require_text_identity: bool = False,
) -> list[PdfSourceFetchResult]:
    if limit <= 0:
        return []

    results: list[PdfSourceFetchResult] = []
    attempts = 0
    for location in locations:
        if not should_probe_for_pdf(location):
            continue
        if attempts >= limit:
            results.append(
                PdfSourceFetchResult(
                    source=location.source,
                    url=location.url,
                    kind=location.kind,
                    ok=False,
                    status="skipped_limit",
                )
            )
            continue
        attempts += 1
        results.append(
            fetch_pdf_source(
                location,
                output_dir=output_dir,
                timeout_seconds=timeout_seconds,
                user_agent=user_agent,
                max_bytes=max_bytes,
                expected_title=expected_title,
                require_text_identity=require_text_identity,
                index=attempts,
            )
        )
    return results


def fetch_pdf_source(
    location: FullTextLocation,
    *,
    output_dir: Path,
    timeout_seconds: int = 30,
    user_agent: str = "zotero-metadata-enrichment/0.1",
    max_bytes: int = 120_000_000,
    expected_title: str = "",
    require_text_identity: bool = False,
    index: int = 1,
) -> PdfSourceFetchResult:
    safety = validate_fetch_url(location.url)
    if not safety.ok:
        return PdfSourceFetchResult(
            source=location.source,
            url=location.url,
            kind=location.kind,
            ok=False,
            status="unsafe_url",
            error=safety.reason,
        )
    request = urllib.request.Request(
        location.url,
        headers={"Accept": "application/pdf,*/*;q=0.1", "User-Agent": user_agent},
        method="GET",
    )
    try:
        with throttled_urlopen(request, timeout=timeout_seconds) as response:
            final_url = getattr(response, "url", location.url)
            content_type = str(response.headers.get("Content-Type") or "")
            body = response.read(max_bytes + 1)
    except UnsafeUrlError as exc:
        return PdfSourceFetchResult(
            source=location.source,
            url=location.url,
            kind=location.kind,
            ok=False,
            status="unsafe_redirect" if exc.is_redirect else "unsafe_url",
            error=str(exc),
        )
    except urllib.error.HTTPError as exc:
        return PdfSourceFetchResult(
            source=location.source,
            url=location.url,
            kind=location.kind,
            ok=False,
            status="http_error",
            error=f"HTTP {exc.code}",
        )
    except Exception as exc:
        return PdfSourceFetchResult(
            source=location.source,
            url=location.url,
            kind=location.kind,
            ok=False,
            status="fetch_error",
            error=str(exc),
        )

    if len(body) > max_bytes:
        return PdfSourceFetchResult(
            source=location.source,
            url=location.url,
            kind=location.kind,
            ok=False,
            status="too_large",
            final_url=final_url,
            content_type=content_type,
            size=len(body),
        )
    if not is_pdf_response(final_url=final_url, content_type=content_type, body=body):
        return PdfSourceFetchResult(
            source=location.source,
            url=location.url,
            kind=location.kind,
            ok=False,
            status="non_pdf",
            final_url=final_url,
            content_type=content_type,
            size=len(body),
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / pdf_source_filename(location, index=index)
    output_path.write_bytes(body)
    identity = assess_pdf_bytes_identity(body, expected_title=expected_title)
    if require_text_identity and not identity.get("ok") and not identity.get("needs_ocr"):
        output_path.unlink(missing_ok=True)
        return PdfSourceFetchResult(
            source=location.source,
            url=location.url,
            kind=location.kind,
            ok=False,
            status="identity_mismatch",
            final_url=final_url,
            content_type=content_type,
            size=len(body),
            identity=identity,
        )
    return PdfSourceFetchResult(
        source=location.source,
        url=location.url,
        kind=location.kind,
        ok=True,
        status="downloaded_needs_ocr" if identity.get("needs_ocr") else "downloaded",
        final_url=final_url,
        content_type=content_type,
        size=len(body),
        output_path=str(output_path),
        identity=identity,
    )


def should_probe_for_pdf(location: FullTextLocation) -> bool:
    content_type = location.content_type.casefold()
    return bool(location.url) and (
        location.kind.casefold() == "pdf"
        or "pdf" in content_type
        or is_probable_pdf_url(location.url)
    )


def is_pdf_response(*, final_url: str, content_type: str, body: bytes) -> bool:
    mime = content_type.split(";", 1)[0].strip().casefold()
    return mime in {"application/pdf", "application/x-pdf"} or body.startswith(b"%PDF")


def pdf_source_filename(location: FullTextLocation, *, index: int) -> str:
    source = safe_filename_part(location.source or "source")
    parsed = urllib.parse.urlparse(location.url)
    host = safe_filename_part(parsed.netloc or "url")
    digest = hashlib.sha1(location.url.encode("utf-8")).hexdigest()[:10]
    return f"{index:02d}.{source}.{host}.{digest}.pdf"


def safe_filename_part(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "._-" else "_" for char in value.strip())
    return cleaned.strip("._-")[:60] or "value"


def assess_pdf_bytes_identity(
    body: bytes,
    *,
    expected_title: str = "",
    max_pages: int = 5,
    min_text_chars: int = 80,
    title_min_score: float = 0.70,
) -> dict[str, Any]:
    if not expected_title:
        return {"ok": True, "reason": "no_expected_title"}
    text_result = extract_pdf_text_sample(body, max_pages=max_pages)
    if not text_result["ok"]:
        return {
            "ok": False,
            "reason": "pdf_text_unavailable",
            "needs_ocr": True,
            **text_result,
        }
    text = str(text_result.get("text") or "")
    if len(text.strip()) < min_text_chars:
        return {
            "ok": False,
            "reason": "no_text_layer",
            "needs_ocr": True,
            "text_chars": len(text.strip()),
        }
    score = title_match_score(expected_title, text[:4000])
    if score < title_min_score and not title_tokens_present(expected_title, text):
        return {
            "ok": False,
            "reason": "title_mismatch",
            "needs_ocr": False,
            "title_score": score,
            "text_chars": len(text),
        }
    return {
        "ok": True,
        "reason": "title_match",
        "needs_ocr": False,
        "title_score": score,
        "text_chars": len(text),
    }


def extract_pdf_text_sample(body: bytes, *, max_pages: int) -> dict[str, Any]:
    try:
        from io import BytesIO

        from pypdf import PdfReader
    except Exception as exc:
        return {"ok": False, "error": f"pypdf_unavailable:{exc}"}
    try:
        reader = PdfReader(BytesIO(body))
        parts: list[str] = []
        for page in reader.pages[: max(max_pages, 1)]:
            parts.append(page.extract_text() or "")
        text = "\n".join(parts)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "text": text, "text_chars": len(text)}


def title_tokens_present(title: str, text: str) -> bool:
    tokens = [
        token
        for token in title.lower().split()
        if len(token) >= 5 and token.isascii()
    ][:8]
    if not tokens:
        return False
    lowered = text.lower()
    hits = sum(1 for token in tokens if token in lowered)
    return hits >= min(max(2, len(tokens) // 2), 5)
