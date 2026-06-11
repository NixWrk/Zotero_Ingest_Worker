from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from ..identifiers import normalize_doi


class OpenCitationsClient:
    def __init__(
        self,
        *,
        timeout_seconds: int = 60,
        user_agent: str = "zotero-metadata-enrichment/0.1",
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

    def references(self, doi: str) -> list[dict[str, Any]]:
        return self._get_list(f"https://opencitations.net/index/coci/api/v1/references/{self._doi_path(doi)}")

    def citations(self, doi: str) -> list[dict[str, Any]]:
        return self._get_list(f"https://opencitations.net/index/coci/api/v1/citations/{self._doi_path(doi)}")

    def citation_count(self, doi: str) -> int | None:
        rows = self._get_list(f"https://opencitations.net/index/coci/api/v1/citation-count/{self._doi_path(doi)}")
        if not rows:
            return None
        value = rows[0].get("count") if isinstance(rows[0], dict) else None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _doi_path(self, doi: str) -> str:
        doi = normalize_doi(doi)
        if not doi:
            raise ValueError("DOI is empty.")
        return urllib.parse.quote(doi, safe="")

    def _get_list(self, url: str) -> list[dict[str, Any]]:
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": self.user_agent},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, list):
            raise RuntimeError(f"Expected JSON list from {url}")
        return [row for row in payload if isinstance(row, dict)]
