import base64
from pathlib import Path

import pytest

from zoteropdf2md.html_images import to_data_url, validate_data_url
from zoteropdf2md.html_links import (
    canonicalize_same_document_links as canonicalize_links_from_shared_module,
    count_same_document_absolute_fragment_links as count_links_from_shared_module,
)
from zoteropdf2md.html_theme import web_readability_style
from zoteropdf2md.web_polish.core import (
    WebHtmlKind as CoreWebHtmlKind,
    WebHtmlPolishError as CoreWebHtmlPolishError,
)
from zoteropdf2md.web_polish.registry import (
    default_origin_for_kind,
    handler_for_kind,
    registered_web_polish_handlers,
)
from zoteropdf2md.web_html_polish import (
    WebHtmlKind,
    WebHtmlPolishError,
    absolutize_root_relative_urls,
    canonicalize_same_document_links,
    count_same_document_absolute_fragment_links,
    detect_web_html_kind,
    inline_local_images_from_web_html_document,
    inline_remote_images_from_web_html_document,
    polish_web_html_file,
    polish_web_html_document,
    require_web_article_html,
)


KOSMOS_LIKE_HTML = """
<!doctype html>
<html>
<head>
  <!-- Generated on arXiv by LaTeXML -->
  <title>1 Introduction</title>
</head>
<body>
  <div class="ltx_page_main">
    <section id="S1">
      <h2 class="ltx_title ltx_title_section">Introduction</h2>
      <p class="ltx_p">
        See <a class="ltx_ref" href="https://arxiv.org/html/2511.02824v2#S1">Section 1</a>
        and cite
        <cite class="ltx_cite">[<a class="ltx_ref" href="https://arxiv.org/html/2511.02824v2#bib.bib56">56</a>]</cite>.
        Code is at <a class="ltx_ref ltx_href" href="https://github.com/EdisonScientific/kosmos-figures">GitHub</a>.
        Reports are at <a class="ltx_ref" href="https://platform.edisonscientific.com/trajectories/example">Edison</a>.
      </p>
    </section>
    <section class="ltx_bibliography" id="bib">
      <ol><li id="bib.bib56">MendelianRandomization R package.</li></ol>
    </section>
  </div>
</body>
</html>
"""

LONG_PARAGRAPH = (
    "This article paragraph contains enough scientific prose to exercise the "
    "article extractor without falling back to the whole body. "
)

PNG_BYTES = b"\x89PNG\r\n\x1a\nz2m-test-image"


def test_web_html_polish_reexports_core_types() -> None:
    assert WebHtmlKind is CoreWebHtmlKind
    assert WebHtmlPolishError is CoreWebHtmlPolishError


def test_web_polish_registry_covers_publisher_handlers() -> None:
    registered_kinds = {handler.kind for handler in registered_web_polish_handlers()}

    assert WebHtmlKind.ARXIV_LATEXML in registered_kinds
    assert WebHtmlKind.PMC_ARTICLE in registered_kinds
    assert WebHtmlKind.IOP_ARTICLE in registered_kinds
    assert default_origin_for_kind(WebHtmlKind.SPRINGER_NATURE_ARTICLE) == "https://link.springer.com/"
    assert default_origin_for_kind(WebHtmlKind.IOP_ARTICLE) == "https://iopscience.iop.org/"

    arxiv_handler = handler_for_kind(WebHtmlKind.ARXIV_LATEXML)
    assert arxiv_handler is not None
    assert arxiv_handler.module_name == "arxiv"


def test_web_polish_registry_rejects_known_landing_pages() -> None:
    abs_html = """
    <html><head><meta name="citation_arxiv_id" content="2511.02824"></head>
    <body><a class="abs-button" href="https://arxiv.org/html/2511.02824v2">HTML (experimental)</a></body></html>
    """

    with pytest.raises(WebHtmlPolishError, match="/html/ attachment"):
        require_web_article_html(abs_html)


def test_detect_web_html_kind_distinguishes_arxiv_latexml_from_abs_page() -> None:
    abs_html = """
    <html><head><meta name="citation_arxiv_id" content="2511.02824"></head>
    <body><div class="extra-services"><a class="abs-button" id="latexml-download-link"
    href="https://arxiv.org/html/2511.02824v2">HTML (experimental)</a></div></body></html>
    """

    assert detect_web_html_kind(KOSMOS_LIKE_HTML) == WebHtmlKind.ARXIV_LATEXML
    assert detect_web_html_kind(abs_html) == WebHtmlKind.ARXIV_ABS_PAGE
    assert (
        detect_web_html_kind("<html></html>", source_url="https://arxiv.org/abs/2511.02824")
        == WebHtmlKind.ARXIV_ABS_PAGE
    )


def test_detect_web_html_kind_accepts_iop_full_article_not_iop_assets() -> None:
    assert (
        detect_web_html_kind("<html></html>", source_url="https://iopscience.iop.org/article/10.1088/1741-2552/ade918")
        == WebHtmlKind.IOP_ARTICLE
    )
    assert (
        detect_web_html_kind("<html></html>", source_url="https://iopscience.iop.org/article/10.1088/1741-2552/ade918/meta")
        == WebHtmlKind.UNKNOWN
    )

    html = f"""
    <html>
      <head><meta name="citation_publisher" content="IOP Publishing"></head>
      <body><div class="article-content"><div class="wd-jnl-art-full-text"><p>{" ".join([LONG_PARAGRAPH] * 20)}</p></div></div></body>
    </html>
    """

    assert detect_web_html_kind(html) == WebHtmlKind.IOP_ARTICLE


def test_polish_web_html_document_rejects_arxiv_abs_page() -> None:
    abs_html = """
    <html><head><meta name="citation_arxiv_id" content="2511.02824"></head>
    <body><a class="abs-button" href="https://arxiv.org/html/2511.02824v2">HTML (experimental)</a></body></html>
    """

    with pytest.raises(WebHtmlPolishError):
        polish_web_html_document(abs_html, source_url="https://arxiv.org/abs/2511.02824")


def test_canonicalize_same_document_links_rewrites_only_self_fragments() -> None:
    result = canonicalize_same_document_links(
        KOSMOS_LIKE_HTML,
        source_url="https://arxiv.org/html/2511.02824v2",
    )

    assert result.rewritten_count == 2
    assert result.unresolved_count == 0
    assert 'href="#S1"' in result.html
    assert 'href="#bib.bib56"' in result.html
    assert 'href="https://github.com/EdisonScientific/kosmos-figures"' in result.html
    assert 'href="https://platform.edisonscientific.com/trajectories/example"' in result.html
    assert count_same_document_absolute_fragment_links(
        result.html,
        source_url="https://arxiv.org/html/2511.02824v2",
    ) == 0


def test_canonicalize_same_document_links_allows_versionless_arxiv_source_url() -> None:
    result = canonicalize_same_document_links(
        KOSMOS_LIKE_HTML,
        source_url="https://arxiv.org/html/2511.02824",
    )

    assert result.rewritten_count == 2
    assert 'href="#S1"' in result.html
    assert 'href="#bib.bib56"' in result.html


def test_same_document_link_helpers_are_source_agnostic() -> None:
    html = """
    <html><body>
      <article>
        <section id="sec1"></section>
        <a href="https://example.org/article#sec1">same</a>
        <a href="https://example.org/other#sec1">external</a>
      </article>
    </body></html>
    """

    result = canonicalize_links_from_shared_module(html, source_url="https://example.org/article")

    assert 'href="#sec1"' in result.html
    assert 'href="https://example.org/other#sec1"' in result.html
    assert count_links_from_shared_module(result.html, source_url="https://example.org/article") == 0


def test_canonicalize_same_document_links_preserves_unresolved_self_fragments() -> None:
    html = (
        '<html><body><section id="S1"></section>'
        '<a href="https://arxiv.org/html/2511.02824v2#missing">missing</a>'
        "</body></html>"
    )

    result = canonicalize_same_document_links(
        html,
        source_url="https://arxiv.org/html/2511.02824v2",
    )

    assert result.rewritten_count == 0
    assert result.unresolved_count == 1
    assert 'href="https://arxiv.org/html/2511.02824v2#missing"' in result.html


def test_polish_web_html_document_extracts_arxiv_latexml_article() -> None:
    html = f"""
    <html>
      <head><title>Kosmos</title></head>
      <body>
        <header>site chrome should disappear</header>
        <script>self.__next_f.push(["not article"])</script>
        <div class="ltx_page_main">
          <section id="S1">
            <h1 class="ltx_title_document">Kosmos</h1>
            <p>{" ".join([LONG_PARAGRAPH] * 18)}</p>
            <a href="https://arxiv.org/html/2511.02824v2#S1">Section</a>
          </section>
          <section class="ltx_bibliography" id="bib">
            <ol><li id="bib.bib1">Reference</li></ol>
          </section>
        </div>
        <footer>publisher footer should disappear</footer>
      </body>
    </html>
    """

    result = polish_web_html_document(html, source_url="https://arxiv.org/html/2511.02824v2")

    assert result.kind == WebHtmlKind.ARXIV_LATEXML
    assert result.article_extracted is True
    assert result.article_selector == ".ltx_page_main"
    assert 'data-z2m-source-kind="arxiv_latexml"' in result.html
    assert "site chrome should disappear" not in result.html
    assert "publisher footer should disappear" not in result.html
    assert "self.__next_f.push" not in result.html
    assert 'href="#S1"' in result.html
    assert web_readability_style() in result.html
    assert "#web-doc :target" in result.html
    assert "outline: 3px solid" in result.html
    assert "border-top: 1px solid #cbd5e1" in result.html
    assert "counter-reset: z2m-ref" in result.html
    assert "figure.ltx_table .ltx_transformed_inner" in result.html
    assert "figure.ltx_table > figcaption" in result.html
    assert "min-width: 100%" in result.html
    assert ".ltx_caption .ltx_transformed_outer" in result.html
    assert "table.disp-formula td.label" in result.html
    assert "dl.def-list" in result.html
    assert ".off-screen, .sr-only" in result.html


