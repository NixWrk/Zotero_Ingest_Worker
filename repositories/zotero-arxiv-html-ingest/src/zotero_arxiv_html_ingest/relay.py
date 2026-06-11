from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .models import LocalAttachment
from .storage import arxiv_html_filename


class ZoteroRelayClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout_seconds: int = 300,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    def add_html_sibling(
        self,
        *,
        attachment: LocalAttachment,
        source_path: Path,
        arxiv_id: str,
        filename: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        filename = filename or arxiv_html_filename(attachment.filename)
        payload = {
            "sourcePath": str(source_path),
            "filename": filename,
            "title": title or filename,
            "libraryId": attachment.library_id,
            "deduplicationKey": (
                f"arxiv-html-sibling:{attachment.state_key}:{arxiv_id}:"
                f"{source_path.stat().st_mtime_ns}"
            ),
        }
        return self._json(
            method="POST",
            path=f"/attachments/{urllib.parse.quote(attachment.key, safe='')}/siblings/html",
            payload=payload,
        )

    def _json(self, *, method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not isinstance(result, dict):
            raise RuntimeError("Relay returned non-object JSON.")
        if not result.get("ok"):
            raise RuntimeError(f"Relay request failed: {result}")
        return result

