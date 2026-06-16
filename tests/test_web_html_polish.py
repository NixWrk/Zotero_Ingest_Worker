import pytest

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
    canonicalize_same_document_links,
    count_same_document_absolute_fragment_links,
    detect_web_html_kind,
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
    assert ".off-screen, .sr-only" in result.html


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
      <head><title>Springer Article</title></head>
      <body>
        <aside>related articles</aside>
        <article>
          <div class="c-article-body" id="body">
            <h2 id="Sec1">Introduction</h2>
            <p>{" ".join([LONG_PARAGRAPH] * 22)}</p>
            <p>
              See <a href="#Fig1">Fig. 1</a>
              and <a href="https://link.springer.com/article/10.1007/example#Tab1">Table 1</a>.
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
              <ul class="c-article-references"><li>Reference one.</li></ul>
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
    assert 'href="https://link.springer.com/article/10.1007/example/figures/1"' in result.html


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