def test_polish_web_html_document_normalizes_latexml_equation_layout() -> None:
    html = f"""
    <html>
      <head><title>Equation Article</title></head>
      <body>
        <div class="ltx_page_main">
          <section id="S1">
            <h1>Equation Article</h1>
            <p>{" ".join([LONG_PARAGRAPH] * 18)}</p>
            <table class="ltx_equationgroup ltx_eqn_align ltx_eqn_table" id="A1.EGx1">
              <tbody><tr class="ltx_equation ltx_eqn_row ltx_align_baseline">
                <td class="ltx_eqn_cell ltx_eqn_center_padleft"></td>
                <td class="ltx_td ltx_align_right ltx_eqn_cell"><math><mi>x</mi><mo>=</mo><mn>1</mn></math></td>
                <td class="ltx_eqn_cell ltx_eqn_center_padright"></td>
                <td class="ltx_eqn_cell ltx_eqn_eqno ltx_align_middle ltx_align_right">(1)</td>
              </tr></tbody>
            </table>
            <table class="ltx_equationgroup ltx_eqn_align ltx_eqn_table" id="A1.EGx2">
              <tbody><tr class="ltx_equation ltx_eqn_row ltx_align_baseline">
                <td class="ltx_eqn_cell ltx_eqn_center_padleft"></td>
                <td class="ltx_td ltx_align_right ltx_eqn_cell"><math><mi>x</mi></math></td>
                <td class="ltx_td ltx_align_left ltx_eqn_cell"><math><mo>=</mo><mn>1</mn></math></td>
                <td class="ltx_eqn_cell ltx_eqn_center_padright"></td>
                <td class="ltx_eqn_cell ltx_eqn_eqno ltx_align_middle ltx_align_right">(2)</td>
              </tr></tbody>
            </table>
          </section>
        </div>
      </body>
    </html>
    """

    result = polish_web_html_document(html, source_url="https://arxiv.org/html/2501.00001")

    assert "table.ltx_equationgroup td" in result.html
    assert "border: 0;" in result.html
    assert "z2m-ltx-single-equation-cell" in result.html
    assert result.html.count('class="ltx_td ltx_align_right ltx_eqn_cell z2m-ltx-single-equation-cell"') == 1
    assert 'class="ltx_td ltx_align_left ltx_eqn_cell z2m-ltx-single-equation-cell"' not in result.html


def test_polish_web_html_document_removes_latexml_table_color_artifacts() -> None:
    html = f"""
    <html>
      <head><title>Table Article</title></head>
      <body>
        <div class="ltx_page_main">
          <section id="S1">
            <h1>Table Article</h1>
            <p>{" ".join([LONG_PARAGRAPH] * 18)}</p>
            <figure class="ltx_table ltx_minipage" style="width:166.9pt;">
              <figcaption class="ltx_caption">Table 1: Results.</figcaption>
              <table class="ltx_tabular">
                <tr><td><span class="ltx_ERROR undefined">\\rowcolor</span>blue!10 BIT End-to-End</td></tr>
                <tr><td>\\rowcolor gray!10 RNN</td></tr>
              </table>
            </figure>
          </section>
        </div>
      </body>
    </html>
    """

    result = polish_web_html_document(html, source_url="https://arxiv.org/html/2511.02824v2")

    assert "\\rowcolor" not in result.html
    assert "blue!10" not in result.html
    assert "gray!10" not in result.html
    assert "BIT End-to-End" in result.html
    assert "RNN" in result.html


def test_polish_web_html_document_styles_latexml_item_markers() -> None:
    html = f"""
    <html>
      <head><title>List Article</title></head>
      <body>
        <div class="ltx_page_main">
          <section id="S1">
            <h1>List Article</h1>
            <p>{" ".join([LONG_PARAGRAPH] * 18)}</p>
            <ul class="ltx_itemize" id="S1.I1">
              <li class="ltx_item" style="list-style-type:none;">
                <span class="ltx_tag ltx_tag_item">•</span>
                <div class="ltx_para"><p class="ltx_p">Visual landmarks are important.</p></div>
              </li>
            </ul>
          </section>
        </div>
      </body>
    </html>
    """

    result = polish_web_html_document(html, source_url="https://arxiv.org/html/2502.10561")

    assert ".ltx_item > .ltx_tag_item" in result.html
    assert "grid-template-columns: 1.35em minmax(0, 1fr)" in result.html
    assert '<span class="ltx_tag ltx_tag_item">•</span>' in result.html
    assert "Visual landmarks are important." in result.html


def test_polish_web_html_document_removes_latexml_black_text_color() -> None:
    html = f"""
    <html>
      <head><title>Black Text Article</title></head>
      <body>
        <div class="ltx_page_main">
          <section id="S1">
            <h1>Black Text Article</h1>
            <p>{" ".join([LONG_PARAGRAPH] * 18)}</p>
            <figure class="ltx_table" id="S1.T1">
              <table class="ltx_tabular">
                <tr>
                  <td>
                    <span class="ltx_rule" style="width:100%;height:0.8pt;color:#000000;background:#000000;display:inline-block;"> </span>
                    <span class="ltx_text ltx_font_bold" style="font-size:90%;color:#000000;">Visual</span>
                  </td>
                </tr>
              </table>
              <figcaption class="ltx_caption">Table 1: Results.</figcaption>
            </figure>
          </section>
        </div>
      </body>
    </html>
    """

    result = polish_web_html_document(html, source_url="https://arxiv.org/html/2502.10561")

    assert '<span class="ltx_text ltx_font_bold" style="font-size:90%;">Visual</span>' in result.html
    assert "ltx_rule" in result.html
    assert "background:#000000" in result.html
    assert "height:0.8pt;color:#000000" in result.html


def test_polish_web_html_document_removes_latexml_black_mathcolor() -> None:
    html = f"""
    <html>
      <head><title>Math Color Article</title></head>
      <body>
        <div class="ltx_page_main">
          <section id="S1">
            <h1>Math Color Article</h1>
            <p>{" ".join([LONG_PARAGRAPH] * 18)}</p>
            <figure class="ltx_table" id="S1.T1">
              <table class="ltx_tabular">
                <tr><td>
                  <math class="ltx_Math" display="inline">
                    <mrow>
                      <mi mathcolor="#000000" mathsize="90%">M</mi>
                      <mo mathcolor="#000" mathsize="90%">=</mo>
                      <mn mathcolor="black">6.09</mn>
                      <mi mathcolor="#ff0000">x</mi>
                    </mrow>
                  </math>
                </td></tr>
              </table>
              <figcaption class="ltx_caption">Table 1: Results.</figcaption>
            </figure>
          </section>
        </div>
      </body>
    </html>
    """

    result = polish_web_html_document(html, source_url="https://arxiv.org/html/2502.10561")

    assert 'mathcolor="#000000"' not in result.html
    assert 'mathcolor="#000"' not in result.html
    assert 'mathcolor="black"' not in result.html
    assert 'mathcolor="#ff0000"' in result.html
    assert '<mi mathsize="90%">M</mi>' in result.html


def test_polish_web_html_document_removes_latexml_description_error_panels() -> None:
    png_base64 = base64.b64encode(PNG_BYTES).decode("ascii")
    html = f"""
    <html>
      <head><title>Description Article</title></head>
      <body>
        <div class="ltx_page_main">
          <section id="S1">
            <h1>Description Article</h1>
            <p>{" ".join([LONG_PARAGRAPH] * 18)}</p>
            <figure class="ltx_figure" id="S1.F1">
              <img class="ltx_graphics" src="data:image/png;base64,{png_base64}" alt="Landmark photo">
              <figcaption class="ltx_caption">Figure 1: Landmark.</figcaption>
              <div class="ltx_flex_figure">
                <div class="ltx_flex_cell ltx_flex_size_1">
                  <span class="ltx_ERROR ltx_figure_panel undefined">\\Description</span>
                </div>
                <div class="ltx_flex_break"></div>
                <div class="ltx_flex_cell ltx_flex_size_1">
                  <p class="ltx_p ltx_figure_panel">Long visual description that should not render under the figure.</p>
                </div>
              </div>
            </figure>
            <figure class="ltx_table" id="S1.T1">
              <figcaption class="ltx_caption">Table 1: Participants.</figcaption>
              <div class="ltx_flex_figure">
                <div class="ltx_flex_cell ltx_flex_size_1">
                  <span class="ltx_ERROR ltx_figure_panel undefined">\\Description</span>
                </div>
                <div class="ltx_flex_break"></div>
                <div class="ltx_flex_cell ltx_flex_size_1">
                  <p class="ltx_p">Table description <span class="ltx_tabular">P01 Active</span></p>
                </div>
              </div>
            </figure>
          </section>
        </div>
      </body>
    </html>
    """

    result = polish_web_html_document(html, source_url="https://arxiv.org/html/2502.10561")

    assert "\\Description" not in result.html
    assert "ltx_ERROR" not in result.html
    assert "Landmark photo" in result.html
    assert "Long visual description" not in result.html
    assert "P01 Active" in result.html


