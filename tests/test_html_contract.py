from __future__ import annotations

from pathlib import Path

import pytest

from zoteropdf2md.html_contract import (
    CANONICAL_HTML_PROFILE,
    canonical_contract_report,
    normalize_canonical_html,
)
from zoteropdf2md.web_html_polish import (
    polish_web_html_document,
    polish_web_html_file,
)


@pytest.mark.parametrize(
    ("document_kind", "root_id", "provenance_kind"),
    [
        ("source", "web-doc", "publisher_article"),
        ("pdf", "marker-doc", "pdf"),
    ],
)
def test_normalize_canonical_html_is_idempotent_for_source_and_pdf(
    document_kind: str,
    root_id: str,
    provenance_kind: str,
) -> None:
    raw = f"""
    <html><body><main id="{root_id}">
      <section><h2>Methods</h2>
        <section id="B2"><p>Reference-shaped section.</p></section>
        <figure><figcaption>Figure one.</figcaption></figure>
      </section>
      <ol><li id="ref-1">Reference one.</li></ol>
      <a href="#ref-1">citation</a>
    </main></body></html>
    """

    normalized = normalize_canonical_html(
        raw,
        document_kind=document_kind,
        provenance_kind=provenance_kind,
    )
    repeated = normalize_canonical_html(
        normalized,
        document_kind=document_kind,
        provenance_kind=provenance_kind,
    )
    report = canonical_contract_report(normalized)

    assert repeated == normalized
    assert f'data-z2m-profile="{CANONICAL_HTML_PROFILE}"' in normalized
    assert f'data-z2m-document-kind="{document_kind}"' in normalized
    assert 'id="sec-1"' in normalized
    assert 'id="fig-1"' in normalized
    assert 'id="B2" data-z2m-node-kind="section reference"' in normalized
    assert report["status"] == "passed"
    assert report["document_kind"] == document_kind
    assert report["provenance_kind"] == provenance_kind
    assert report["semantics"] == {
        "section_total": 2,
        "section_annotated": 2,
        "figure_total": 1,
        "figure_annotated": 1,
        "reference_total": 3,
        "reference_annotated": 3,
    }


def test_contract_ignores_tag_like_text_in_comments_and_styles() -> None:
    raw = """
    <html><head><style>.sample::after { content: 'id="ghost" <section>'; }</style></head>
    <body><!-- <figure id="ghost"><section></section></figure> -->
    <main id="web-doc"><section id="real"></section></main></body></html>
    """

    normalized = normalize_canonical_html(
        raw,
        document_kind="source",
        provenance_kind="publisher",
    )
    report = canonical_contract_report(normalized)

    assert "<section>" in normalized
    assert '<figure id="ghost"><section></section></figure>' in normalized
    assert report["status"] == "passed"
    assert report["duplicate_ids"] == []
    assert report["semantics"]["section_total"] == 1


def test_contract_rejects_document_kind_root_mismatch() -> None:
    normalized = normalize_canonical_html(
        '<main id="web-doc"></main>', document_kind="source", provenance_kind="publisher"
    ).replace('id="web-doc"', 'id="marker-doc"')

    assert "document_root_id_mismatch" in canonical_contract_report(normalized)["failures"]


def test_canonical_contract_report_rejects_duplicate_ids_and_multiple_roots() -> None:
    normalized = normalize_canonical_html(
        '<main id="web-doc"><section id="dup"></section><div id="dup"></div></main>',
        document_kind="source",
        provenance_kind="publisher",
    )
    duplicate_report = canonical_contract_report(normalized)
    multiple_roots = normalized.replace(
        "</main>",
        '<main data-z2m-document-root="1"></main></main>',
    )
    multiple_report = canonical_contract_report(multiple_roots)

    assert duplicate_report["status"] == "failed"
    assert duplicate_report["duplicate_ids"] == ["dup"]
    assert "duplicate_ids" in duplicate_report["failures"]
    assert multiple_report["status"] == "failed"
    assert "document_root_count" in multiple_report["failures"]


@pytest.mark.parametrize(
    ("document_kind", "provenance_kind"),
    [("epub", "publisher"), ("source", "   ")],
)
def test_normalize_canonical_html_rejects_invalid_identity(
    document_kind: str,
    provenance_kind: str,
) -> None:
    with pytest.raises(ValueError):
        normalize_canonical_html(
            '<main id="web-doc"></main>',
            document_kind=document_kind,
            provenance_kind=provenance_kind,
        )


