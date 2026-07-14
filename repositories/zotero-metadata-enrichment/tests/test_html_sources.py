from __future__ import annotations

import urllib.error
from pathlib import Path
from typing import Any

from zotero_metadata_enrichment import provider_http
from zotero_metadata_enrichment.html_sources import (
    ResourceReferenceParser,
    assess_article_html,
    download_html_sources,
    fetch_html_source,
    parse_srcset,
    write_html_snapshot,
)
from zotero_metadata_enrichment.models import FullTextLocation
from zotero_metadata_enrichment.provider_http import HostThrottle


class FakeResponse:
    def __init__(
        self,
        *,
        url: str,
        content_type: str,
        body: bytes,
        content_length: int | str | None = None,
    ) -> None:
        self.url = url
        self.headers = {"Content-Type": content_type}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self._body = body
        self.read_sizes: list[int] = []

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        self.read_sizes.append(_size)
        return self._body if _size < 0 else self._body[:_size]


def test_fetch_html_source_saves_html(monkeypatch: Any, tmp_path: Path) -> None:
    def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
        assert timeout == 10
        if request.full_url == "https://journal.example/article":
            return FakeResponse(
                url="https://journal.example/article",
                content_type="text/html; charset=utf-8",
                body=(
                    "<!doctype html><html><head><title>Article</title></head>"
                    "<body><article><h1>Article</h1><section>Abstract</section>"
                    f"<section>{'Methods results discussion conclusion. ' * 400}</section>"
                    "<section>References</section><img src=\"/figures/one.png\"></article></body></html>"
                ).encode("utf-8"),
            )
        assert request.full_url == "https://journal.example/figures/one.png"
        return FakeResponse(
            url="https://journal.example/figures/one.png",
            content_type="image/png",
            body=b"PNG",
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    location = FullTextLocation(source="crossref", url="https://journal.example/article", kind="html")

    result = fetch_html_source(
        location,
        output_dir=tmp_path,
        timeout_seconds=10,
        expected_title="Article",
    )

    assert result.ok
    assert result.status == "downloaded"
    saved = Path(result.output_path).read_text(encoding="utf-8")
    assert saved.startswith("<!doctype html>")
    assert "01.crossref.journal.example" in Path(result.output_path).name
    assert result.article["reason"] == "article_html"
    assert result.assets["saved"] == 1
    assert "_assets/" in saved


def test_fetch_html_source_rate_limit_sets_host_cooldown(monkeypatch: Any, tmp_path: Path) -> None:
    now = 0.0
    sleeps: list[float] = []

    def clock() -> float:
        return now

    def sleeper(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    monkeypatch.setattr(provider_http, "_GLOBAL_HOST_THROTTLE", HostThrottle(clock=clock, sleeper=sleeper))

    def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
        raise urllib.error.HTTPError(
            request.full_url,
            429,
            "Too Many Requests",
            {"Retry-After": "5"},
            None,
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = fetch_html_source(
        FullTextLocation(source="crossref", url="https://journal.example/article", kind="html"),
        output_dir=tmp_path,
    )

    assert not result.ok
    assert result.status == "http_error"
    provider_http._GLOBAL_HOST_THROTTLE.wait("journal.example", min_interval_seconds=0.0)
    assert sleeps == [5.0]


def test_fetch_html_source_uses_base_href_for_relative_assets(monkeypatch: Any, tmp_path: Path) -> None:
    requested_urls: list[str] = []

    def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
        requested_urls.append(request.full_url)
        if request.full_url == "https://arxiv.org/html/2602.09735":
            return FakeResponse(
                url="https://arxiv.org/html/2602.09735",
                content_type="text/html; charset=utf-8",
                body=(
                    "<!doctype html><html><head><title>Article</title>"
                    "<base href=\"/html/2602.09735v1/\"></head>"
                    "<body><main class=\"ltx_document arxiv-html\"><section class=\"ltx_abstract\">Abstract</section>"
                    f"<section>{'Methods results discussion conclusion. ' * 240}</section>"
                    "<img src=\"fig_bci.png\"><section class=\"ltx_bibliography\">References</section>"
                    "</main></body></html>"
                ).encode("utf-8"),
            )
        assert request.full_url == "https://arxiv.org/html/2602.09735v1/fig_bci.png"
        return FakeResponse(
            url="https://arxiv.org/html/2602.09735v1/fig_bci.png",
            content_type="image/png",
            body=b"PNG",
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    location = FullTextLocation(source="arxiv", url="https://arxiv.org/html/2602.09735", kind="html")

    result = fetch_html_source(location, output_dir=tmp_path, timeout_seconds=10, expected_title="Article")

    assert result.ok
    assert "https://arxiv.org/html/2602.09735v1/fig_bci.png" in requested_urls
    saved = Path(result.output_path).read_text(encoding="utf-8")
    assert "<base" not in saved
    assert "src=\"fig_bci.png\"" not in saved
    assert "_assets/" in saved
    assert result.assets["saved"] == 1


def test_fetch_html_source_does_not_download_publisher_style_assets(monkeypatch: Any, tmp_path: Path) -> None:
    requested_urls: list[str] = []

    def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
        requested_urls.append(request.full_url)
        if request.full_url == "https://arxiv.org/html/2602.09735":
            return FakeResponse(
                url="https://arxiv.org/html/2602.09735",
                content_type="text/html; charset=utf-8",
                body=(
                    "<html><head><title>Article</title><base href=\"/html/2602.09735v1/\">"
                    "<style>@import \"./ar5iv.css\" layer(ar5iv);</style></head>"
                    "<body><main class=\"ltx_document arxiv-html\">"
                    "<section class=\"ltx_abstract\">Abstract</section>"
                    f"<section>{'Introduction methods results discussion. ' * 220}</section>"
                    "<section class=\"ltx_bibliography\">References</section>"
                    "</main></body></html>"
                ).encode("utf-8"),
            )
        raise AssertionError(request.full_url)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    location = FullTextLocation(source="arxiv", url="https://arxiv.org/html/2602.09735", kind="html")

    result = fetch_html_source(location, output_dir=tmp_path, timeout_seconds=10, expected_title="Article")

    assert result.ok
    assert requested_urls == ["https://arxiv.org/html/2602.09735"]
    saved = Path(result.output_path).read_text(encoding="utf-8")
    assert '@import "./ar5iv.css"' in saved
    assert result.assets["saved"] == 0


def test_fetch_html_source_rejects_declared_oversize_before_read(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    response = FakeResponse(
        url="https://journal.example/article",
        content_type="text/html; charset=utf-8",
        body=b"not read",
        content_length=101,
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: response)

    result = fetch_html_source(
        FullTextLocation(source="crossref", url="https://journal.example/article", kind="html"),
        output_dir=tmp_path,
        max_bytes=100,
    )

    assert result.status == "too_large"
    assert result.size == 101
    assert response.read_sizes == []


def test_fetch_html_source_rejects_redirect_to_private_host_before_read(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    response = FakeResponse(
        url="http://127.0.0.1/private",
        content_type="text/html",
        body=b"not read",
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: response)

    result = fetch_html_source(
        FullTextLocation(source="crossref", url="https://journal.example/article", kind="html"),
        output_dir=tmp_path,
    )

    assert result.status == "unsafe_redirect"
    assert result.error == "blocked_ip"
    assert response.read_sizes == []


def test_snapshot_asset_rejects_declared_oversize_before_read(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    asset_response = FakeResponse(
        url="https://journal.example/figure.png",
        content_type="image/png",
        body=b"not read",
        content_length=8_000_001,
    )

    def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
        if request.full_url == "https://journal.example/article":
            return FakeResponse(
                url=request.full_url,
                content_type="text/html",
                body=(
                    "<html><head><title>Article</title></head><body><article>"
                    f"{'Methods results discussion conclusion. ' * 400}"
                    '<section>References</section><img src="figure.png"></article></body></html>'
                ).encode("utf-8"),
            )
        return asset_response

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = fetch_html_source(
        FullTextLocation(source="crossref", url="https://journal.example/article", kind="html"),
        output_dir=tmp_path,
        expected_title="Article",
    )

    assert result.ok
    assert result.assets["saved"] == 0
    assert result.assets["failures"][0]["reason"] == "asset_too_large"
    assert asset_response.read_sizes == []


def test_snapshot_asset_rejects_redirect_to_private_host(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    asset_response = FakeResponse(
        url="http://169.254.169.254/latest/meta-data",
        content_type="image/png",
        body=b"not read",
    )

    def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
        if request.full_url == "https://journal.example/article":
            return FakeResponse(
                url=request.full_url,
                content_type="text/html",
                body=(
                    "<html><head><title>Article</title></head><body><article>"
                    f"{'Methods results discussion conclusion. ' * 400}"
                    '<section>References</section><img src="figure.png"></article></body></html>'
                ).encode("utf-8"),
            )
        return asset_response

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = fetch_html_source(
        FullTextLocation(source="crossref", url="https://journal.example/article", kind="html"),
        output_dir=tmp_path,
        expected_title="Article",
    )

    assert result.ok
    assert result.assets["saved"] == 0
    assert result.assets["failures"][0]["reason"] == "unsafe_redirect:blocked_ip"
    assert asset_response.read_sizes == []


def test_snapshot_asset_enforces_total_budget_before_read(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    response = FakeResponse(
        url="https://journal.example/figure.png",
        content_type="image/png",
        body=b"not read",
        content_length=6,
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: response)

    report = write_html_snapshot(
        b'<html><body><img src="https://journal.example/figure.png"></body></html>',
        base_url="https://journal.example/article",
        output_path=tmp_path / "article.html",
        timeout_seconds=10,
        user_agent="test",
        max_total_asset_bytes=5,
    )

    assert report["saved"] == 0
    assert report["failures"][0]["reason"] == "asset_total_bytes_limit"
    assert response.read_sizes == []


def test_snapshot_does_not_fetch_stylesheet_or_recursive_background_assets(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    requested_urls: list[str] = []

    def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
        requested_urls.append(request.full_url)
        raise AssertionError(request.full_url)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    report = write_html_snapshot(
        b'<html><head><link rel="stylesheet" href="style.css"></head></html>',
        base_url="https://journal.example/article",
        output_path=tmp_path / "article.html",
        timeout_seconds=10,
        user_agent="test",
        max_assets=1,
    )

    assert report["saved"] == 0
    assert report["failed"] == 0
    assert requested_urls == []
    assert 'href="style.css"' in (tmp_path / "article.html").read_text(encoding="utf-8")


def test_fetch_html_source_rejects_title_mismatch(monkeypatch: Any, tmp_path: Path) -> None:
    def fake_urlopen(_request: Any, timeout: int) -> FakeResponse:
        return FakeResponse(
            url="https://journal.example/article",
            content_type="text/html; charset=utf-8",
            body=b"<!doctype html><html><head><title>Client Challenge</title></head><body>Checking your browser</body></html>",
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    location = FullTextLocation(source="crossref", url="https://journal.example/article", kind="html")

    result = fetch_html_source(
        location,
        output_dir=tmp_path,
        expected_title="Object recognition and localization enhancement in visual prostheses",
    )

    assert not result.ok
    assert result.status == "challenge_page"
    assert list(tmp_path.iterdir()) == []


def test_assess_article_html_rejects_metadata_landing_page() -> None:
    body = (
        "<html><head><title>Article - repository</title>"
        "<meta name=\"citation_title\" content=\"Article\"></head>"
        "<body><h1>Article</h1><p>Abstract</p>"
        f"<p>{'short metadata page ' * 100}</p></body></html>"
    ).encode("utf-8")

    result = assess_article_html(body, expected_title="Article")

    assert not result.ok
    assert result.reason == "short_text"


def test_assess_article_html_accepts_arxiv_latex_profile() -> None:
    body = (
        "<html><head><title>Article</title></head>"
        "<body><main class=\"ltx_document arxiv-html\">"
        "<section class=\"ltx_abstract\">Abstract</section>"
        f"<section>{'Introduction methods results discussion. ' * 180}</section>"
        "<section class=\"ltx_bibliography\">References</section>"
        "</main></body></html>"
    ).encode("utf-8")

    result = assess_article_html(body, expected_title="Article", profile="arxiv")

    assert result.ok
    assert "arxiv_ltx_document" in result.markers


def test_assess_article_html_rejects_subscription_preview_page() -> None:
    body = (
        "<html><head><title>Simulations of Prosthetic Vision</title>"
        "<meta name=\"citation_title\" content=\"Simulations of Prosthetic Vision\">"
        "<script type=\"application/ld+json\">{\"@type\":\"ScholarlyArticle\"}</script></head>"
        "<body><article class=\"article-body\"><h1>Simulations of Prosthetic Vision</h1>"
        "<section>Abstract</section>"
        f"<p>{'Methods results discussion conclusion prosthetic vision. ' * 240}</p>"
        "<p>This is a preview of subscription content, log in via an institution to check access.</p>"
        "<h2>Access this chapter</h2><p>Subscribe and save. Buy Chapter.</p>"
        "<section>References</section></article></body></html>"
    ).encode("utf-8")

    result = assess_article_html(body, expected_title="Simulations of Prosthetic Vision")

    assert not result.ok
    assert result.reason == "limited_access_preview"


def test_fetch_html_source_rejects_arxiv_abs_landing(monkeypatch: Any, tmp_path: Path) -> None:
    def fake_urlopen(_request: Any, timeout: int) -> FakeResponse:
        return FakeResponse(
            url="https://arxiv.org/abs/2602.09735",
            content_type="text/html; charset=utf-8",
            body=(
                "<html><head><title>[2602.09735] Article</title>"
                "<meta name=\"citation_title\" content=\"Article\"></head>"
                "<body><h1>Article</h1><p>Abstract</p>"
                "<a href=\"/html/2602.09735\">HTML</a><a href=\"#references\">References</a>"
                f"<p>{'metadata and announcement page text. ' * 240}</p>"
                "</body></html>"
            ).encode("utf-8"),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    location = FullTextLocation(source="zotero_translation_server", url="https://arxiv.org/abs/2602.09735", kind="landing")

    result = fetch_html_source(location, output_dir=tmp_path, timeout_seconds=10, expected_title="Article")

    assert not result.ok
    assert result.status == "arxiv_abs_landing"
    assert result.article["text_chars"] > 4_000
    assert list(tmp_path.iterdir()) == []


def test_download_html_sources_falls_back_to_ar5iv_for_arxiv_abs(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    requested_urls: list[str] = []

    def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
        requested_urls.append(request.full_url)
        if request.full_url == "https://arxiv.org/html/2502.18864":
            raise urllib.error.HTTPError(request.full_url, 404, "Not found", {}, None)
        if request.full_url == "https://ar5iv.labs.arxiv.org/html/2502.18864":
            return FakeResponse(
                url="https://ar5iv.labs.arxiv.org/html/2502.18864",
                content_type="text/html; charset=utf-8",
                body=(
                    "<html><head><title>[2502.18864] Article</title></head>"
                    "<body><main class=\"ltx_document arxiv-html\">"
                    "<section class=\"ltx_abstract\">Abstract</section>"
                    f"<section>{'Introduction methods results discussion. ' * 220}</section>"
                    "<section class=\"ltx_bibliography\">References</section>"
                    "</main></body></html>"
                ).encode("utf-8"),
            )
        raise AssertionError(request.full_url)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    results = download_html_sources(
        [
            FullTextLocation(source="zotero_translation_server", url="https://arxiv.org/abs/2502.18864", kind="landing"),
        ],
        output_dir=tmp_path,
        limit=2,
        expected_title="Article",
        stop_after_first_ok=True,
    )

    assert [result.url for result in results] == [
        "https://arxiv.org/html/2502.18864",
        "https://ar5iv.labs.arxiv.org/html/2502.18864",
    ]
    assert requested_urls == [
        "https://arxiv.org/html/2502.18864",
        "https://ar5iv.labs.arxiv.org/html/2502.18864",
    ]
    assert results[0].status == "http_error"
    assert results[1].ok
    assert results[1].source == "ar5iv"


def test_download_html_sources_reclassifies_ojs_article_view(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
        assert request.full_url == "https://journal.example/jour/article/view/335"
        return FakeResponse(
            url="https://journal.example/jour/article/view/335",
            content_type="text/html; charset=utf-8",
            body=(
                "<html><head><title>OJS Article</title><meta name=\"citation_title\" content=\"OJS Article\"></head>"
                "<body><article><h1>OJS Article</h1><section>Abstract</section>"
                f"<section>{'Methods results discussion conclusion. ' * 420}</section>"
                "<section>References</section></article></body></html>"
            ).encode("utf-8"),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    results = download_html_sources(
        [
            FullTextLocation(source="doaj", url="https://journal.example/jour/article/view/335", kind="landing"),
        ],
        output_dir=tmp_path,
        limit=1,
        expected_title="OJS Article",
        stop_after_first_ok=True,
    )

    assert len(results) == 1
    assert results[0].ok
    assert results[0].kind == "html"


def test_fetch_html_source_follows_thieme_abstract_html_link(monkeypatch: Any, tmp_path: Path) -> None:
    requested_urls: list[str] = []

    def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
        requested_urls.append(request.full_url)
        if request.full_url == "https://doi.org/10.1055/s-2008-1027636":
            return FakeResponse(
                url="https://www.thieme-connect.de/products/ejournals/abstract/10.1055/s-2008-1027636",
                content_type="text/html; charset=utf-8",
                body=(
                    "<html><head><title>Thieme / Abstract</title></head><body>"
                    "<p>Abstract only.</p>"
                    "<a href=\"/products/ejournals/html/10.1055/s-2008-1027636\">Full text HTML</a>"
                    "</body></html>"
                ).encode("utf-8"),
            )
        if request.full_url == "https://www.thieme-connect.de/products/ejournals/html/10.1055/s-2008-1027636":
            return FakeResponse(
                url="https://www.thieme-connect.de/products/ejournals/html/10.1055/s-2008-1027636",
                content_type="text/html; charset=utf-8",
                body=(
                    "<html><head><title>Article</title></head><body>"
                    "<article><h1>Article</h1><section>Abstract</section>"
                    f"<section>{'Methods results discussion conclusion. ' * 450}</section>"
                    "<section>References</section></article></body></html>"
                ).encode("utf-8"),
            )
        raise AssertionError(request.full_url)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = fetch_html_source(
        FullTextLocation(source="crossref", url="https://doi.org/10.1055/s-2008-1027636", kind="doi"),
        output_dir=tmp_path,
        timeout_seconds=10,
        expected_title="Article",
    )

    assert result.ok
    assert result.kind == "html"
    assert result.final_url.endswith("/products/ejournals/html/10.1055/s-2008-1027636")
    assert requested_urls == [
        "https://doi.org/10.1055/s-2008-1027636",
        "https://www.thieme-connect.de/products/ejournals/html/10.1055/s-2008-1027636",
    ]


def test_fetch_html_source_rejects_tandfonline_get_access(monkeypatch: Any, tmp_path: Path) -> None:
    def fake_urlopen(_request: Any, timeout: int) -> FakeResponse:
        return FakeResponse(
            url="https://www.tandfonline.com/doi/full/10.1080/example",
            content_type="text/html; charset=utf-8",
            body=(
                "<html><head><title>Article: Journal: Vol 1 - Get Access</title></head><body>"
                "<article><h1>Article</h1><section>Abstract</section>"
                f"<section>{'Methods results discussion conclusion. ' * 450}</section>"
                "<section>References</section></article></body></html>"
            ).encode("utf-8"),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = fetch_html_source(
        FullTextLocation(source="crossref", url="https://doi.org/10.1080/example", kind="landing"),
        output_dir=tmp_path,
        timeout_seconds=10,
        expected_title="Article",
    )

    assert not result.ok
    assert result.kind == "html"
    assert result.status == "access_landing"
    assert list(tmp_path.iterdir()) == []


def test_fetch_html_source_reclassifies_frontiers_full_article(monkeypatch: Any, tmp_path: Path) -> None:
    def fake_urlopen(_request: Any, timeout: int) -> FakeResponse:
        return FakeResponse(
            url="https://www.frontiersin.org/journals/psychology/articles/10.3389/fpsyg.2017.00882/full",
            content_type="text/html; charset=utf-8",
            body=(
                "<html><head><title>Article</title></head><body>"
                "<article><h1>Article</h1><section>Abstract</section>"
                f"<section>{'Methods results discussion conclusion. ' * 450}</section>"
                "<section>References</section></article></body></html>"
            ).encode("utf-8"),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = fetch_html_source(
        FullTextLocation(source="crossref", url="https://doi.org/10.3389/fpsyg.2017.00882", kind="landing"),
        output_dir=tmp_path,
        timeout_seconds=10,
        expected_title="Article",
    )

    assert result.ok
    assert result.kind == "html"
    assert result.status == "downloaded"


def test_fetch_html_source_derives_pdf_from_citation_pdf_url(monkeypatch: Any, tmp_path: Path) -> None:
    def fake_urlopen(_request: Any, timeout: int) -> FakeResponse:
        return FakeResponse(
            url="https://pmc.ncbi.nlm.nih.gov/articles/PMC12013345/",
            content_type="text/html; charset=utf-8",
            body=(
                "<html><head><title>PMC Article</title>"
                "<meta name=\"citation_pdf_url\" content=\"pdf/nihms-2072483.pdf\">"
                "<meta name=\"citation_pdf_url\" content=\"pdf/article-supplement.pdf\"></head>"
                "<body><article class=\"pmc-article\"><h1>PMC Article</h1>"
                "<section>Abstract</section>"
                f"<section>{'Methods results discussion conclusion. ' * 260}</section>"
                "<section>References</section></article></body></html>"
            ).encode("utf-8"),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = fetch_html_source(
        FullTextLocation(
            source="europe_pmc",
            url="https://pmc.ncbi.nlm.nih.gov/articles/PMC12013345/",
            kind="html",
            repository="PMC",
        ),
        output_dir=tmp_path,
        timeout_seconds=10,
        expected_title="PMC Article",
    )

    assert result.ok
    assert [location.url for location in result.derived_pdf_locations] == [
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC12013345/pdf/nihms-2072483.pdf"
    ]
    assert result.derived_pdf_locations[0].raw["derivation"] == "citation_pdf_url"


def test_fetch_html_source_reclassifies_iop_article_and_derives_pdf(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    def fake_urlopen(_request: Any, timeout: int) -> FakeResponse:
        return FakeResponse(
            url="https://iopscience.iop.org/article/10.1088/1741-2552/ade918",
            content_type="text/html; charset=utf-8",
            body=(
                "<html><head><title>IOP Article</title></head><body>"
                "<article class=\"article-body\"><h1>IOP Article</h1>"
                "<section>Abstract</section>"
                f"<section>{'Methods results discussion conclusion. ' * 260}</section>"
                "<section>References</section></article></body></html>"
            ).encode("utf-8"),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = fetch_html_source(
        FullTextLocation(
            source="crossref",
            url="https://iopscience.iop.org/article/10.1088/1741-2552/ade918",
            kind="landing",
        ),
        output_dir=tmp_path,
        timeout_seconds=10,
        expected_title="IOP Article",
    )

    assert result.ok
    assert result.kind == "html"
    assert [location.url for location in result.derived_pdf_locations] == [
        "https://iopscience.iop.org/article/10.1088/1741-2552/ade918/pdf"
    ]
    assert result.derived_pdf_locations[0].raw["derivation"] == "iop_article_pdf"


def test_fetch_html_source_rejects_springer_chapter_landing(monkeypatch: Any, tmp_path: Path) -> None:
    def fake_urlopen(_request: Any, timeout: int) -> FakeResponse:
        return FakeResponse(
            url="https://link.springer.com/chapter/10.1007/978-1-4419-0754-7_16",
            content_type="text/html; charset=utf-8",
            body=(
                "<html><head><title>Article</title></head><body>"
                "<article><h1>Article</h1><section>Abstract</section>"
                f"<section>{'Methods results discussion conclusion. ' * 450}</section>"
                "<section>References</section></article></body></html>"
            ).encode("utf-8"),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = fetch_html_source(
        FullTextLocation(source="crossref", url="https://doi.org/10.1007/example", kind="landing"),
        output_dir=tmp_path,
        timeout_seconds=10,
        expected_title="Article",
    )

    assert not result.ok
    assert result.kind == "landing"
    assert result.status == "publisher_landing"


def test_fetch_html_source_does_not_use_pmc_profile_for_non_pmc_europe_pmc_url(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    def fake_urlopen(_request: Any, timeout: int) -> FakeResponse:
        return FakeResponse(
            url="https://link.springer.com/article/10.1007/BF02442682",
            content_type="text/html; charset=utf-8",
            body=(
                "<html><head><title>Article</title></head><body>"
                "<article class=\"article-body\"><h1>Article</h1>"
                "<section>References</section>"
                f"<p>{'short springer article text. ' * 230}</p>"
                "</article></body></html>"
            ).encode("utf-8"),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = fetch_html_source(
        FullTextLocation(
            source="europe_pmc",
            url="https://doi.org/10.1007/BF02442682",
            kind="html",
        ),
        output_dir=tmp_path,
        timeout_seconds=10,
        expected_title="Article",
    )

    assert not result.ok
    assert result.status == "short_text"


def test_download_html_sources_skips_pdf_locations(tmp_path: Path) -> None:
    results = download_html_sources(
        [
            FullTextLocation(source="unpaywall", url="https://repo.example/paper.pdf", kind="pdf"),
        ],
        output_dir=tmp_path,
    )

    assert results[0].status == "skipped_pdf"
    assert list(tmp_path.iterdir()) == []


def test_download_html_sources_prioritizes_direct_arxiv_html(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
        assert request.full_url == "https://arxiv.org/html/2602.09735"
        return FakeResponse(
            url="https://arxiv.org/html/2602.09735",
            content_type="text/html; charset=utf-8",
            body=(
                "<html><head><title>Article</title></head>"
                "<body><main class=\"ltx_document arxiv-html\">"
                "<section class=\"ltx_abstract\">Abstract</section>"
                f"<section>{'Introduction methods results discussion. ' * 220}</section>"
                "<section class=\"ltx_bibliography\">References</section>"
                "</main></body></html>"
            ).encode("utf-8"),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    results = download_html_sources(
        [
            FullTextLocation(source="zotero_translation_server", url="https://arxiv.org/abs/2602.09735", kind="landing"),
            FullTextLocation(source="arxiv", url="https://arxiv.org/html/2602.09735", kind="html"),
        ],
        output_dir=tmp_path,
        limit=1,
        expected_title="Article",
    )

    assert results[0].ok is True
    assert results[0].source == "arxiv"
    assert results[0].kind == "html"
    assert results[1].status == "skipped_limit"


def test_download_html_sources_can_stop_after_first_valid_html(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
        if request.full_url != "https://example.org/a":
            raise AssertionError(f"unexpected fetch: {request.full_url}")
        return FakeResponse(
            url="https://example.org/a",
            content_type="text/html; charset=utf-8",
            body=(
                "<html><head><title>Article</title></head><body>"
                "<article><h1>Article</h1><section>Abstract</section>"
                f"<section>{'methods results discussion. ' * 500}</section>"
                "<section>References</section></article></body></html>"
            ).encode("utf-8"),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    results = download_html_sources(
        [
            FullTextLocation(source="crossref", url="https://example.org/a", kind="html"),
            FullTextLocation(source="unpaywall", url="https://example.org/b", kind="html"),
        ],
        output_dir=tmp_path,
        expected_title="Article",
        stop_after_first_ok=True,
    )

    assert len(results) == 1
    assert results[0].ok is True


def test_fetch_html_source_rejects_unsafe_url(tmp_path: Path) -> None:
    result = fetch_html_source(
        FullTextLocation(source="bad", url="http://localhost/article", kind="html"),
        output_dir=tmp_path,
    )

    assert not result.ok
    assert result.status == "unsafe_url"
    assert list(tmp_path.iterdir()) == []


def test_parse_srcset_keeps_iiif_commas_inside_url() -> None:
    value = (
        "https://iiif.example/lax:1/full/617,/0/default.jpg 617w, "
        "https://iiif.example/lax:1/full/1234,/0/default.jpg 1234w"
    )

    assert parse_srcset(value) == [
        "https://iiif.example/lax:1/full/617,/0/default.jpg",
        "https://iiif.example/lax:1/full/1234,/0/default.jpg",
    ]


def test_resource_parser_keeps_article_figures_and_drops_ui_or_active_assets() -> None:
    parser = ResourceReferenceParser()
    parser.feed(
        """
        <html>
        <head>
          <script src="/runtime.js"></script>
          <link rel="stylesheet" href="/site.css">
          <link rel="icon" href="/favicon.ico">
          <link rel="preload" as="font" href="/font.woff2">
        </head><body>
          <img class="site-logo" src="/static/logo.svg">
          <img alt="Crossmark" src="/crossmark.gif">
          <img width="1" height="1" src="/analytics.gif">
          <img src="/static/img/launch.svg">
          <img role="presentation" src="/decorative.png">
          <img hidden src="/hidden-ad.png">
          <img width="1" height="100" src="/tracking-strip.png">
          <figure><img src="/pmc/blobs/article/Fig1_HTML.jpg"></figure>
          <picture><source srcset="/pmc/blobs/article/Fig2.webp 1x"></picture>
          <figure><img src="/figures/method-diagram.svg"></figure>
          <figure><img src="https://brand.example/figures/brand-effect.png"
                       alt="Figure showing brand trust"></figure>
          <video poster="/video-poster.jpg"><source src="/supplement.mp4"></video>
        </body></html>
        """
    )

    assert parser.resource_urls == [
        "/pmc/blobs/article/Fig1_HTML.jpg",
        "/pmc/blobs/article/Fig2.webp",
        "https://brand.example/figures/brand-effect.png",
        "/figures/method-diagram.svg",
    ]


def test_snapshot_downloads_only_article_image_candidates(monkeypatch: Any, tmp_path: Path) -> None:
    requested_urls: list[str] = []

    def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
        requested_urls.append(request.full_url)
        return FakeResponse(
            url=request.full_url,
            content_type="image/jpeg",
            body=b"figure",
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    output = tmp_path / "article.html"
    report = write_html_snapshot(
        b"""
        <html><head>
          <script src="runtime.js"></script>
          <link rel="stylesheet" href="site.css">
          <link rel="icon" href="favicon.ico">
        </head><body>
          <img class="site-logo" src="logo.svg">
          <img src="figures/Figure1.jpg">
          <video poster="poster.jpg"><source src="supplement.mp4"></video>
        </body></html>
        """,
        base_url="https://journal.example/article/",
        output_path=output,
        timeout_seconds=10,
        user_agent="test",
    )

    assert requested_urls == ["https://journal.example/article/figures/Figure1.jpg"]
    assert report["saved"] == 1
    saved = output.read_text(encoding="utf-8")
    assert "article_assets/001." in saved
    assert 'src="runtime.js"' in saved
    assert 'href="site.css"' in saved
    assert 'poster="poster.jpg"' in saved