def test_polish_web_html_file_inlines_arxiv_extracted_remote_images(tmp_path, monkeypatch) -> None:
    from zoteropdf2md import web_html_polish as web_polish_module

    html_path = tmp_path / "article.html"
    html_path.write_text(
        f"""
        <html>
          <body>
            <div class="ltx_page_main">
              <section id="S1">
                <h1>arXiv Article</h1>
                <p>{" ".join([LONG_PARAGRAPH] * 20)}</p>
                <figure>
                  <img alt="Figure" src="extracted/6200412/graphs/visimark.png">
                </figure>
              </section>
            </div>
          </body>
        </html>
        """,
        encoding="utf-8",
    )
    fetched: list[str] = []

    def fake_fetch(url: str) -> tuple[bytes, str]:
        fetched.append(url)
        return PNG_BYTES, "image/png"

    monkeypatch.setattr(web_polish_module, "_fetch_remote_image", fake_fetch)

    result = polish_web_html_file(html_path, source_url="https://arxiv.org/html/2502.10561")

    assert fetched == ["https://arxiv.org/html/2502.10561/extracted/6200412/graphs/visimark.png"]
    assert result.inlined_images == 1
    assert 'src="data:image/png;base64,' in result.html
    assert 'data-z2m-src="https://arxiv.org/html/2502.10561/extracted/6200412/graphs/visimark.png"' in result.html


def test_polish_web_html_file_runs_arxiv_source_recovery(tmp_path, monkeypatch) -> None:
    from zoteropdf2md import web_html_polish as web_polish_module
    from zoteropdf2md.arxiv_source_recovery import ArxivSourceRecoveryResult

    html_path = tmp_path / "article.html"
    html_path.write_text(
        f"""
        <html>
          <body>
            <div class="ltx_page_main">
              <section id="S1">
                <h1>arXiv Article</h1>
                <p>{" ".join([LONG_PARAGRAPH] * 20)}</p>
                <figure id="S1.F1">
                  <span class="ltx_ERROR undefined">{{forest}}</span>
                  <figcaption>Figure 1: Taxonomy.</figcaption>
                </figure>
              </section>
            </div>
          </body>
        </html>
        """,
        encoding="utf-8",
    )
    calls: list[str | None] = []

    def fake_recover(html: str, *, source_url: str | None, **kwargs: object) -> ArxivSourceRecoveryResult:
        del kwargs
        calls.append(source_url)
        return ArxivSourceRecoveryResult(
            html=html.replace(
                '<span class="ltx_ERROR undefined">{forest}</span>',
                '<img data-z2m-recovery="arxiv-source" src="data:image/png;base64,AAAA" alt="Recovered">',
            ),
            recovered_figures=1,
            attempted_figures=1,
            errors=(),
        )

    monkeypatch.setattr(web_polish_module, "recover_latexml_figures_from_arxiv_source_html", fake_recover)

    result = polish_web_html_file(html_path, source_url="https://arxiv.org/html/2507.01903")

    assert calls == ["https://arxiv.org/html/2507.01903"]
    assert result.recovered_source_figures == 1
    assert result.attempted_source_figures == 1
    assert result.source_recovery_errors == ()
    assert 'data-z2m-recovery="arxiv-source"' in result.html
    assert "ltx_ERROR" not in result.html


def test_polish_web_html_document_removes_empty_arxiv_missing_image_placeholder() -> None:
    html = f"""
    <html>
      <body>
        <div class="ltx_page_main">
          <section id="S1">
            <h1>arXiv Article</h1>
            <p>{" ".join([LONG_PARAGRAPH] * 20)}</p>
            <figure>
              <img src="" class="ltx_graphics ltx_missing ltx_missing_image" alt="Refer to caption">
              <figcaption>Figure with unavailable source art.</figcaption>
            </figure>
          </section>
        </div>
      </body>
    </html>
    """

    result = polish_web_html_document(html, source_url="https://arxiv.org/html/2101.05452")

    assert 'src=""' not in result.html
    assert "ltx_missing_image" not in result.html
    assert "Figure with unavailable source art." in result.html


def test_polish_web_html_document_removes_empty_table_shells() -> None:
    html = f"""
    <html>
      <body>
        <article>
          <h1>Article</h1>
          <p>{" ".join([LONG_PARAGRAPH] * 20)}</p>
          <table>  </table>
          <table><tr><td>real value</td></tr></table>
        </article>
      </body>
    </html>
    """

    result = polish_web_html_document(html)

    assert "<table>  </table>" not in result.html
    assert "<td>real value</td>" in result.html


def test_polish_web_html_document_absolutizes_root_relative_publisher_urls() -> None:
    html = f"""
    <html>
      <head>
        <title>Publisher Article</title>
        <link rel="canonical" href="https://www.tandfonline.com/doi/full/10.1080/example">
      </head>
      <body>
        <article>
          <h1>Article</h1>
          <p>{" ".join([LONG_PARAGRAPH] * 20)}</p>
          <a href="/action/downloadSupplement?doi=10.1080%2Fexample&amp;file=sm.docx">Supplement</a>
          <img src="/cms/asset/figure.jpg" alt="Figure">
          <picture><source srcset="/cms/asset/figure-small.jpg 1x, /cms/asset/figure-large.jpg 2x"></picture>
        </article>
      </body>
    </html>
    """

    result = polish_web_html_document(html)

    assert 'href="https://www.tandfonline.com/action/downloadSupplement?doi=10.1080%2Fexample&amp;file=sm.docx"' in result.html
    assert 'src="https://www.tandfonline.com/cms/asset/figure.jpg"' in result.html
    assert "https://www.tandfonline.com/cms/asset/figure-small.jpg 1x" in result.html
    assert "https://www.tandfonline.com/cms/asset/figure-large.jpg 2x" in result.html
    assert 'href="/action/' not in result.html
    assert 'src="/cms/' not in result.html


def test_polish_web_html_document_removes_metrics_and_disables_missing_local_fragments() -> None:
    html = f"""
    <html>
      <head><title>Frontiers-like Article</title></head>
      <body>
        <article>
          <div class="ArticleMetrics">
            <p>Article metrics</p>
            <a class="ArticleMetrics__link" href="#metrics">View details</a>
          </div>
          <h1>Article</h1>
          <p>{" ".join([LONG_PARAGRAPH] * 20)}</p>
          <p>
            <a class="ArticleReference" href="#SM1">Supplementary Figure S1</a>
            <a class="ArticleReference" href="#present">real target</a>
          </p>
          <section id="present">A real local target.</section>
        </article>
      </body>
    </html>
    """

    result = polish_web_html_document(html)

    assert "ArticleMetrics" not in result.html
    assert 'href="#metrics"' not in result.html
    assert ' href="#SM1"' not in result.html
    assert 'data-z2m-unresolved-href="#SM1"' in result.html
    assert 'href="#present"' in result.html


