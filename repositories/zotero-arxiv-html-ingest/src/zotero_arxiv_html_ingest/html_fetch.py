from __future__ import annotations

import urllib.parse
import urllib.request

from zotero_metadata_enrichment.provider_http import read_response_bytes, throttled_urlopen
from zotero_metadata_enrichment.safe_http import host_suffix_redirect

from .identifiers import normalize_arxiv_id
from .text import strip_html


class ArxivHtmlClient:
    def __init__(
        self,
        *,
        timeout_seconds: int = 120,
        user_agent: str = "zotero-arxiv-html-ingest/0.1",
        min_text_chars: int = 200,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.min_text_chars = min_text_chars

    def fetch(self, arxiv_id: str) -> tuple[str, dict[str, object]]:
        arxiv_id = normalize_arxiv_id(arxiv_id)
        if not arxiv_id:
            raise ValueError("arXiv id is empty.")
        url = f"https://arxiv.org/html/{urllib.parse.quote(arxiv_id, safe='/')}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                "User-Agent": self.user_agent,
            },
            method="GET",
        )
        with throttled_urlopen(
            request,
            timeout=self.timeout_seconds,
            redirect_validator=host_suffix_redirect("arxiv.org"),
        ) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            body = read_response_bytes(response, error_label=url)
            html_text = body.decode(charset, errors="replace")
        validation = validate_arxiv_html(html_text, min_text_chars=self.min_text_chars)
        if not validation["ok"]:
            raise RuntimeError(f"arXiv HTML validation failed: {validation['reason']}")
        return html_text, validation


def validate_arxiv_html(html_text: str, *, min_text_chars: int = 200) -> dict[str, object]:
    if "<html" not in html_text[:2000].lower():
        return {"ok": False, "reason": "missing_html_tag", "text_chars": 0}
    body_text = strip_html(html_text)
    text_chars = len(body_text)
    if text_chars < min_text_chars:
        return {
            "ok": False,
            "reason": "too_little_text",
            "text_chars": text_chars,
            "min_text_chars": min_text_chars,
        }
    return {"ok": True, "reason": "ok", "text_chars": text_chars}

