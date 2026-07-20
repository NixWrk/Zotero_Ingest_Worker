from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .local_zotero import LocalAttachment, LocalItemMetadata


MAX_RELAY_RESPONSE_BYTES = 8_000_000
_TRANSIENT_HTTP_STATUSES = {207, 408, 429, 500, 502, 503, 504}
_TRANSIENT_RELAY_CODES = {
    "RELAY_RESPONSE_READ_FAILED",
    "WEB_API_REQUEST_FAILED",
    "WEBDAV_REQUEST_FAILED",
}
_TRANSIENT_ERROR_MARKERS = (
    "temporary failure in name resolution",
    "name resolution",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "connection reset",
    "connection refused",
    "network is unreachable",
)


class ZoteroRelayClient:
    def __init__(self, config: Any):
        self.config = config

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.config, "zotero_relay_url", ""))

    def request_json(
        self,
        *,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        error_label: str,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("ZOTERO_RELAY_URL is not configured.")
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        token = str(getattr(self.config, "zotero_relay_token", "") or "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        timeout = int(getattr(self.config, "request_timeout_seconds", 300) or 300)
        attempts = max(
            1, int(getattr(self.config, "zotero_relay_request_attempts", 3) or 3)
        )
        retry_delay = max(
            0.0,
            float(getattr(self.config, "zotero_relay_retry_delay_seconds", 2.0) or 0.0),
        )
        urls = relay_url_candidates(str(getattr(self.config, "zotero_relay_url")), path)
        last_error: RuntimeError | None = None
        for attempt in range(attempts):
            result: dict[str, Any] | None = None
            for index, url in enumerate(urls):
                request = urllib.request.Request(
                    url, data=data, headers=headers, method=method
                )
                try:
                    with urllib.request.urlopen(request, timeout=timeout) as response:
                        result = _read_relay_result(response)
                    break
                except urllib.error.HTTPError as exc:
                    result = _read_relay_result(exc, http_status=exc.code)
                    last_error = RuntimeError(f"{error_label} failed: {result}")
                    if exc.code in _TRANSIENT_HTTP_STATUSES and attempt + 1 < attempts:
                        break
                    raise last_error from exc
                except (urllib.error.URLError, TimeoutError) as exc:
                    last_error = RuntimeError(f"{error_label} failed: {exc}")
                    if index + 1 < len(urls):
                        continue
                    if attempt + 1 < attempts:
                        break
                    raise last_error from exc
            if result is not None and result.get("ok") is True:
                return result
            if result is not None:
                last_error = RuntimeError(f"{error_label} failed: {result}")
                if not _is_retryable_relay_result(result) or attempt + 1 >= attempts:
                    raise last_error
            if attempt + 1 < attempts:
                _sleep_before_retry(retry_delay, attempt)
        raise last_error or RuntimeError(f"{error_label} failed: no relay response")

    def create_parent_attachment(
        self,
        *,
        metadata: LocalItemMetadata,
        source_path: Path,
        filename: str,
        title: str,
        content_type: str,
        probe_attachment_key: str | None,
        dedupe_prefix: str,
        source_sha256: str | None = None,
    ) -> dict[str, Any]:
        digest: str | None = None
        if source_sha256 is None:
            source_stat = source_path.stat()
            source_identity = f"{source_stat.st_size}:{source_stat.st_mtime_ns}"
        else:
            digest = source_sha256.strip().casefold()
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError(
                    "source_sha256 must be a 64-character hexadecimal digest"
                )
            source_identity = f"sha256:{digest}"
        payload = {
            "sourcePath": str(source_path),
            "filename": filename,
            "title": title,
            "contentType": content_type,
            "libraryId": metadata.library_id,
            "probeAttachmentKey": probe_attachment_key or "",
            "deduplicationKey": (
                f"{dedupe_prefix}:{metadata.library_id}:{metadata.key}:"
                f"{source_identity}"
            ),
        }
        if digest is not None:
            payload["sourceSha256"] = digest
        return self.request_json(
            method="POST",
            path=f"/attachments/parents/{urllib.parse.quote(metadata.key, safe='')}/attachments/file",
            payload=payload,
            error_label="zotero-file-relay parent attachment",
        )

    def create_html_sibling(
        self,
        *,
        attachment: LocalAttachment,
        source_path: Path,
        filename: str,
        title: str,
        arxiv_id: str | None = None,
        deduplication_key: str | None = None,
        error_label: str = "zotero-file-relay arXiv HTML sibling",
    ) -> dict[str, Any]:
        if deduplication_key is None:
            if arxiv_id is None:
                raise ValueError(
                    "create_html_sibling requires arxiv_id or deduplication_key."
                )
            deduplication_key = (
                f"arxiv-html-sibling:{attachment.state_key}:{arxiv_id}:"
                f"{source_path.stat().st_mtime_ns}"
            )
        return self.request_json(
            method="POST",
            path=f"/attachments/{urllib.parse.quote(attachment.key, safe='')}/siblings/html",
            payload={
                "sourcePath": str(source_path),
                "filename": filename,
                "title": title,
                "libraryId": attachment.library_id,
                "deduplicationKey": deduplication_key,
            },
            error_label=error_label,
        )

    def ensure_parent(self, attachment: LocalAttachment) -> dict[str, Any]:
        return self.request_json(
            method="POST",
            path=f"/attachments/{urllib.parse.quote(attachment.key, safe='')}/parent/ensure",
            payload={
                "libraryId": attachment.library_id,
                "title": Path(attachment.filename).stem,
                "deduplicationKey": f"ensure-parent:{attachment.state_key}",
            },
            error_label="zotero-file-relay parent preflight",
        )

    def trash_attachment(
        self,
        *,
        attachment: LocalAttachment,
        dry_run: bool,
        delete_webdav: bool = False,
    ) -> dict[str, Any]:
        return self.request_json(
            method="POST",
            path=f"/attachments/{urllib.parse.quote(attachment.key, safe='')}/trash",
            payload={
                "libraryId": attachment.library_id,
                "dryRun": dry_run,
                "deleteWebdav": delete_webdav,
                "deduplicationKey": f"trash-html:{attachment.state_key}:webdav={int(delete_webdav)}",
            },
            error_label="zotero-file-relay trash",
        )

    def trash_generated_html_parent(
        self,
        *,
        library_id: str,
        parent_key: str,
        deleted_child_keys: list[str],
        dry_run: bool,
    ) -> dict[str, Any]:
        return self.request_json(
            method="POST",
            path=f"/items/{urllib.parse.quote(parent_key, safe='')}/trash-if-generated-html-only",
            payload={
                "libraryId": library_id,
                "dryRun": dry_run,
                "deletedChildKeys": deleted_child_keys,
                "deduplicationKey": (
                    f"trash-empty-html-parent:{library_id}:{parent_key}:"
                    f"{','.join(sorted(deleted_child_keys))}"
                ),
            },
            error_label="zotero-file-relay parent cleanup",
        )

    def patch_parent_metadata(
        self,
        *,
        attachment: LocalAttachment,
        metadata: LocalItemMetadata,
        fields: dict[str, str],
        policy: str,
        patch_digest: str,
    ) -> dict[str, Any]:
        return self.request_json(
            method="PATCH",
            path=f"/attachments/{urllib.parse.quote(attachment.key, safe='')}/parent/metadata",
            payload={
                "fields": fields,
                "policy": policy,
                "expectedVersion": 0,
                "libraryId": attachment.library_id,
                "deduplicationKey": (
                    f"metadata-enrich:{attachment.state_key}:{metadata.key}:"
                    f"refresh:{patch_digest}:{policy}"
                ),
            },
            error_label="zotero-file-relay metadata patch",
        )


def relay_url_candidates(base_url: str, path: str) -> list[str]:
    base = base_url.rstrip("/")
    urls = [f"{base}{path}"]
    parsed = urllib.parse.urlparse(base)
    if parsed.hostname == "zotero-file-relay":
        port = f":{parsed.port}" if parsed.port else ""
        fallback = parsed._replace(netloc=f"127.0.0.1{port}").geturl().rstrip("/")
        fallback_url = f"{fallback}{path}"
        if fallback_url not in urls:
            urls.append(fallback_url)
    return urls


def _is_retryable_relay_result(result: dict[str, Any]) -> bool:
    error = result.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or "").upper()
        details = error.get("details")
        status = None
        if isinstance(details, dict):
            raw_status = details.get("status")
            try:
                status = int(raw_status) if raw_status is not None else None
            except (TypeError, ValueError):
                status = None
        text = json.dumps(error, ensure_ascii=False).casefold()
        if status in _TRANSIENT_HTTP_STATUSES or any(
            marker in text for marker in _TRANSIENT_ERROR_MARKERS
        ):
            return True
        return code in _TRANSIENT_RELAY_CODES and status is None
    text = json.dumps(result, ensure_ascii=False).casefold()
    return any(marker in text for marker in _TRANSIENT_ERROR_MARKERS)


def _sleep_before_retry(base_delay: float, attempt: int) -> None:
    if base_delay <= 0:
        return
    time.sleep(base_delay * (2**attempt))


def _relay_response_error(
    code: str,
    message: str,
    *,
    details: dict[str, object],
) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "details": details,
        },
    }