def test_polish_web_html_document_unwraps_frontiers_and_links_references() -> None:
    raw_article = f"""
    <main class="ArticleDetailsV4__main">
      <h1>How path integration abilities of blind people change</h1>
      <div class="ArticleContent">
        <p>
          {" ".join([LONG_PARAGRAPH] * 20)}
          Kosslyn and Osherson,
          <button type="button" color="Blue40" id="B24-button" class="ArticleReference" data-event="articleReference-a-b24"> 1995 </button>.
          Another Frontiers variant cites
          <button type="button" color="Blue40" id="ref9-button" class="ArticleReference" data-event="articleReference-a-ref9"> Bourne et al., 2017 </button>.
        </p>
        <button class="ArticleFigure__figureButton" aria-label="Open lightbox for FIGURE 1">
          <figure>
            <figcaption>
              Figure caption cites
              <button type="button" color="Blue40" id="B31-button" class="ArticleReference" data-event="articleReference-a-b31"> Grill-Spector and Weiner, 2014 </button>.
            </figcaption>
          </figure>
        </button>
        <div id="h14">
          <h2>References</h2>
          <ul class="References">
            <li class="References__item" id="B24">
              <div class="References__label"><p>24</p></div>
              <div class="References__content">
                <p class="notranslate">
                  <span class="References__personGroup">
                    <span class="References__name">
                      <span class="References__surname">Kosslyn</span>
                    </span>
                  </span>
                  (1995). Title.
                </p>
              </div>
            </li>
            <li class="References__item" id="ref9">
              <div class="References__label"><p>9</p></div>
              <div class="References__content"><p>Bourne et al. (2017). Title.</p></div>
            </li>
            <li class="References__item" id="B31">
              <div class="References__label"><p>31</p></div>
              <div class="References__content"><p>Grill-Spector and Weiner (2014). Title.</p></div>
            </li>
          </ul>
        </div>
      </div>
    </main>
    """
    html = f"""
    <html>
      <head><title>Previously polished Frontiers article</title></head>
      <body>
        <main id="web-doc" data-z2m-source-kind="unknown">
          <main id="web-doc" data-z2m-source-kind="unknown">
            {raw_article}
          </main>
        </main>
      </body>
    </html>
    """

    result = polish_web_html_document(html)

    assert result.html.count('id="web-doc"') == 1
    assert '<button type="button" color="Blue40"' not in result.html
    assert (
        '<a class="ArticleReference z2m-frontiers-citation" href="#B24" id="B24-button"> 1995 </a>'
        in result.html
    )
    assert (
        '<a class="ArticleReference z2m-frontiers-citation" href="#ref9" id="ref9-button"> Bourne et al., 2017 </a>'
        in result.html
    )
    assert (
        '<a class="ArticleReference z2m-frontiers-citation" href="#B31" id="B31-button"> Grill-Spector and Weiner, 2014 </a>'
        in result.html
    )
    assert 'class="References"' in result.html
    assert ".References__item" in result.html
    assert ".ArticleReference.z2m-frontiers-citation" in result.html
    assert result.unresolved_same_document_links == 0


def test_polish_web_html_document_removes_discovered_js_chrome_and_supplementary_refs() -> None:
    html = f"""
    <html>
      <head><title>Previously polished mixed publisher article</title></head>
      <body>
        <main id="web-doc" data-z2m-source-kind="pmc_article">
          <main id="web-doc" data-z2m-source-kind="pmc_article">
            <article class="pmc-article" id="article">
              <h1>Mixed publisher article</h1>
              <p>
                {" ".join([LONG_PARAGRAPH] * 20)}
                <a class="ArticleReference" data-event="articleReference-a-sm1">Supplementary Material</a>
                <a class="ArticleReference" data-event="articleReference-a-b2">reference two</a>
              </p>
              <button class="ArticleFigure__figureButton" aria-label="Open lightbox for FIGURE 1">
                <figure id="F1">
                  <img alt="Figure 1" src="data:image/png;base64,iVBORw0KGgo="/>
                  <figcaption>Visible figure caption.</figcaption>
                </figure>
              </button>
              <button class="ButtonIcon" data-event="articleFigure-button-download" aria-label="Download FIGURE 1">Download</button>
              <button class="ButtonIcon" data-event="articleFigure-button-openLightbox" aria-label="Expand FIGURE 1">Expand</button>
              <button class="citation-dialog-trigger">Cite</button>
              <li class="pmc-permalink">
                <button aria-label="Show article permalink" type="button">Permalink</button>
                <div class="pmc-permalink__dropdown"><button class="pmc-permalink__dropdown__copy__btn">Copy</button></div>
              </li>
              <div id="collections-action-dialog" class="dialog collections-dialog">
                <div class="collections-action-panel action-panel">
                  <form id="collections-action-dialog-form"><button type="button">Save</button></form>
                </div>
              </div>
              <ul class="d-buttons inline-list">
                <li><button class="d-button" aria-controls="copyright-dialog" type="button">Copyright</button></li>
              </ul>
              <button class="usa-accordion__button" type="button">Accordion</button>
              <section id="B2">Reference two.</section>
            </article>
          </main>
        </main>
      </body>
    </html>
    """

    result = polish_web_html_document(html, source_url="https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/")

    assert result.html.count('id="web-doc"') == 1
    assert "ArticleFigure__figureButton" not in result.html
    assert "ButtonIcon" not in result.html
    assert "articleFigure-button-download" not in result.html
    assert "citation-dialog-trigger" not in result.html
    assert "pmc-permalink" not in result.html
    assert "collections-dialog" not in result.html
    assert "collections-action-panel" not in result.html
    assert "collections-action-dialog-form" not in result.html
    assert "usa-accordion__button" not in result.html
    assert "d-button" not in result.html
    assert '<figure id="F1">' in result.html
    assert "Visible figure caption." in result.html
    assert (
        '<span class="ArticleReference z2m-frontiers-unresolved-reference" '
        'data-z2m-unresolved-reference="sm1">Supplementary Material</span>'
        in result.html
    )
    assert '<a class="ArticleReference z2m-frontiers-citation" href="#B2">reference two</a>' in result.html


def test_polish_web_html_document_does_not_inject_mathjax_when_static_katex_unavailable(monkeypatch) -> None:
    from zoteropdf2md.raw_html_polish import katex as katex_module

    def raise_import_error() -> None:
        raise ImportError("mini racer unavailable")

    monkeypatch.setattr(katex_module, "katex_v8_context", raise_import_error)
    html = f"""
    <html>
      <head><title>Math Article</title></head>
      <body>
        <article>
          <h1>Math Article</h1>
          <p>{" ".join([LONG_PARAGRAPH] * 20)}</p>
          <p>Inline math \\(x + y\\).</p>
        </article>
      </body>
    </html>
    """

    result = polish_web_html_document(html)

    assert "MathJax-script" not in result.html
    assert "<script" not in result.html
    assert "\\(x + y\\)" in result.html


def test_polish_web_html_document_extracts_pmc_article() -> None:
    html = f"""
    <html>
      <head><title>PMC Article</title></head>
      <body>
        <nav>PMC navigation</nav>
        <main id="main-content">
          <article class="pmc-article" id="article">
            <h1>Comparison of methods</h1>
            <section id="sec1"><p>{" ".join([LONG_PARAGRAPH] * 20)}</p></section>
            <a href="https://pmc.ncbi.nlm.nih.gov/articles/PMC8911527/#sec1">same document</a>
            <a href="/articles/PMC8911527/figure/fig1/">Figure 1</a>
            <a href="/articles/PMC8911527/table/table1/">Table 1</a>
            <a href="https://example.org/outside">outside</a>
            <figure id="FIG1"><figcaption>Figure 1.</figcaption></figure>
            <div id="T1"><table><tr><td>Value</td></tr></table></div>
          </article>
        </main>
      </body>
    </html>
    """

    result = polish_web_html_document(html, source_url="https://pmc.ncbi.nlm.nih.gov/articles/PMC8911527/")

    assert result.kind == WebHtmlKind.PMC_ARTICLE
    assert result.article_extracted is True
    assert result.article_selector in {"article .pmc-article", ".pmc-article"}
    assert "PMC navigation" not in result.html
    assert 'href="#sec1"' in result.html
    assert 'href="#FIG1"' in result.html
    assert 'href="#T1"' in result.html
    assert 'href="https://example.org/outside"' in result.html


def test_polish_web_html_document_removes_empty_pmc_figure_shells() -> None:
    html = f"""
    <html>
      <head><title>PMC Article</title></head>
      <body>
        <main id="main-content">
          <article class="pmc-article" id="article">
            <h1>PMC Article Title</h1>
            <section class="body">
              <p>{" ".join([LONG_PARAGRAPH] * 20)}</p>
              <figure class="fig-group xbox font-sm" id="F2">
                <div class="p text-right font-secondary"><a href="figure/F2/">Open in a new tab</a></div>
              </figure>
              <figure class="fig xbox font-sm" id="F3"><img alt="Figure 3" src="data:image/png;base64,iVBORw0KGgo="/></figure>
            </section>
          </article>
        </main>
      </body>
    </html>
    """

    result = polish_web_html_document(html, source_url="https://pmc.ncbi.nlm.nih.gov/articles/PMC3115600/")

    assert 'id="F2"' not in result.html
    assert 'href="figure/F2/"' not in result.html
    assert 'id="F3"' in result.html


def test_pmc_polish_ignores_invalid_float_href() -> None:
    html = f"""
    <html><body>
      <main id="main-content">
        <article class="pmc-article" id="article">
          <h1>PMC Article</h1>
          <section id="sec1"><p>{" ".join([LONG_PARAGRAPH] * 20)}</p></section>
          <a href="https://[broken/figure/fig1/">broken figure href</a>
          <figure id="FIG1"><figcaption>Figure 1.</figcaption></figure>
        </article>
      </main>
    </body></html>
    """

    result = polish_web_html_document(html, source_url="https://pmc.ncbi.nlm.nih.gov/articles/PMC8911527/")

    assert 'href="https://[broken/figure/fig1/"' in result.html


