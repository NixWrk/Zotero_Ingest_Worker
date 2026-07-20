from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import stat
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .config import DEFAULT_SCIHUB_MIRRORS, PROJECT_ROOT, WorkerConfig
from .package_paths import ensure_local_package_paths
from .provider_scripts import provider_script_path

ensure_local_package_paths()

from zotero_metadata_enrichment import (
    extract_doi_from_text as package_extract_doi_from_text,
    normalize_doi as package_normalize_doi,
)
from zotero_metadata_enrichment.pdf_sources import (
    assess_pdf_bytes_identity as package_assess_pdf_bytes_identity,
)
from zotero_metadata_enrichment.safe_http import (
    UnsafeUrlError,
    safe_urlopen,
)
from zotero_metadata_enrichment.url_safety import (
    validate_fetch_url as package_validate_fetch_url,
)


# Ported from NixWrk/Zotero_SciHub_module (src/scihubShared.ts).
DEFAULT_SCIHUB_URL = "https://sci-hub.ru/"
DEFAULT_SCIHUB_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 11_3_1 like Mac OS X) "
    "AppleWebKit/603.1.30 (KHTML, like Gecko) Version/10.0 Mobile/14E304 Safari/602.1"
)
RETRYABLE_HTTP_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}
PDF_NOT_AVAILABLE_MARKERS = (
    "please try to search again using doi",
    "статья не найдена в базе",
)

DEFAULT_DOWNLOAD_DIR = PROJECT_ROOT / "data" / "ingest" / "scihub_downloads"

_PDF_TAG_RE = re.compile(
    r"""<[^>]*\bid\s*=\s*(?:"pdf"|'pdf'|pdf)[^>]*>""", re.IGNORECASE
)
_SRC_RE = re.compile(
    r"""\bsrc\s*=\s*(?:"(?P<dq>[^"]+)"|'(?P<sq>[^']+)'|(?P<bare>[^\s>]+))""",
    re.IGNORECASE,
)
# Current sci-hub markup no longer uses <embed id="pdf">; the PDF is a plain
# href/src to a *.pdf (typically under /storage/ or /downloads/).
_PDF_ATTR_RE = re.compile(
    r"""(?:href|src)\s*=\s*(?:"([^"]+\.pdf[^"]*)"|'([^']+\.pdf[^']*)')""",
    re.IGNORECASE,
)
_PDF_QUOTED_RE = re.compile(r"""["']([^"']+\.pdf(?:[?#][^"']*)?)["']""", re.IGNORECASE)


class SciHubError(RuntimeError):
    pass


class SciHubUnsafeUrlError(SciHubError):
    pass


class SciHubTransportError(SciHubError):
    pass


@dataclass(frozen=True)
class SciHubResolveResult:
    doi: str
    scihub_url: str
    pdf_url: str


@dataclass(frozen=True)
class SciHubPdfOptions:
    item_key: str
    doi: str = ""
    data_dir: str = ""
    output_dir: Path | None = None
    mirrors: tuple[str, ...] = DEFAULT_SCIHUB_MIRRORS
    user_agent: str = DEFAULT_SCIHUB_USER_AGENT
    timeout_seconds: int = 60
    max_bytes: int = 120_000_000
    force_attach: bool = False
    ensure_active: Callable[[], None] | None = None


def normalize_base_url(base_url: str) -> str:
    base_url = (base_url or DEFAULT_SCIHUB_URL).strip()
    return base_url if base_url.endswith("/") else f"{base_url}/"


def normalize_doi_value(doi: str) -> str:
    value = (doi or "").strip()
    value = re.sub(r"^\s*doi:\s*", "", value, flags=re.IGNORECASE).strip()
    return value


def url_to_https(url: str) -> str:
    safe_url = urllib.parse.urlparse(re.sub(r"^//", "https://", url.strip()))
    return urllib.parse.urlunparse(safe_url._replace(scheme="https"))


def extract_raw_pdf_url_from_html(html: str) -> str:
    # 1) Legacy / mirror markup: an element with id="pdf" carrying the src.
    tag_match = _PDF_TAG_RE.search(html)
    if tag_match:
        src_match = _SRC_RE.search(tag_match.group(0))
        if src_match:
            raw = (
                src_match.group("dq")
                or src_match.group("sq")
                or src_match.group("bare")
                or ""
            ).strip()
            if raw:
                return raw

    # 2) Current sci-hub.ru markup: a href/src pointing at a *.pdf file.
    candidates = [
        (m.group(1) or m.group(2)).strip() for m in _PDF_ATTR_RE.finditer(html)
    ]
    if not candidates:
        candidates = [
            m.group(1).strip()
            for m in _PDF_QUOTED_RE.finditer(html)
            if not m.group(1).strip().lower().startswith("data:")
        ]
    candidates = [c for c in candidates if c]
    if not candidates:
        return ""

    def _score(url: str) -> int:
        lowered = url.lower()
        score = 0
        if "/storage/" in lowered or "/downloads/" in lowered:
            score += 2
        if lowered.startswith(("//", "http", "/")):
            score += 1
        return score

    return max(candidates, key=_score)