def _read_relay_result(
    stream: Any,
    *,
    http_status: int | None = None,
) -> dict[str, Any]:
    details: dict[str, object] = {}
    if http_status is not None:
        details["httpStatus"] = http_status
    try:
        raw = stream.read(MAX_RELAY_RESPONSE_BYTES + 1)
    except Exception as exc:
        details["type"] = type(exc).__name__
        return _relay_response_error(
            "RELAY_RESPONSE_READ_FAILED",
            "Relay response body could not be read.",
            details=details,
        )
    if not isinstance(raw, (bytes, bytearray)):
        details["type"] = type(raw).__name__
        return _relay_response_error(
            "INVALID_RELAY_RESPONSE",
            "Relay response body must be bytes.",
            details=details,
        )
    body = bytes(raw)
    if len(body) > MAX_RELAY_RESPONSE_BYTES:
        details.update(
            {
                "maxBytes": MAX_RELAY_RESPONSE_BYTES,
                "receivedAtLeast": len(body),
            }
        )
        return _relay_response_error(
            "RELAY_RESPONSE_TOO_LARGE",
            "Relay response body exceeds the configured safety limit.",
            details=details,
        )
    return _decoded_relay_result(body, http_status=http_status)


def _decoded_relay_result(
    raw: bytes,
    *,
    http_status: int | None = None,
) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        details: dict[str, object] = {"type": type(exc).__name__}
        if http_status is not None:
            details["httpStatus"] = http_status
        return _relay_response_error(
            "INVALID_RELAY_RESPONSE",
            "Relay response body is not valid UTF-8 JSON.",
            details=details,
        )
    return _normalized_relay_result(value)


def _normalized_relay_result(value: object) -> dict[str, Any]:
    if isinstance(value, dict) and (
        value.get("ok") is True or value.get("ok") is False
    ):
        return value
    return {
        "ok": False,
        "error": {
            "code": "INVALID_RELAY_RESPONSE",
            "message": "Relay response must be a mapping with a boolean ok field.",
            "details": {
                "type": type(value).__name__,
            },
        },
    }