def test_polish_web_html_document_extracts_taylor_francis_nlm_fulltext() -> None:
    html = f"""
    <html>
      <head><title>Taylor Article</title></head>
      <body>
        <div class="topbar">Taylor navigation</div>
        <article class="NLM_article">
          <div id="abstractId1" class="hlFld-Abstract"><p>{" ".join([LONG_PARAGRAPH] * 8)}</p></div>
          <div class="hlFld-Fulltext">
            <div class="NLM_sec" id="S0001"><h2>Methods</h2><p>{" ".join([LONG_PARAGRAPH] * 20)}</p></div>
            <a href="https://www.tandfonline.com/doi/full/10.1080/example#S0001">back</a>
          </div>
        </article>
      </body>
    </html>
    """

    result = polish_web_html_document(
        html,
        source_url="https://www.tandfonline.com/doi/full/10.1080/example",
    )

    assert result.kind == WebHtmlKind.TAYLOR_FRANCIS_ARTICLE
    assert result.article_extracted is True
    assert result.article_selector == "article .NLM_article"
    assert "Taylor navigation" not in result.html
    assert 'href="#S0001"' in result.html


def test_taylor_francis_polish_rewrites_script_backed_internal_controls() -> None:
    html = f"""
    <html>
      <head><title>Taylor Article</title></head>
      <body>
        <article class="NLM_article">
          <div class="hlFld-Fulltext">
            <div class="NLM_sec" id="S0001"><p>{" ".join([LONG_PARAGRAPH] * 20)}</p></div>
            <p>
              <a href="#" data-rid="CIT0001 CIT0002" data-ref-type="bibr">1-2</a>
              <a href="#" data-rid="EN0001" data-ref-type="fn">note</a>
              <a href="#" data-behaviour-ref="#references-Section1">References</a>
              <button class="ref show-table-fig-ref" data-id="t0001">Table 1</button>
              <a class="displaySizeTable" href="#" data-id="t0001" data-behaviour="show-popup">Display Table</a>
            </p>
            <div id="references-Section1"><ol><li id="CIT0001">Reference one.</li></ol></div>
            <div id="EN0001">Footnote one.</div>
            <div id="t0001"><table><tr><td>Value</td></tr></table></div>
          </div>
        </article>
      </body>
    </html>
    """

    result = polish_web_html_document(
        html,
        source_url="https://www.tandfonline.com/doi/full/10.1080/example",
    )

    assert 'href="#CIT0001"' in result.html
    assert 'href="#EN0001"' in result.html
    assert 'href="#references-Section1"' in result.html
    assert '<a class="z2m-web-ref-button" href="#t0001">Table 1</a>' in result.html
    assert '<a class="displaySizeTable" href="#t0001" data-id="t0001" data-behaviour="show-popup">' in result.html


def test_taylor_francis_polish_targets_existing_table_and_figure_wrappers() -> None:
    html = f"""
    <html>
      <head><title>Taylor Article</title></head>
      <body>
        <article class="NLM_article">
          <div class="hlFld-Fulltext">
            <div class="NLM_sec" id="S0001"><p>{" ".join([LONG_PARAGRAPH] * 20)}</p></div>
            <p>
              <button class="ref show-table-fig-ref" data-id="t0001">Table 1</button>
              <a class="displaySizeTable" href="#" data-id="t0001" data-behaviour="show-popup">Display Table</a>
              <button class="ref show-table-fig-ref" data-id="f0001">Figure 1</button>
            </p>
            <div id="t0001-table-wrapper"><table><tr><td>Value</td></tr></table></div>
            <figure id="f0001-figure-wrapper"><figcaption>Figure 1.</figcaption></figure>
          </div>
        </article>
      </body>
    </html>
    """

    result = polish_web_html_document(
        html,
        source_url="https://www.tandfonline.com/doi/full/10.1080/example",
    )

    assert '<a class="z2m-web-ref-button" href="#t0001-table-wrapper">Table 1</a>' in result.html
    assert '<a class="displaySizeTable" href="#t0001-table-wrapper" data-id="t0001" data-behaviour="show-popup">' in result.html
    assert '<a class="z2m-web-ref-button" href="#f0001-figure-wrapper">Figure 1</a>' in result.html


def test_taylor_francis_polish_uses_publisher_origin_for_doi_source_root_links() -> None:
    html = f"""
    <html>
      <head><title>Taylor Article</title></head>
      <body>
        <article class="NLM_article">
          <div class="hlFld-Fulltext">
            <div class="NLM_sec" id="S0001"><p>{" ".join([LONG_PARAGRAPH] * 20)}</p></div>
            <a href="/action/downloadSupplement?doi=10.1080%2Fexample&amp;file=sm.docx">Supplement</a>
            <img src="/cms/asset/figure.jpg" alt="Figure">
          </div>
        </article>
      </body>
    </html>
    """

    result = polish_web_html_document(html, source_url="https://doi.org/10.1080/example")

    assert 'href="https://www.tandfonline.com/action/downloadSupplement?doi=10.1080%2Fexample&amp;file=sm.docx"' in result.html
    assert 'src="https://www.tandfonline.com/cms/asset/figure.jpg"' in result.html
    assert "https://doi.org/action/downloadSupplement" not in result.html


def test_polish_web_html_document_extracts_springer_nature_body() -> None:
    html = f"""
    <html>
      <head>
        <title>Springer Article</title>
        <link rel="canonical" href="https://link.springer.com/article/10.1007/example">
      </head>
      <body>
        <aside>related articles</aside>
        <article>
          <div class="c-article-body" id="body">
            <h2 id="Sec1">Introduction</h2>
            <p>{" ".join([LONG_PARAGRAPH] * 22)}</p>
            <p>
              See <a href="#Fig1">Fig. 1</a>
              and <a href="https://link.springer.com/article/10.1007/example#Tab1">Table 1</a>.
              Also cite <a href="https://link.springer.com/articles/example#ref-CR1">Reference 1</a>.
            </p>
            <div class="c-article-section__figure" id="figure-1" data-container-section="figure">
              <figure>
                <figcaption>
                  <b id="Fig1" class="c-article-section__figure-caption">Fig. 1</b>
                  Figure caption text.
                </figcaption>
              </figure>
            </div>
            <div class="c-article-table" id="table-1" data-container-section="table">
              <div class="c-article-table__caption">
                <b id="Tab1">Table 1</b> Table caption text.
              </div>
              <table><tr><td>Value</td></tr></table>
            </div>
            <section data-title="References">
              <ul class="c-article-references"><li id="ref-CR1">Reference one.</li></ul>
            </section>
            <a href="/article/10.1007/example/figures/1">Full image</a>
          </div>
        </article>
      </body>
    </html>
    """

    result = polish_web_html_document(html, source_url="https://link.springer.com/article/10.1007/example")

    assert result.kind == WebHtmlKind.SPRINGER_NATURE_ARTICLE
    assert result.article_extracted is True
    assert result.article_selector in {"article .c-article-body", ".c-article-body"}
    assert "related articles" not in result.html
    assert 'href="#figure-1"' in result.html
    assert 'href="#table-1"' in result.html
    assert 'id="Fig1"' in result.html
    assert 'id="Tab1"' in result.html
    assert 'href="#Fig1"' not in result.html
    assert 'href="https://link.springer.com/article/10.1007/example#Tab1"' not in result.html
    assert 'href="#ref-CR1"' in result.html
    assert 'href="https://link.springer.com/articles/example#ref-CR1"' not in result.html
    assert 'href="https://link.springer.com/article/10.1007/example/figures/1"' in result.html


def test_polish_web_html_document_inlines_springer_full_size_tables() -> None:
    html = f"""
    <html>
      <head>
        <title>Springer Article</title>
        <link rel="canonical" href="https://link.springer.com/article/10.1007/example">
      </head>
      <body>
        <article>
          <div class="c-article-body" id="body">
            <h2 id="Sec1">Introduction</h2>
            <p>{" ".join([LONG_PARAGRAPH] * 22)}</p>
            <p>See <a href="https://link.springer.com/article/10.1007/example#Tab1">Table 1</a>.</p>
            <div class="c-article-table" id="table-1" data-container-section="table">
              <figure>
                <figcaption class="c-article-table__figcaption">
                  <b id="Tab1" data-test="table-caption">Table 1 Summary of values</b>
                </figcaption>
                <div class="u-text-right u-hide-print">
                  <a data-test="table-link" href="/article/10.1007/example/tables/1">Full size table</a>
                </div>
              </figure>
            </div>
          </div>
        </article>
      </body>
    </html>
    """
    table_page = """
    <html><body>
      <main>
        <div class="c-article-table-container">
          <div class="c-article-table-border">
            <table class="data last-table">
              <thead><tr><th><p>Fiber Type</p></th><th><p>Function</p></th></tr></thead>
              <tbody><tr><td><p>Aα</p></td><td><p>Motor</p></td></tr></tbody>
            </table>
          </div>
        </div>
      </main>
    </body></html>
    """
    fetched: list[str] = []

    def fake_fetch(url: str) -> str:
        fetched.append(url)
        return table_page

    result = polish_web_html_document(
        html,
        fetch_text=fake_fetch,
    )

    assert fetched == ["https://link.springer.com/article/10.1007/example/tables/1"]
    assert 'href="#table-1"' in result.html
    assert "<table" in result.html
    assert "Fiber Type" in result.html
    assert "Motor" in result.html
    assert "Full size table" not in result.html