def to_absolute_pdf_url(raw_pdf_url: str, *, base_url: str) -> str:
    raw = raw_pdf_url.strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        absolute = f"https:{raw}"
    elif urllib.parse.urlparse(raw).scheme:
        absolute = raw
    else:
        absolute = urllib.parse.urljoin(base_url, raw)
    return url_to_https(absolute)


def is_pdf_not_available_markup(html: str | None) -> bool:
    if not html:
        return True
    lowered = html.casefold()
    return any(marker in lowered for marker in PDF_NOT_AVAILABLE_MARKERS)


def resolve_pdf_url(
    doi: str,
    *,
    mirrors: tuple[str, ...] | list[str] | None = None,
    user_agent: str = DEFAULT_SCIHUB_USER_AGENT,
    timeout_seconds: int = 60,
    max_bytes: int = 15_000_000,
) -> SciHubResolveResult:
    """Build the Sci-Hub request URL automatically from the DOI and resolve the
    PDF link, trying each configured mirror until one responds with a link."""
    normalized_doi = normalize_doi_value(doi)
    if not normalized_doi:
        raise SciHubError("A non-empty DOI is required for Sci-Hub resolution.")

    mirror_list = [
        normalize_base_url(m)
        for m in (mirrors or DEFAULT_SCIHUB_MIRRORS)
        if str(m).strip()
    ]
    if not mirror_list:
        mirror_list = [normalize_base_url(DEFAULT_SCIHUB_URL)]

    errors: list[str] = []
    unsafe_failures = 0
    transport_failures = 0
    for base_url in mirror_list:
        try:
            return _resolve_on_mirror(
                normalized_doi,
                base_url=base_url,
                user_agent=user_agent,
                timeout_seconds=timeout_seconds,
                max_bytes=max_bytes,
            )
        except (SciHubUnsafeUrlError, UnsafeUrlError) as exc:
            unsafe_failures += 1
            host = urllib.parse.urlparse(base_url).netloc or base_url
            errors.append(f"{host}: {exc}")
            continue
        except SciHubError as exc:
            host = urllib.parse.urlparse(base_url).netloc or base_url
            errors.append(f"{host}: {exc}")
            continue
        except urllib.error.HTTPError as exc:
            if exc.code in RETRYABLE_HTTP_STATUSES:
                transport_failures += 1
            host = urllib.parse.urlparse(base_url).netloc or base_url
            errors.append(f"{host}: HTTP {exc.code}")
            continue
        except Exception as exc:  # noqa: BLE001 - try the next mirror on any failure
            transport_failures += 1
            host = urllib.parse.urlparse(base_url).netloc or base_url
            errors.append(f"{host}: {exc}")
            continue
    if unsafe_failures == len(mirror_list):
        raise SciHubUnsafeUrlError(
            f"All Sci-Hub mirrors were rejected as unsafe ({'; '.join(errors)})."
        )
    if transport_failures:
        raise SciHubTransportError(
            f"Sci-Hub mirror transport failed for DOI {normalized_doi} ({'; '.join(errors)})."
        )
    raise SciHubError(
        f"All Sci-Hub mirrors failed for DOI {normalized_doi} ({'; '.join(errors)})."
    )


