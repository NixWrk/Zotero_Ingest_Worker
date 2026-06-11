from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from zotero_ingest_worker import metadata_processor


def test_full_text_orchestrator_does_not_retry_pdf_when_pdf_pass_already_ran(
    monkeypatch: Any,
    tmp_path,
) -> None:
    discovery_module = metadata_processor.full_text_discovery
    calls: list[dict[str, Any]] = []

    class FakeResult:
        discovery = SimpleNamespace(locations=[SimpleNamespace(url="https://example.test/article.pdf")])

        def to_dict(self) -> dict[str, Any]:
            return {
                "status": "unresolved",
                "html_downloads": [
                    {
                        "ok": True,
                        "status": "downloaded",
                        "output_path": str(tmp_path / "landing.html"),
                        "article": {"ok": False, "reason": "weak_landing"},
                    }
                ],
                "pdf_downloads": [],
            }

    def fake_discover_full_text(**kwargs: Any) -> FakeResult:
        calls.append(kwargs)
        return FakeResult()

    def unexpected_pdf_fallback(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise AssertionError("PDF fallback should not retry after a completed pdf_downloads pass.")

    monkeypatch.setattr(discovery_module, "package_download_pdf_sources", unexpected_pdf_fallback)

    payload = discovery_module.FullTextDiscoveryOrchestrator(
        config=SimpleNamespace(arxiv_html_fetch_timeout_seconds=1, metadata_user_agent="test-agent"),
        metadata_config=SimpleNamespace(request_timeout_seconds=1),
        discover_full_text=fake_discover_full_text,
        fetch_arxiv_html=lambda _arxiv_id: "",
    ).discover_payload(
        attachment=SimpleNamespace(),
        metadata=SimpleNamespace(title="Article"),
        inventory={"has_pdf": False},
        output_dir=tmp_path,
        source_context="parent_item",
    )

    assert calls[0]["max_pdf_downloads"] == 3
    assert payload["pdf_downloads"] == []
    assert payload["worker_status"] == "html_rejected"


def test_full_text_summary_reports_researchgate_browser_fallback() -> None:
    discovery_module = metadata_processor.full_text_discovery
    payload = {
        "status": "unresolved",
        "existing_full_text_inventory": {"has_pdf": False},
        "discovery": {
            "locations": [
                {
                    "source": "semantic_scholar",
                    "url": "https://www.researchgate.net/publication/123_example",
                    "kind": "landing",
                }
            ]
        },
        "html_downloads": [
            {
                "ok": True,
                "output_path": "landing.html",
                "article_verdict": {"ok": False, "reason": "weak_landing"},
            }
        ],
        "pdf_downloads": [],
    }

    summary = discovery_module.summarize_full_text_payload(payload)

    assert summary.worker_status == "browser_pdf_fallback_available"
    assert len(summary.browser_fallbacks) == 1
    assert summary.browser_fallbacks[0]["url"] == "https://www.researchgate.net/publication/123_example"
    assert discovery_module.full_text_worker_status(payload) == "browser_pdf_fallback_available"


def test_full_text_summary_suppresses_browser_fallback_when_pdf_exists() -> None:
    discovery_module = metadata_processor.full_text_discovery
    payload = {
        "status": "unresolved",
        "existing_full_text_inventory": {"has_pdf": True},
        "discovery": {
            "locations": [
                {
                    "source": "semantic_scholar",
                    "url": "https://www.researchgate.net/publication/123_example",
                    "kind": "landing",
                }
            ]
        },
        "pdf_downloads": [],
    }

    summary = discovery_module.summarize_full_text_payload(payload)

    assert summary.browser_fallbacks == ()
    assert summary.worker_status == "unresolved"