def test_polish_web_html_document_extracts_iop_article_content() -> None:
    html = f"""
    <html>
      <head>
        <title>IOP Article</title>
        <link rel="canonical" href="https://iopscience.iop.org/article/10.1088/example">
        <meta name="citation_publisher" content="IOP Publishing">
        <meta name="citation_title" content="IOP Clean Article Title">
      </head>
      <body>
        <header>IOP publisher navigation</header>
        <main id="skip-to-content-link-target">
          <div class="da1-da2" id="page-content" itemscope itemtype="http://schema.org/ScholarlyArticle">
            <div class="article-head">
              <div class="eyebrow">PAPER - OPEN ACCESS</div>
              <p>To cite this article: journal citation text.</p>
            </div>
            <div class="article-content">
              <div class="article-abstract">
                <h2 id="artAbst">Abstract</h2>
                <div class="article-text wd-jnl-art-abstract" itemprop="description">
                  <p>{" ".join([LONG_PARAGRAPH] * 8)}</p>
                </div>
              </div>
              <div class="col-no-break wd-jnl-art-license media">license boilerplate</div>
              <p><small>Export citation and abstract</small></p>
              <div class="linked-articles linked-articles--issue-nav">Previous and next issue articles</div>
              <section class="leaderboard-ad"><div class="ad-iframe-wrap">advertising slot</div></section>
              <div itemprop="articleBody" class="wd-jnl-art-full-text article-text">
                <h2 class="header-anchor" id="iops1">
                  <svg aria-hidden="true" class="fa-icon"><path></path></svg>1. Introduction
                </h2>
                <div class="article-text">
                  <p>
                    {" ".join([LONG_PARAGRAPH] * 20)}
                    <a href="https://iopscience.iop.org/article/10.1088/example#iops1">same document</a>
                    <a href="/article/10.1088/example/pdf">PDF</a>
                    <a href="#iopfn1">note</a>
                    <span class="inline-eqn"><span class="tex"><span class="texImage">
                      <img alt="$G(x,\\sigma)$" role="math" src="data:image/png;base64,placeholder" data-src="https://content.cld.iop.org/journals/example/jneieqn1.gif">
                    </span><script type="math/tex">G(x,\\sigma)</script></span></span>
                  </p>
                </div>
                <div class="display-eqn" id="iop-eqn1">
                  <span class="tex"><span class="texImage">
                    <img alt="Equation (1)" role="math" src="data:image/png;base64,placeholder" data-src="https://content.cld.iop.org/journals/example/jneeqn1.gif">
                  </span><script type="math/tex; mode=display">E=mc^2 \\tag{{1}}</script></span>
                </div>
                <figure id="iopf1" data-toolbar-img="https://content.cld.iop.org/journals/example/fig1_lr.jpg">
                  <figure>
                    <div class="panzoom-container">
                      <div class="panzoom-parent">
                        <img class="panzoom" alt="Figure 1." src="data:image/png;base64,placeholder" data-src="https://content.cld.iop.org/journals/example/fig1_lr.jpg">
                      </div>
                      <div class="buttons zoom-tools"><button class="zoom-in">Zoom In</button></div>
                    </div>
                    <figcaption>
                      <p><strong id="iopf1-label">Figure 1.</strong> Figure caption text.</p>
                      <p class="mb-05 print-hide">Download figure:</p>
                      <span class="btn-multi-block print-hide"><a class="btn fig-dwnld-std-img" href="/journals/example/fig1_lr.jpg">Standard image</a></span>
                    </figcaption>
                  </figure>
                </figure>
              </div>
              <h2 id="footnotes"><svg aria-hidden="true" class="fa-icon"><path></path></svg>Footnotes</h2>
              <div data-mobile-collapse><ul><li id="iopfn1">Footnote one.</li></ul></div>
              <div class="reveal-container references">
                <h2 id="references">References</h2>
                <div><ol><li id="iopbib1">Reference one.</li></ol></div>
              </div>
              <section class="boxout related wd-related-articles">
                <h2>You may also like</h2>
                <p>ChArUco-based 3D scanner</p>
              </section>
            </div>
          </div>
        </main>
      </body>
    </html>
    """

    result = polish_web_html_document(html, source_url="https://iopscience.iop.org/article/10.1088/example")

    assert result.kind == WebHtmlKind.IOP_ARTICLE
    assert result.article_extracted is True
    assert result.article_selector == ".article-content"
    assert 'data-z2m-source-kind="iop_article"' in result.html
    assert '<h1 class="z2m-web-title">IOP Clean Article Title</h1>' in result.html
    assert "Abstract" in result.html
    assert "1. Introduction" in result.html
    assert "Footnote one." in result.html
    assert "Reference one." in result.html
    assert "IOP publisher navigation" not in result.html
    assert "PAPER - OPEN ACCESS" not in result.html
    assert "To cite this article" not in result.html
    assert "license boilerplate" not in result.html
    assert "Export citation and abstract" not in result.html
    assert "Previous and next issue articles" not in result.html
    assert "advertising slot" not in result.html
    assert "You may also like" not in result.html
    assert "ChArUco-based 3D scanner" not in result.html
    assert "Download figure" not in result.html
    assert "Standard image" not in result.html
    assert "Zoom In" not in result.html
    assert "fa-icon" not in result.html
    assert 'href="#iops1"' in result.html
    assert 'href="https://iopscience.iop.org/article/10.1088/example/pdf"' in result.html
    assert 'data-z2m-style="katex"' in result.html
    assert 'class="z2m-math z2m-math-inline"' in result.html
    assert 'class="z2m-math z2m-math-display"' in result.html
    assert r'data-z2m-tex="\(G(x,\sigma)\)"' in result.html
    assert r'data-z2m-tex="\[E=mc^2 \tag{1}\]"' in result.html
    assert "texImage" not in result.html
    assert "jneieqn1.gif" not in result.html
    assert "jneeqn1.gif" not in result.html
    assert 'src="https://content.cld.iop.org/journals/example/fig1_lr.jpg"' in result.html
    assert 'data-z2m-src-placeholder="data:image/png;base64,placeholder"' in result.html
    assert 'src="data:image/png;base64,placeholder"' not in result.html


def test_polish_web_html_document_builds_iop_references_from_metadata() -> None:
    html = f"""
    <html>
      <head>
        <title>IOP Article</title>
        <link rel="canonical" href="https://iopscience.iop.org/article/10.1088/example">
        <meta name="citation_publisher" content="IOP Publishing">
        <meta name="citation_reference" content="citation_journal_title=Journal One; citation_title=First reference; citation_author=A Author; citation_publication_date=2024; citation_firstpage=1; citation_lastpage=2; citation_doi=10.1000/one;">
        <meta name="citation_reference" content="citation_journal_title=Journal Two; citation_title=Second reference; citation_author=B Author; citation_publication_date=2025;">
      </head>
      <body>
        <div class="article-content">
          <div itemprop="articleBody" class="wd-jnl-art-full-text article-text">
            <h2 id="iops1">1. Introduction</h2>
            <p>{" ".join([LONG_PARAGRAPH] * 20)}
              <a class="cite" href="#iopbib1" id="fnref-iopbib1">Author 2024</a>
              <a class="cite" href="#iopbib2" id="fnref-iopbib2">Author 2025</a>
            </p>
          </div>
          <div class="reveal-container references">
            <h2><button class="reveal-trigger article-references" id="references">Show References</button></h2>
            <div id="references-wrapper"><div class="loading-icon">Please wait references are loading.</div></div>
          </div>
        </div>
      </body>
    </html>
    """

    result = polish_web_html_document(html, source_url="https://iopscience.iop.org/article/10.1088/example")

    assert result.unresolved_same_document_links == 0
    assert 'id="iopbib1"' in result.html
    assert 'id="iopbib2"' in result.html
    assert "First reference" in result.html
    assert "Second reference" in result.html
    assert 'href="#iopbib1"' in result.html
    assert 'href="https://doi.org/10.1000/one"' in result.html
    assert "Please wait references are loading" not in result.html


def test_canonicalize_same_document_links_counts_broken_local_fragments() -> None:
    html = '<html><body><section id="sec1"></section><a href="#missing">missing</a><a href="#">noop</a></body></html>'

    result = canonicalize_same_document_links(html)

    assert result.rewritten_count == 0
    assert result.unresolved_count == 1


def test_inline_remote_images_from_web_html_document_allows_scoped_hosts() -> None:
    html = (
        '<html><body><img src="https://content.cld.iop.org/journals/example/fig1.gif">'
        '<img src="https://example.org/other.png"></body></html>'
    )

    result = inline_remote_images_from_web_html_document(
        html,
        allowed_hosts=frozenset({"content.cld.iop.org"}),
        fetch_bytes=lambda url: (b"GIF89afig;", "image/gif"),
    )

    assert result.inlined_images == 1
    assert 'data-z2m-src="https://content.cld.iop.org/journals/example/fig1.gif"' in result.html
    assert 'src="data:image/gif;base64,' in result.html
    assert 'src="https://example.org/other.png"' in result.html