def _resolve_on_mirror(
    normalized_doi: str,
    *,
    base_url: str,
    user_agent: str,
    timeout_seconds: int,
    max_bytes: int,
) -> SciHubResolveResult:
    query_path = urllib.parse.quote(normalized_doi.lstrip("/"), safe="/")
    if not query_path:
        raise SciHubError("A non-empty Sci-Hub query is required.")
    scihub_url = urllib.parse.urljoin(base_url, query_path)
    byte_limit = max(1, int(max_bytes))

    safety = package_validate_fetch_url(scihub_url)
    if not safety.ok:
        raise SciHubUnsafeUrlError(f"Unsafe Sci-Hub URL: {safety.reason}")

    request = urllib.request.Request(
        scihub_url,
        headers={
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
            "User-Agent": user_agent,
        },
        method="GET",
    )
    with safe_urlopen(request, timeout=timeout_seconds) as response:
        final_url = getattr(response, "url", scihub_url)
        body = response.read(byte_limit + 1)
    if len(body) > byte_limit:
        raise SciHubError(f"Sci-Hub mirror response exceeds {byte_limit} bytes.")
    html = body.decode("utf-8", errors="replace")

    if is_pdf_not_available_markup(html):
        raise SciHubError(
            f"document not available on {urllib.parse.urlparse(base_url).netloc}"
        )

    raw_pdf_url = extract_raw_pdf_url_from_html(html)
    if not raw_pdf_url:
        raise SciHubError(f"no PDF link on {urllib.parse.urlparse(base_url).netloc}")

    pdf_url = to_absolute_pdf_url(raw_pdf_url, base_url=final_url or scihub_url)
    if not pdf_url:
        raise SciHubError("could not normalize the PDF URL")

    return SciHubResolveResult(
        doi=normalized_doi, scihub_url=scihub_url, pdf_url=pdf_url
    )


def download_scihub_pdf(
    doi: str,
    *,
    output_dir: Path,
    mirrors: tuple[str, ...] | list[str] | None = None,
    user_agent: str = DEFAULT_SCIHUB_USER_AGENT,
    timeout_seconds: int = 60,
    max_bytes: int = 120_000_000,
    expected_title: str = "",
) -> dict[str, Any]:
    try:
        resolved = resolve_pdf_url(
            doi,
            mirrors=mirrors,
            user_agent=user_agent,
            timeout_seconds=timeout_seconds,
        )
    except SciHubUnsafeUrlError as exc:
        return {"ok": False, "status": "unsafe_url", "error": str(exc), "doi": doi}
    except SciHubTransportError as exc:
        return {"ok": False, "status": "transport_error", "error": str(exc), "doi": doi}
    except SciHubError as exc:
        return {"ok": False, "status": "unresolved", "error": str(exc), "doi": doi}
    except Exception as exc:  # noqa: BLE001 - surface transport errors as a status
        return {"ok": False, "status": "resolve_error", "error": str(exc), "doi": doi}

    safety = package_validate_fetch_url(resolved.pdf_url)
    if not safety.ok:
        return {
            "ok": False,
            "status": "unsafe_url",
            "error": safety.reason,
            "scihub_url": resolved.scihub_url,
            "pdf_url": resolved.pdf_url,
            "doi": resolved.doi,
        }

    request = urllib.request.Request(
        resolved.pdf_url,
        headers={"Accept": "application/pdf,*/*;q=0.1", "User-Agent": user_agent},
        method="GET",
    )
    byte_limit = max(1, int(max_bytes))
    try:
        with safe_urlopen(request, timeout=timeout_seconds) as response:
            final_url = getattr(response, "url", resolved.pdf_url)
            content_type = str(response.headers.get("Content-Type") or "")
            body = response.read(byte_limit + 1)
    except UnsafeUrlError as exc:
        return {
            "ok": False,
            "status": "unsafe_url",
            "error": str(exc),
            "scihub_url": resolved.scihub_url,
            "pdf_url": resolved.pdf_url,
            "doi": resolved.doi,
        }
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "status": "http_error",
            "http_status": exc.code,
            "error": f"HTTP {exc.code}",
            "scihub_url": resolved.scihub_url,
            "pdf_url": resolved.pdf_url,
            "doi": resolved.doi,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "status": "fetch_error",
            "error": str(exc),
            "scihub_url": resolved.scihub_url,
            "pdf_url": resolved.pdf_url,
            "doi": resolved.doi,
        }

    if len(body) > byte_limit:
        return {
            "ok": False,
            "status": "too_large",
            "size": len(body),
            "max_bytes": byte_limit,
            "scihub_url": resolved.scihub_url,
            "pdf_url": resolved.pdf_url,
            "doi": resolved.doi,
        }
    if b"%PDF-" not in body[:1024]:
        return {
            "ok": False,
            "status": "non_pdf",
            "content_type": content_type,
            "size": len(body),
            "scihub_url": resolved.scihub_url,
            "pdf_url": resolved.pdf_url,
            "doi": resolved.doi,
        }

    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_path = output_dir / (
        f"scihub_{stamp}_{uuid.uuid4().hex}_{_safe_doi(resolved.doi)}.pdf"
    )
    source_sha256 = hashlib.sha256(body).hexdigest()
    owner_identity = _write_owned_download(output_path, body)
    try:
        identity = package_assess_pdf_bytes_identity(
            body,
            expected_title=expected_title,
        )
        if not isinstance(identity, dict):
            removed, cleanup_error = _remove_owned_download(
                output_path,
                owner_identity=owner_identity,
            )
            return {
                "ok": False,
                "status": "identity_invalid_result",
                "content_type": content_type,
                "size": len(body),
                "final_url": final_url,
                "scihub_url": resolved.scihub_url,
                "pdf_url": resolved.pdf_url,
                "doi": resolved.doi,
                "identity": identity,
                "removed": removed,
                **({"cleanup_error": cleanup_error} if cleanup_error else {}),
            }
        needs_ocr = identity.get("needs_ocr") is True
        if expected_title and identity.get("ok") is not True and not needs_ocr:
            removed, cleanup_error = _remove_owned_download(
                output_path,
                owner_identity=owner_identity,
            )
            return {
                "ok": False,
                "status": "identity_mismatch",
                "content_type": content_type,
                "size": len(body),
                "final_url": final_url,
                "scihub_url": resolved.scihub_url,
                "pdf_url": resolved.pdf_url,
                "doi": resolved.doi,
                "identity": identity,
                "removed": removed,
                **({"cleanup_error": cleanup_error} if cleanup_error else {}),
            }
        if not _owned_download_matches(
            output_path,
            owner_identity=owner_identity,
            expected_size=len(body),
            expected_sha256=source_sha256,
        ):
            removed, cleanup_error = _remove_owned_download(
                output_path,
                owner_identity=owner_identity,
            )
            return {
                "ok": False,
                "status": "download_artifact_changed",
                "output_path": str(output_path),
                "removed": removed,
                **({"cleanup_error": cleanup_error} if cleanup_error else {}),
            }
        return {
            "ok": True,
            "status": "downloaded_needs_ocr" if needs_ocr else "downloaded",
            "source": "scihub",
            "kind": "pdf",
            "url": resolved.pdf_url,
            "final_url": final_url,
            "content_type": content_type,
            "size": len(body),
            "output_path": str(output_path),
            "scihub_url": resolved.scihub_url,
            "pdf_url": resolved.pdf_url,
            "doi": resolved.doi,
            "identity": identity,
            "source_sha256": source_sha256,
        }
    except BaseException as exc:
        removed, cleanup_error = _remove_owned_download(
            output_path,
            owner_identity=owner_identity,
        )
        if not removed:
            exc.add_note(
                "Sci-Hub owned download cleanup failed for "
                f"{output_path}: {cleanup_error or 'artifact remains'}"
            )
        raise


