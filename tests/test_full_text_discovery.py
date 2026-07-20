from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from zotero_ingest_worker import metadata_processor


def test_full_text_orchestrator_does_not_retry_pdf_when_pdf_pass_already_ran(
    monkeypatch: Any,
    tmp_path,
) -> None:
    discovery_module = metadata_processor.full_text_discovery
    calls: list[dict[str, Any]] = []

    class FakeResult:
        discovery = SimpleNamespace(
            locations=[SimpleNamespace(url="https://example.test/article.pdf")]
        )

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
        raise AssertionError(
            "PDF fallback should not retry after a completed pdf_downloads pass."
        )

    monkeypatch.setattr(
        discovery_module, "package_download_pdf_sources", unexpected_pdf_fallback
    )

    payload = discovery_module.FullTextDiscoveryOrchestrator(
        config=SimpleNamespace(
            arxiv_html_fetch_timeout_seconds=1, metadata_user_agent="test-agent"
        ),
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
    assert (
        summary.browser_fallbacks[0]["url"]
        == "https://www.researchgate.net/publication/123_example"
    )
    assert (
        discovery_module.full_text_worker_status(payload)
        == "browser_pdf_fallback_available"
    )


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


@pytest.mark.parametrize(
    "invalid_ok", ["false", 1], ids=["string-false", "integer-one"]
)
def test_full_text_summary_requires_boolean_download_success(
    invalid_ok: object,
) -> None:
    discovery_module = metadata_processor.full_text_discovery
    payload = {
        "status": "unresolved",
        "html_downloads": [
            {
                "ok": invalid_ok,
                "output_path": "article.html",
                "article": {
                    "text_chars": 25_000,
                    "markers": ["article_tag", "article_body"],
                    "section_markers": ["methods", "results", "references"],
                },
            }
        ],
        "pdf_downloads": [
            {
                "ok": invalid_ok,
                "output_path": "article.pdf",
                "identity": {"needs_ocr": False},
            }
        ],
    }

    summary = discovery_module.summarize_full_text_payload(payload)

    assert summary.accepted_html == ()
    assert summary.rejected_html == ()
    assert summary.successful_pdf == ()
    assert summary.ocr_candidates == ()
    assert summary.worker_status == "unresolved"


@pytest.mark.parametrize(
    "invalid_ok", ["false", 1], ids=["string-false", "integer-one"]
)
def test_arxiv_rescue_requires_boolean_article_assessment_success(
    invalid_ok: object,
    monkeypatch: Any,
    tmp_path,
) -> None:
    discovery_module = metadata_processor.full_text_discovery

    class InvalidAssessment:
        def to_dict(self) -> dict[str, object]:
            return {"ok": invalid_ok, "reason": "invalid_assessment"}

    monkeypatch.setattr(
        discovery_module,
        "package_assess_article_html",
        lambda *_args, **_kwargs: InvalidAssessment(),
    )

    def unexpected_snapshot(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        raise AssertionError("Malformed article assessment must not be published.")

    monkeypatch.setattr(
        discovery_module,
        "package_write_html_snapshot",
        unexpected_snapshot,
    )
    payload = {
        "html_downloads": [
            {
                "ok": True,
                "kind": "landing",
                "url": "https://arxiv.org/abs/2501.00001",
                "final_url": "https://arxiv.org/abs/2501.00001",
                "output_path": str(tmp_path / "landing.html"),
                "article": {"ok": False, "reason": "arxiv_abs_landing"},
            }
        ]
    }
    orchestrator = discovery_module.FullTextDiscoveryOrchestrator(
        config=SimpleNamespace(
            arxiv_html_fetch_timeout_seconds=1,
            metadata_user_agent="test-agent",
        ),
        metadata_config=SimpleNamespace(request_timeout_seconds=1),
        discover_full_text=lambda **_kwargs: None,
        fetch_arxiv_html=lambda _arxiv_id: "<html></html>",
    )

    orchestrator._append_arxiv_html_candidates_from_abs_landings(
        payload=payload,
        output_dir=tmp_path,
        metadata=SimpleNamespace(title="Article"),
    )

    assert len(payload["html_downloads"]) == 2
    rejected = payload["html_downloads"][-1]
    assert rejected["ok"] is False
    assert rejected["status"] == "invalid_assessment"


@pytest.mark.parametrize(
    "invalid_ok", ["false", 1], ids=["string-false", "integer-one"]
)
def test_standardized_package_path_requires_boolean_success(
    invalid_ok: object,
    monkeypatch: Any,
    tmp_path,
) -> None:
    discovery_module = metadata_processor.full_text_discovery
    package = {
        "ok": invalid_ok,
        "article_html_path": str(tmp_path / "untrusted.html"),
    }
    monkeypatch.setattr(
        discovery_module,
        "standardize_native_html_download",
        lambda *_args, **_kwargs: package,
    )
    download = {
        "ok": True,
        "output_path": str(tmp_path / "article.html"),
        "article": {
            "text_chars": 25_000,
            "markers": ["article_tag", "article_body"],
            "section_markers": ["methods", "results", "references"],
        },
    }
    payload = {"html_downloads": [download]}

    standardized = discovery_module.standardize_accepted_html_downloads(
        payload,
        metadata=SimpleNamespace(title="Article"),
        output_dir=tmp_path,
        source_context="test",
    )

    assert standardized == [package]
    assert "standard_article_html_path" not in download


@pytest.mark.parametrize(
    "invalid_needs_ocr",
    ["false", 1],
    ids=["string-false", "integer-one"],
)
def test_full_text_summary_requires_exact_boolean_needs_ocr(
    invalid_needs_ocr: object,
) -> None:
    discovery_module = metadata_processor.full_text_discovery
    payload = {
        "status": "downloaded",
        "html_downloads": [],
        "pdf_downloads": [
            {
                "ok": True,
                "output_path": "article.pdf",
                "identity": {"needs_ocr": invalid_needs_ocr},
            }
        ],
    }

    summary = discovery_module.summarize_full_text_payload(payload)

    assert len(summary.successful_pdf) == 1
    assert summary.ocr_candidates == ()
    assert summary.worker_status == "pdf_found"


@pytest.mark.parametrize(
    "invalid_text_chars",
    ["30000", True, 30_000.5, -1],
    ids=["numeric-string", "boolean", "float", "negative"],
)
def test_article_verdict_rejects_non_exact_text_character_count(
    invalid_text_chars: object,
) -> None:
    from zotero_ingest_worker import full_text_article

    verdict = full_text_article.html_download_article_verdict(
        {
            "ok": True,
            "kind": "html",
            "url": "https://example.test/article",
            "output_path": "/tmp/article.html",
            "article": {
                "ok": True,
                "text_chars": invalid_text_chars,
                "markers": ["article_tag"],
                "section_markers": ["methods", "results", "references"],
            },
        }
    )

    assert verdict["ok"] is False
    assert verdict["reason"] == "article_validator_invalid_text_chars"


def test_arxiv_abs_parser_preserves_legacy_category_identifier() -> None:
    from zotero_ingest_worker import full_text_article

    assert (
        full_text_article.arxiv_id_from_abs_url(
            "https://arxiv.org/abs/hep-th/9901001v2"
        )
        == "hep-th/9901001"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://evilarxiv.org/abs/2501.00001",
        "https://arxiv.org.example/abs/2501.00001",
        "http://[broken/abs/2501.00001",
    ],
    ids=["lookalike-prefix", "lookalike-suffix", "malformed-ipv6"],
)
def test_arxiv_abs_classification_rejects_lookalike_or_malformed_hosts(
    url: str,
) -> None:
    from zotero_ingest_worker import full_text_article

    assert full_text_article.arxiv_id_from_abs_url(url) is None
    assert full_text_article.is_arxiv_abs_landing_download({"url": url}) is False


def test_full_text_summary_rejects_non_string_paths_and_malformed_relay_shape() -> None:
    discovery_module = metadata_processor.full_text_discovery
    payload = {
        "status": "unresolved",
        "relay_attachment": "malformed",
        "html_downloads": [
            {
                "ok": True,
                "output_path": 1,
                "article": {
                    "ok": True,
                    "text_chars": 25_000,
                    "markers": ["article_tag"],
                    "section_markers": ["methods", "results"],
                },
            }
        ],
        "pdf_downloads": [
            {
                "ok": True,
                "output_path": 2,
                "identity": {"needs_ocr": False},
            }
        ],
    }

    summary = discovery_module.summarize_full_text_payload(payload)

    assert summary.accepted_html == ()
    assert summary.successful_pdf == ()
    assert summary.output_path is None
    assert summary.worker_status == "html_rejected"


def test_existing_pdf_status_requires_exact_success_contract() -> None:
    discovery_module = metadata_processor.full_text_discovery
    payload = {
        "status": "unresolved",
        "existing_pdf_enqueue": {
            "ok": "true",
            "html_enqueue": {
                "classification": "queued",
                "job": {"job_id": 42},
            },
        },
    }

    assert discovery_module.full_text_worker_status(payload) == "unresolved"


def test_researchgate_fallback_does_not_treat_truthy_inventory_flag_as_pdf() -> None:
    discovery_module = metadata_processor.full_text_discovery
    payload = {
        "status": "unresolved",
        "discovery": {
            "locations": [
                {
                    "source": "researchgate",
                    "url": "https://www.researchgate.net/publication/123_example",
                    "kind": "landing",
                }
            ]
        },
        "pdf_downloads": [],
    }

    fallbacks = discovery_module.researchgate_browser_fallbacks(
        payload,
        inventory={"has_pdf": "false"},
    )

    assert [item["url"] for item in fallbacks] == [
        "https://www.researchgate.net/publication/123_example"
    ]