def test_polish_web_html_document_rejects_known_non_full_text_web_pages() -> None:
    researchgate_html = """
    <html><body><h1>ResearchGate publication page</h1><a>Download full-text PDF</a></body></html>
    """
    sciendo_html = """
    <html><body><div id="content-tabs"><button id="tab-button-article"></button>
    <div id="abstract-content">Abstract only</div><script>self.__next_f.push([])</script></div></body></html>
    """
    ojs_html = """
    <html><head><meta name="citation_pdf_url" content="https://almclinmed.ru/jour/article/download/335/341"></head>
    <body><div id="articleAbstract">Abstract</div><div id="articleFullText">
    <a class="file" href="/jour/article/download/335/341">PDF</a></div></body></html>
    """

    with pytest.raises(WebHtmlPolishError):
        polish_web_html_document(researchgate_html, source_url="https://www.researchgate.net/publication/example")
    with pytest.raises(WebHtmlPolishError):
        polish_web_html_document(sciendo_html, source_url="https://content.sciendo.com/article/example")
    with pytest.raises(WebHtmlPolishError):
        polish_web_html_document(ojs_html, source_url="https://www.almclinmed.ru/jour/article/view/335")


def test_polish_web_html_document_accepts_researchgate_article_like_html() -> None:
    html = f"""
    <html><head><title>ResearchGate Article Copy</title></head><body>
      <div class="research-detail-header-section">Publication metadata</div>
      <article>
        <h1>ResearchGate Article Copy</h1>
        <section>
          <h2>Abstract</h2>
          <p>{" ".join([LONG_PARAGRAPH] * 8)}</p>
          <h2>Introduction</h2>
          <p>{" ".join([LONG_PARAGRAPH] * 16)}</p>
          <figure><img src="figure1.png"><figcaption>Figure 1. Signal trace.</figcaption></figure>
          <h2>Results</h2>
          <table><tr><th>Group</th><th>Value</th></tr><tr><td>A</td><td>42</td></tr></table>
          <p>{" ".join([LONG_PARAGRAPH] * 12)}</p>
        </section>
      </article>
    </body></html>
    """

    result = polish_web_html_document(
        html,
        source_url="https://www.researchgate.net/publication/example",
    )

    assert result.kind == WebHtmlKind.RESEARCHGATE_PAGE
    assert result.article_extracted is True
    assert "ResearchGate Article Copy" in result.html
    assert "Figure 1. Signal trace." in result.html
    assert "<table" in result.html


def test_polish_web_html_document_repairs_pdf_like_block_flow() -> None:
    html = f"""
    <html><head><title>ResearchGate PDF-like Article Copy</title></head><body>
      <div class="research-detail-header-section">Publication metadata</div>
      <article>
        <h1>ResearchGate PDF-like Article Copy</h1>
        <p>{" ".join([LONG_PARAGRAPH] * 12)}</p>
        <p>Before table.
          <table id="table-1"><tr><th>Group</th><th>Value</th></tr><tr><td>A</td><td>42</td></tr></table>
          After table.
        </p>
        <p class="source-media"><img src="figure1.png" alt="Signal trace"></p>
        <p><ul id="refs"><li>Reference one.</li><li>Reference two.</li></ul></p>
        <p>{" ".join([LONG_PARAGRAPH] * 12)}</p>
      </article>
    </body></html>
    """

    result = polish_web_html_document(
        html,
        source_url="https://www.researchgate.net/publication/example",
    )

    assert "<table" in result.html
    assert "Before table." in result.html
    assert "After table." in result.html
    assert '<figure class="z2m-standalone-media source-media" id="fig-1"><img src="figure1.png"' in result.html
    assert '<ul id="refs">' in result.html
    assert "<p><table" not in result.html
    assert '<p><ul id="refs">' not in result.html
    assert '<p class="source-media"><img' not in result.html


def test_sciendo_abstract_page_wins_over_article_body_css_noise() -> None:
    html = """
    <html><head><style>.article-body { display: block; }</style></head>
    <body><div id="content-tabs"><button id="tab-button-article"></button>
    <div id="abstract-content">Abstract only</div><script>self.__next_f.push([])</script></div></body></html>
    """

    assert (
        detect_web_html_kind(html, source_url="https://content.sciendo.com/article/example")
        == WebHtmlKind.SCIENDO_ABSTRACT_PAGE
    )


def test_sciendo_abstract_page_fetches_citation_full_html_url() -> None:
    large_inline_payload = "x" * 1_100_000
    abstract_html = """
    <html><head>
      <script>
    """ + large_inline_payload + """
      </script>
      <meta name="citation_full_html_url" content="https://reference-global.com/article/10.5617/jeb.690?tab=article">
    </head><body><div id="content-tabs"><button id="tab-button-article"></button>
    <div id="abstract-content">Abstract only</div><script>self.__next_f.push([])</script></div></body></html>
    """
    full_html = f"""
    <html><head><title>Full Sciendo Article</title></head><body>
      <article><h1>Full Sciendo Article</h1><p>{" ".join([LONG_PARAGRAPH] * 20)}</p></article>
    </body></html>
    """
    fetched: list[str] = []

    def fake_fetch(url: str) -> str:
        fetched.append(url)
        return full_html

    result = polish_web_html_document(
        abstract_html,
        source_url="https://reference-global.com/article/10.5617/jeb.690?tab=abstract",
        fetch_text=fake_fetch,
    )

    assert fetched == ["https://reference-global.com/article/10.5617/jeb.690?tab=article"]
    assert result.kind == WebHtmlKind.GENERIC_ARTICLE
    assert "Full Sciendo Article" in result.html


def test_sciendo_article_tab_is_not_rejected_as_abstract() -> None:
    html = f"""
    <html><body><div id="content-tabs">
      <a id="tab-button-article" href="?tab=article" aria-expanded="true">Article</a>
      <h2>Full Article</h2>
      <div class="ArticleContent_articleContent__cLudH article-content">
        <section><div>Introduction</div><p>{" ".join([LONG_PARAGRAPH] * 20)}</p></section>
      </div>
    </div></body></html>
    """

    assert (
        detect_web_html_kind(html, source_url="https://reference-global.com/article/10.5617/jeb.690?tab=article")
        == WebHtmlKind.GENERIC_ARTICLE
    )
    result = polish_web_html_document(
        html,
        source_url="https://reference-global.com/article/10.5617/jeb.690?tab=article",
    )

    assert result.kind == WebHtmlKind.GENERIC_ARTICLE
    assert result.article_selector == ".article-content"
    assert "Introduction" in result.html


def test_researchgate_detector_does_not_reject_plain_mentions() -> None:
    html = f"""
    <html><body><article>
      <h1>Article</h1>
      <p>{" ".join([LONG_PARAGRAPH] * 20)}</p>
      <p>Dataset mirrored on ResearchGate for visibility.</p>
    </article></body></html>
    """

    assert detect_web_html_kind(html) == WebHtmlKind.GENERIC_ARTICLE


def test_polish_web_html_file_inlines_local_images_without_marker_polish(tmp_path) -> None:
    image_path = tmp_path / "figure.png"
    image_path.write_bytes(PNG_BYTES)
    html_path = tmp_path / "article.html"
    html_path.write_text(
        f"""
        <html><body>
          <article>
            <h1>Article</h1>
            <p>{" ".join([LONG_PARAGRAPH] * 20)}</p>
            <img src="figure.png?download=1" alt="Figure">
          </article>
        </body></html>
        """,
        encoding="utf-8",
    )

    result = polish_web_html_file(html_path)

    assert result.inlined_images == 1
    assert 'data-z2m-src="figure.png?download=1"' in result.html
    assert 'src="data:image/png;base64,' in result.html


def test_polish_web_html_file_repairs_generic_inline_image_data_mime(tmp_path) -> None:
    webp_blob = b"RIFF\x10\x00\x00\x00WEBPVP8 z2m"
    data_url = "data:application/octet-stream;base64," + base64.b64encode(webp_blob).decode("ascii")
    html_path = tmp_path / "article.html"
    html_path.write_text(
        f"""
        <html><body>
          <article>
            <h1>Article</h1>
            <p>{" ".join([LONG_PARAGRAPH] * 20)}</p>
            <figure>
              <picture>
                <source media="(max-width: 600px)" srcset="https://cdn.example/remote.webp 1x">
                <img class="is-inside-mask" src="{data_url}" alt="Figure">
              </picture>
            </figure>
          </article>
        </body></html>
        """,
        encoding="utf-8",
    )

    result = polish_web_html_file(html_path)

    assert 'src="data:image/webp;base64,' in result.html
    assert "data:application/octet-stream" not in result.html
    assert "<source" not in result.html


def test_polish_web_html_file_inlines_local_srcset_images(tmp_path) -> None:
    image_path = tmp_path / "figure.png"
    image_path.write_bytes(PNG_BYTES)
    html_path = tmp_path / "article.html"
    html_path.write_text(
        f"""
        <html><body>
          <article>
            <h1>Article</h1>
            <p>{" ".join([LONG_PARAGRAPH] * 20)}</p>
            <picture><source srcset="figure.png 1x"></picture>
          </article>
        </body></html>
        """,
        encoding="utf-8",
    )

    result = polish_web_html_file(html_path)

    assert result.inlined_images == 1
    assert 'srcset="data:image/png;base64,' in result.html