def download_and_attach_scihub_pdf(
    config: WorkerConfig,
    options: SciHubPdfOptions,
) -> dict[str, Any]:
    module = _script_module()
    try:
        metadata, store = module.find_item(
            config,
            item_key=options.item_key,
            data_dir=options.data_dir,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "item_not_found", "error": str(exc)}

    inventory = store.item_full_text_inventory(metadata)
    if inventory.get("has_pdf") is True and not options.force_attach:
        return {
            "ok": True,
            "status": "parent_already_has_pdf",
            "download": {
                "ok": True,
                "skipped": True,
                "reason": "parent_already_has_pdf",
            },
            "inventory": inventory,
        }

    # The DOI is derived automatically from the item metadata when it was not
    # supplied, so the request URL is always built inside the worker.
    doi = normalize_doi_value(options.doi) or doi_from_metadata(metadata)
    if not doi:
        return {
            "ok": False,
            "status": "missing_doi",
            "download": {
                "ok": False,
                "status": "missing_doi",
                "reason": "no_doi_for_item",
            },
        }

    raw_download = download_scihub_pdf(
        doi,
        output_dir=options.output_dir or DEFAULT_DOWNLOAD_DIR,
        mirrors=options.mirrors,
        user_agent=options.user_agent,
        timeout_seconds=max(1, int(options.timeout_seconds)),
        max_bytes=options.max_bytes,
        expected_title=metadata.title or "",
    )
    if not isinstance(raw_download, dict):
        return {
            "ok": False,
            "status": "download_invalid_result",
            "download": raw_download,
        }
    download = raw_download
    download_ok_value = download.get("ok")
    download_ok = download_ok_value is True
    download_status = download.get("status")
    if download_ok_value is not True and download_ok_value is not False:
        download_status = "download_invalid_result"
    output_path_value = download.get("output_path")
    output_path = (
        output_path_value.strip() if isinstance(output_path_value, str) else ""
    )
    if download_ok and not output_path:
        download_ok = False
        download_status = "download_invalid_result"
    payload: dict[str, Any] = {
        "ok": download_ok,
        "status": download_status,
        "download": download,
    }
    if not download_ok:
        return payload
    if options.ensure_active is not None:
        options.ensure_active()

    attach = module.attach_pdf_to_zotero_parent(
        config,
        item_key=options.item_key,
        source_path=Path(output_path),
        data_dir=options.data_dir,
        force=options.force_attach,
    )
    if not isinstance(attach, dict):
        payload["ok"] = False
        payload["status"] = "attach_invalid_result"
        payload["attach"] = attach
        return payload
    payload["attach"] = attach
    attach_ok_value = attach.get("ok")
    attach_ok = attach_ok_value is True
    payload["ok"] = attach_ok
    if attach_ok:
        payload["status"] = "attached"
    elif attach_ok_value is not False:
        payload["status"] = "attach_invalid_result"
    else:
        payload["status"] = str(
            attach.get("status") or attach.get("reason") or "attach_failed"
        )
    return payload