def test_normalize_canonical_html_rejects_data_id_spoof() -> None:
    with pytest.raises(ValueError, match="found 0"):
        normalize_canonical_html(
            '<main data-id="web-doc"></main>',
            document_kind="source",
            provenance_kind="publisher",
        )


def test_attribute_reader_does_not_treat_data_role_as_reference_role() -> None:
    normalized = normalize_canonical_html(
        '<main id="web-doc"><p data-role="doc-biblioref">Text</p></main>',
        document_kind="source",
        provenance_kind="publisher",
    )

    assert canonical_contract_report(normalized)["semantics"]["reference_total"] == 0


def test_polish_web_html_document_is_byte_idempotent() -> None:
    raw = f"""
    <html><head><title>Stable Article</title></head><body>
      <article id="article"><h1>Stable Article</h1>
        <section><h2>Results</h2><p>{"Article evidence. " * 600}</p></section>
        <figure><figcaption>Figure one.</figcaption></figure>
        <ol><li id="B2">Reference two.</li></ol>
        <p><a href="#B2">Reference two</a></p>
      </article>
    </body></html>
    """

    first = polish_web_html_document(raw)
    second = polish_web_html_document(first.html)

    assert second.kind == first.kind
    assert second.article_selector == first.article_selector
    assert second.html == first.html


def test_repolish_preserves_canonical_source_provenance() -> None:
    raw = f"""
    <html><head><title>Provenance Article</title></head><body><article>
      <h1>Provenance Article</h1><p>{"Evidence. " * 600}</p>
    </article></body></html>
    """
    polished = polish_web_html_document(raw).html
    polished = polished.replace(
        'data-z2m-source-kind="generic_article"',
        'data-z2m-source-kind="iop_article"',
    ).replace(
        'data-z2m-provenance-kind="generic_article"',
        'data-z2m-provenance-kind="iop_article"',
    ).replace(
        'data-z2m-article-selector="article"',
        'data-z2m-article-selector=".original-article"',
    )

    repeated = polish_web_html_document(polished)

    assert repeated.kind.value == "iop_article"
    assert repeated.article_selector == ".original-article"
    assert repeated.html == polished


def test_polish_web_html_file_is_idempotent_after_local_image_inline(tmp_path: Path) -> None:
    source = tmp_path / "article.html"
    (tmp_path / "figure.png").write_bytes(b"\x89PNG\r\n\x1a\narticle-figure")
    source.write_text(
        f"""
        <html><head><title>Image Article</title></head><body><article>
          <h1>Image Article</h1><p>{"Evidence. " * 600}</p>
          <figure><img src="figure.png"><figcaption>Figure one.</figcaption></figure>
        </article></body></html>
        """,
        encoding="utf-8",
    )

    first = polish_web_html_file(source)
    source.write_text(first.html, encoding="utf-8")
    second = polish_web_html_file(source)

    assert first.inlined_images == 1
    assert second.inlined_images == 0
    assert second.html == first.html
    assert canonical_contract_report(second.html)["status"] == "passed"


def test_spoofed_canonical_profile_does_not_bypass_source_sanitizer() -> None:
    spoofed = f"""
    <html><head><title>Spoofed Article</title></head><body>
      <main id="web-doc" data-z2m-document-root="1"
            data-z2m-profile="{CANONICAL_HTML_PROFILE}"
            data-z2m-profile-version="1" data-z2m-document-kind="source"
            data-z2m-provenance-kind="spoofed">
        <article><h1>Spoofed Article</h1><p>{"Evidence. " * 600}</p>
          <script>alert('unsafe')</script>
          <section onclick="alert('unsafe')"><p>Results.</p></section>
        </article>
      </main>
    </body></html>
    """

    result = polish_web_html_document(spoofed)

    assert "<script" not in result.html
    assert "onclick=" not in result.html
    assert result.html.count('data-z2m-document-root="1"') == 1
    assert canonical_contract_report(result.html)["status"] == "passed"


def test_contract_rejects_duplicate_attributes() -> None:
    normalized = normalize_canonical_html(
        '<main id="web-doc"><section id="one" id="two"></section></main>',
        document_kind="source",
        provenance_kind="publisher",
    )
    report = canonical_contract_report(normalized)

    assert "duplicate_attributes" in report["failures"]
    assert report["duplicate_attributes"] == ["section:id"]