def test_inline_local_images_skips_inline_data_srcset() -> None:
    payload = "UklGR" + ("A" * 5000)
    html = (
        '<html><body><picture><source srcset="data:image/webp;base64,'
        f'{payload} 1x"></picture></body></html>'
    )

    result = inline_local_images_from_web_html_document(html, base_dir=Path("."))

    assert result.inlined_images == 0
    assert "data:image/webp;base64," in result.html


def test_web_image_data_url_enforces_individual_byte_limit(tmp_path: Path) -> None:
    image = tmp_path / "figure.png"
    image.write_bytes(PNG_BYTES)

    assert to_data_url(image, max_bytes=len(PNG_BYTES) - 1) is None
    data_url = to_data_url(image, max_bytes=len(PNG_BYTES))
    assert data_url is not None
    assert validate_data_url(data_url, image, max_bytes=len(PNG_BYTES)) is True
    assert validate_data_url(data_url, image, max_bytes=len(PNG_BYTES) - 1) is False


def test_inline_local_images_counts_repeated_file_once_against_total_budget(tmp_path: Path) -> None:
    image = tmp_path / "figure.png"
    image.write_bytes(PNG_BYTES)
    html = '<img src="figure.png"><img src="figure.png">'

    result = inline_local_images_from_web_html_document(
        html,
        base_dir=tmp_path,
        max_image_bytes=len(PNG_BYTES),
        max_total_bytes=len(PNG_BYTES),
    )

    assert result.inlined_images == 2
    assert result.html.count("data:image/png;base64,") == 2


def test_inline_local_images_stops_at_document_total_budget(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(PNG_BYTES)
    second.write_bytes(PNG_BYTES)
    html = '<img src="first.png"><img src="second.png">'

    result = inline_local_images_from_web_html_document(
        html,
        base_dir=tmp_path,
        max_image_bytes=len(PNG_BYTES),
        max_total_bytes=len(PNG_BYTES),
    )

    assert result.inlined_images == 1
    assert result.html.count("data:image/png;base64,") == 1
    assert 'src="second.png"' in result.html


def test_inline_remote_images_rejects_injected_oversized_payload() -> None:
    url = "https://content.cld.iop.org/figure.png"
    result = inline_remote_images_from_web_html_document(
        f'<img src="{url}">',
        allowed_hosts=frozenset({"content.cld.iop.org"}),
        fetch_bytes=lambda _url: (b"X" * 5, "image/png"),
        max_image_bytes=4,
        max_total_bytes=10,
    )

    assert result.inlined_images == 0
    assert f'src="{url}"' in result.html


def test_inline_remote_images_enforces_total_budget_and_caches_duplicates() -> None:
    first = "https://content.cld.iop.org/first.png"
    second = "https://content.cld.iop.org/second.png"
    calls: list[str] = []

    def fetch(url: str) -> tuple[bytes, str]:
        calls.append(url)
        return b"PNG", "image/png"

    result = inline_remote_images_from_web_html_document(
        f'<img src="{first}"><img src="{first}"><img src="{second}">',
        allowed_hosts=frozenset({"content.cld.iop.org"}),
        fetch_bytes=fetch,
        max_image_bytes=3,
        max_total_bytes=3,
    )

    assert result.inlined_images == 2
    assert result.html.count("data:image/png;base64,") == 2
    assert f'src="{second}"' in result.html
    assert calls == [first, second]


class _BoundedFetchHeaders(dict[str, str]):
    def get_content_charset(self) -> str:
        return "utf-8"


class _BoundedFetchResponse:
    def __init__(
        self,
        *,
        url: str,
        content_type: str,
        body: bytes,
        content_length: int | None = None,
    ) -> None:
        self.url = url
        self.headers = _BoundedFetchHeaders({"Content-Type": content_type})
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.body = body
        self.read_sizes: list[int] = []

    def __enter__(self) -> "_BoundedFetchResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self.body if size < 0 else self.body[:size]


def test_remote_image_fetch_rejects_declared_oversize_before_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zoteropdf2md import web_html_polish as web_polish_module

    url = "https://content.cld.iop.org/figure.png"
    response = _BoundedFetchResponse(
        url=url,
        content_type="image/png",
        body=b"not read",
        content_length=5,
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: response)

    with pytest.raises(WebHtmlPolishError, match="exceeds 4 bytes"):
        web_polish_module._fetch_remote_image(url, max_bytes=4)
    assert response.read_sizes == []


def test_remote_image_fetch_rejects_cross_host_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zoteropdf2md import web_html_polish as web_polish_module

    source_url = "https://content.cld.iop.org/figure.png"
    response = _BoundedFetchResponse(
        url="https://other.example/figure.png",
        content_type="image/png",
        body=b"not read",
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: response)

    with pytest.raises(WebHtmlPolishError, match="Cross-host"):
        web_polish_module._fetch_remote_image(source_url)
    assert response.read_sizes == []


def test_remote_html_fetch_rejects_declared_oversize_before_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zoteropdf2md import web_html_polish as web_polish_module

    url = "https://reference-global.com/article/example?tab=article"
    response = _BoundedFetchResponse(
        url=url,
        content_type="text/html",
        body=b"not read",
        content_length=5,
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: response)

    with pytest.raises(WebHtmlPolishError, match="exceeds 4 bytes"):
        web_polish_module._fetch_remote_html(url, max_bytes=4)
    assert response.read_sizes == []


def test_polish_web_html_file_rejects_oversized_source_before_parse(tmp_path: Path) -> None:
    source = tmp_path / "article.html"
    source.write_text("<html><body>Article</body></html>", encoding="utf-8")

    with pytest.raises(WebHtmlPolishError, match="exceeds the 8 byte limit"):
        polish_web_html_file(source, max_html_bytes=8)


def test_generic_canonicalizer_infers_repeated_non_arxiv_self_links() -> None:
    html = """
    <html><body>
      <article>
        <section id="sec1"></section>
        <section id="sec2"></section>
        <a href="https://example.org/article#sec1">one</a>
        <a href="https://example.org/article#sec2">two</a>
      </article>
    </body></html>
    """

    result = canonicalize_same_document_links(html)

    assert result.rewritten_count == 2
    assert 'href="#sec1"' in result.html
    assert 'href="#sec2"' in result.html


def test_polish_web_html_replaces_navigation_and_active_media_with_provenance() -> None:
    html = f"""
    <html><head><title>Media Article</title><script src="runtime.js"></script></head><body>
      <nav><img src="logo.png"><a href="/home">Home</a></nav>
      <article>
        <h1>Media Article</h1>
        <p>{" ".join([LONG_PARAGRAPH] * 20)}</p>
        <video controls poster="poster.jpg"><source src="media/demo.mp4"></video>
        <iframe src="/supplement/interactive"></iframe>
        <object data="javascript:alert(1)"></object>
        <a href="java&#10;script:alert(2)" onclick="alert(3)"
           style="background-image: url(https://tracker.example/pixel)">Unsafe link</a>
        <img src="figure.png" onerror=alert(4)>
        <span style=background:url(https://tracker2.example/pixel)>Tracked</span>
        <pre>Code sample: onclick="example" href="javascript:example"</pre>
      </article>
    </body></html>
    """

    result = polish_web_html_document(
        html,
        source_url="https://journal.example/articles/one",
    )

    assert "<script" not in result.html
    assert "<nav" not in result.html
    assert "logo.png" not in result.html
    assert "<video" not in result.html
    assert "<iframe" not in result.html
    assert "<object" not in result.html
    assert 'href="https://journal.example/articles/media/demo.mp4"' in result.html
    assert 'href="https://journal.example/supplement/interactive"' in result.html
    assert "javascript:alert" not in result.html
    assert 'onclick="alert(3)"' not in result.html
    assert "onerror=alert(4)" not in result.html
    assert "tracker.example" not in result.html
    assert "tracker2.example" not in result.html
    assert "java&#10;script" not in result.html
    assert "Unsafe link" in result.html
    assert 'Code sample: onclick="example" href="javascript:example"' in result.html
    assert result.html.count('rel="noopener noreferrer"') == 3


def test_srcset_rewriters_preserve_iiif_commas_inside_urls(tmp_path: Path) -> None:
    image = tmp_path / "iiif" / "full" / "1234," / "0" / "default.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"JPEG")
    html = (
        '<picture><source srcset="iiif/full/1234,/0/default.jpg 1x, '
        'iiif/full/5678,/0/default.jpg 2x"></picture>'
    )

    inlined = inline_local_images_from_web_html_document(html, base_dir=tmp_path)
    absolute = absolutize_root_relative_urls(
        '<source srcset="/iiif/full/1234,/0/default.jpg 1x, '
        '/iiif/full/5678,/0/default.jpg 2x">',
        base_url="https://iiif.example/article",
    )

    assert inlined.inlined_images == 1
    assert "data:image/jpeg;base64,SlBFRw==" in inlined.html
    assert "iiif/full/5678,/0/default.jpg 2x" in inlined.html
    assert "https://iiif.example/iiif/full/1234,/0/default.jpg 1x" in absolute
    assert "https://iiif.example/iiif/full/5678,/0/default.jpg 2x" in absolute