def doi_from_metadata(metadata: Any) -> str:
    """Best-effort automatic DOI extraction from a LocalItemMetadata object."""
    fields = getattr(metadata, "fields", None)
    if isinstance(fields, dict):
        explicit = str(fields.get("DOI") or "").strip()
        if explicit:
            normalized = package_normalize_doi(explicit)
            if normalized:
                return normalized
    parts: list[str] = [str(getattr(metadata, "title", "") or "")]
    if isinstance(fields, dict):
        parts.extend(str(value) for value in fields.values())
    found = (
        package_extract_doi_from_text(" ".join(part for part in parts if part)) or ""
    )
    return package_normalize_doi(found)


FileIdentity = tuple[int, int]


def _write_owned_download(path: Path, payload: bytes) -> FileIdentity:
    owner_identity: FileIdentity | None = None
    try:
        with path.open("xb") as handle:
            stat_result = os.fstat(handle.fileno())
            owner_identity = (int(stat_result.st_dev), int(stat_result.st_ino))
            written = handle.write(payload)
            if written != len(payload):
                raise OSError(
                    f"Short Sci-Hub PDF write: {written}/{len(payload)} bytes"
                )
        return owner_identity
    except BaseException as exc:
        if owner_identity is None:
            owner_identity = _download_file_identity(path)
        removed, cleanup_error = _remove_owned_download(
            path,
            owner_identity=owner_identity,
        )
        if not removed:
            exc.add_note(
                "Sci-Hub partial download cleanup failed for "
                f"{path}: {cleanup_error or 'artifact remains'}"
            )
        raise


def _download_file_identity(path: Path) -> FileIdentity | None:
    try:
        stat_result = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(stat_result.st_mode):
        return None
    return int(stat_result.st_dev), int(stat_result.st_ino)


def _owned_download_matches(
    path: Path,
    *,
    owner_identity: FileIdentity,
    expected_size: int,
    expected_sha256: str,
) -> bool:
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if (int(before.st_dev), int(before.st_ino)) != owner_identity:
                return False
            digest = hashlib.sha256()
            size = 0
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > expected_size:
                    return False
                digest.update(chunk)
            after = os.fstat(handle.fileno())
    except OSError:
        return False
    return (
        (int(after.st_dev), int(after.st_ino)) == owner_identity
        and size == expected_size
        and digest.hexdigest() == expected_sha256
    )


def _remove_owned_download(
    path: Path,
    *,
    owner_identity: FileIdentity | None,
) -> tuple[bool, str | None]:
    if owner_identity is None:
        try:
            path.lstat()
        except FileNotFoundError:
            return True, None
        except OSError as exc:
            return False, f"{type(exc).__name__}: {exc}"[:500]
        return False, "download ownership is unavailable"
    current_identity = _download_file_identity(path)
    if current_identity is None:
        try:
            path.lstat()
        except FileNotFoundError:
            return True, None
        except OSError as exc:
            return False, f"{type(exc).__name__}: {exc}"[:500]
        return False, "download target is no longer a regular owned file"
    if current_identity != owner_identity:
        return False, "download target ownership changed"
    try:
        path.unlink()
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"[:500]
    try:
        path.lstat()
    except FileNotFoundError:
        return True, None
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"[:500]
    return False, "download artifact remains after cleanup"


def _safe_doi(doi: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", doi.strip())
    return cleaned.strip("._-")[:80] or "document"


def _script_module() -> Any:
    script_path = provider_script_path(
        "researchgate_pdf_browser_download.py", package_root=PROJECT_ROOT
    )
    spec = importlib.util.spec_from_file_location(
        "researchgate_pdf_browser_download", script_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load Zotero parent attach helper: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
