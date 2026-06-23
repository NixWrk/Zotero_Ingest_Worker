from __future__ import annotations

import sqlite3
from pathlib import Path

from zoteropdf2md.html_theme import web_readability_style
from zotero_ingest_worker.local_zotero_paths import library_id_for_data_dir
from zotero_ingest_worker.source_html_quality_audit import run_audit


GOOD_HTML = f"""<!doctype html>
<html>
<head>
  <title>Good Article</title>
  {web_readability_style()}
</head>
<body>
<main id="web-doc" data-z2m-source-kind="pmc_article">
  <h1>Good Article</h1>
  <p>See <a href="#bib1">reference</a>.</p>
  <figure><img alt="Plot" src="data:image/png;base64,iVBORw0KGgo="/></figure>
  <table><tr><td>A</td></tr></table>
  <section id="bib1">Bibliography</section>
</main>
</body>
</html>
"""


BAD_HTML = """<!doctype html>
<html>
<head><link rel="canonical" href="https://example.org/article"></head>
<body>
  <p><a href="#missing">missing local target</a></p>
  <a href="https://example.org/article#present">same document absolute link</a>
  <span id="present"></span>
  <img alt="">
  <img alt="Missing file" src="figures/missing.png">
  <figure><figcaption>Lost figure body</figcaption></figure>
  <table></table>
  <script>console.log("not wanted")</script>
  <a href="/tables/1">Table satellite placeholder</a>
</body>
</html>
"""


def test_source_html_audit_accepts_standard_polished_html(tmp_path: Path) -> None:
    data_dir = tmp_path / "Zotero_Test"
    html_path = _write_html_attachment(
        data_dir,
        key="GOOD1234",
        parent_key="PARENT1",
        title="Good Article [source HTML]",
        html=GOOD_HTML,
    )
    state_db = tmp_path / "state.sqlite"
    _write_html_job(
        state_db,
        library_id=library_id_for_data_dir(data_dir),
        attachment_key="GOOD1234",
        status="succeeded",
        pipeline_key="translate=1|en=1|ru=1|source_html=1",
    )

    report = run_audit(zotero_data_dirs=(data_dir,), state_db=state_db)

    assert report["summary"]["source_html_files"] == 1
    assert report["summary"]["critical_records"] == 0
    assert report["summary"]["source_kind_counts"] == {"pmc_article": 1}
    assert report["summary"]["warning_counts"] == {}
    assert report["all_records"][0]["path"] == str(html_path)
    assert report["all_records"][0]["job_ok"] is True


def test_source_html_audit_reports_quality_defects(tmp_path: Path) -> None:
    data_dir = tmp_path / "Zotero_Test"
    _write_html_attachment(
        data_dir,
        key="BAD12345",
        parent_key="PARENT1",
        title="Bad Article [source HTML]",
        html=BAD_HTML,
    )
    state_db = tmp_path / "state.sqlite"
    _write_html_job(
        state_db,
        library_id=library_id_for_data_dir(data_dir),
        attachment_key="OTHER999",
        status="succeeded",
        pipeline_key="translate=1|en=1|ru=1|source_html=1",
    )

    report = run_audit(zotero_data_dirs=(data_dir,), state_db=state_db)
    issues = set(report["critical_records"][0]["issues"])

    assert "no_succeeded_source_html_job" in issues
    assert "missing_web_polish_style" in issues
    assert "missing_web_doc_main" in issues
    assert "missing_source_kind" in issues
    assert "unresolved_local_fragment_links" in issues
    assert "absolute_fragment_links_resolve_local" in issues
    assert "image_missing_src" in issues
    assert "image_relative_missing_file" in issues
    assert "script_tags_present" in issues
    assert "table_without_rows" in issues
    assert "springer_table_placeholder" not in issues
    assert report["summary"]["warning_only_records"] == 0
    assert report["summary"]["warning_counts"]["figure_without_media_warning"] == 1
    assert report["warning_records"][0]["warnings"] == ["figure_without_media_warning"]


def test_source_html_audit_accepts_nested_figure_media(tmp_path: Path) -> None:
    data_dir = tmp_path / "Zotero_Test"
    html = GOOD_HTML.replace(
        '<figure><img alt="Plot" src="data:image/png;base64,iVBORw0KGgo="/></figure>',
        '<figure id="outer"><figure><img alt="Plot" src="data:image/png;base64,iVBORw0KGgo="/></figure></figure>',
    )
    _write_html_attachment(
        data_dir,
        key="NEST1234",
        parent_key="PARENT1",
        title="Nested Figure [source HTML]",
        html=html,
    )

    report = run_audit(zotero_data_dirs=(data_dir,), state_db=None)

    assert report["summary"]["warning_counts"] == {}


def test_source_html_audit_accepts_latexml_table_and_listing_figures(tmp_path: Path) -> None:
    data_dir = tmp_path / "Zotero_Test"
    html = GOOD_HTML.replace(
        '<figure><img alt="Plot" src="data:image/png;base64,iVBORw0KGgo="/></figure>',
        """
        <figure class="ltx_table"><figcaption>Table 1</figcaption><table><tr><td>A</td></tr></table></figure>
        <figure class="ltx_float ltx_lstlisting"><figcaption>Listing 1</figcaption><pre>print(1)</pre></figure>
        """,
    )
    _write_html_attachment(
        data_dir,
        key="LTX12345",
        parent_key="PARENT1",
        title="LaTeXML Figure-like Blocks [source HTML]",
        html=html,
    )

    report = run_audit(zotero_data_dirs=(data_dir,), state_db=None)

    assert report["summary"]["warning_counts"] == {}


def test_source_html_audit_accepts_supplementary_file_boxes(tmp_path: Path) -> None:
    data_dir = tmp_path / "Zotero_Test"
    html = GOOD_HTML.replace(
        '<figure><img alt="Plot" src="data:image/png;base64,iVBORw0KGgo="/></figure>',
        '<figure class="fig xbox"><a href="supp1-3358562.docx">supplementary document</a></figure>',
    )
    _write_html_attachment(
        data_dir,
        key="SUPP1234",
        parent_key="PARENT1",
        title="Supplementary File [source HTML]",
        html=html,
    )

    report = run_audit(zotero_data_dirs=(data_dir,), state_db=None)

    assert report["summary"]["warning_counts"] == {}


def test_source_html_audit_accepts_video_figure_boxes(tmp_path: Path) -> None:
    data_dir = tmp_path / "Zotero_Test"
    html = GOOD_HTML.replace(
        '<figure><img alt="Plot" src="data:image/png;base64,iVBORw0KGgo="/></figure>',
        '<figure class="fig xbox"><a href="video1.mp4">Download video file</a></figure>',
    )
    _write_html_attachment(
        data_dir,
        key="VID12345",
        parent_key="PARENT1",
        title="Video Figure [source HTML]",
        html=html,
    )

    report = run_audit(zotero_data_dirs=(data_dir,), state_db=None)

    assert report["summary"]["warning_counts"] == {}


def test_source_html_audit_flags_latexml_render_errors_inside_figures(tmp_path: Path) -> None:
    data_dir = tmp_path / "Zotero_Test"
    html = GOOD_HTML.replace(
        '<figure><img alt="Plot" src="data:image/png;base64,iVBORw0KGgo="/></figure>',
        '<figure class="ltx_figure"><span class="ltx_ERROR undefined">{forest}</span><p>raw tree code</p></figure>',
    )
    _write_html_attachment(
        data_dir,
        key="LTXERR12",
        parent_key="PARENT1",
        title="LaTeXML Error Figure [source HTML]",
        html=html,
    )

    report = run_audit(zotero_data_dirs=(data_dir,), state_db=None)

    assert report["summary"]["issue_counts"]["latexml_figure_render_error"] == 1
    assert report["summary"]["critical_records"] == 1
    assert "latexml_figure_render_error" in report["critical_records"][0]["issues"]


def test_source_html_audit_flags_latexml_item_marker_layout_without_style(tmp_path: Path) -> None:
    data_dir = tmp_path / "Zotero_Test"
    html = """<!doctype html>
    <html>
    <head><style data-z2m-style="web-html-polish">body { color: #111; }</style></head>
    <body>
    <main id="web-doc" data-z2m-source-kind="arxiv_latexml">
      <h1>List Article</h1>
      <ul class="ltx_itemize">
        <li class="ltx_item" style="list-style-type:none;">
          <span class="ltx_tag ltx_tag_item">•</span>
          <div class="ltx_para"><p class="ltx_p">Visual landmarks.</p></div>
        </li>
      </ul>
    </main>
    </body>
    </html>
    """
    _write_html_attachment(
        data_dir,
        key="LTXITEM1",
        parent_key="PARENT1",
        title="LaTeXML List [source HTML]",
        html=html,
    )

    report = run_audit(zotero_data_dirs=(data_dir,), state_db=None)

    assert report["summary"]["issue_counts"]["latexml_itemize_marker_layout"] == 1
    assert report["critical_records"][0]["counts"]["latexml_itemize_marker_blocks"] == 1


def test_source_html_audit_flags_latexml_inline_black_text(tmp_path: Path) -> None:
    data_dir = tmp_path / "Zotero_Test"
    html = GOOD_HTML.replace(
        "<h1>Good Article</h1>",
        """
        <h1>Good Article</h1>
        <figure class="ltx_table" id="S1.T1">
          <table class="ltx_tabular"><tr><td>
            <span class="ltx_rule" style="width:100%;height:0.8pt;color:#000000;background:#000000;display:inline-block;"> </span>
            <span class="ltx_text ltx_font_bold" style="font-size:90%;color:#000000;">Visual</span>
          </td></tr></table>
          <figcaption>Table 1</figcaption>
        </figure>
        """,
    )
    _write_html_attachment(
        data_dir,
        key="LTXBLK01",
        parent_key="PARENT1",
        title="LaTeXML Black Text [source HTML]",
        html=html,
    )

    report = run_audit(zotero_data_dirs=(data_dir,), state_db=None)

    assert report["summary"]["issue_counts"]["latexml_inline_black_text"] == 1
    assert report["critical_records"][0]["counts"]["latexml_inline_black_text_styles"] == 1


def test_source_html_audit_flags_latexml_black_mathcolor(tmp_path: Path) -> None:
    data_dir = tmp_path / "Zotero_Test"
    html = GOOD_HTML.replace(
        "<h1>Good Article</h1>",
        """
        <h1>Good Article</h1>
        <figure class="ltx_table" id="S1.T1">
          <table class="ltx_tabular"><tr><td>
            <math class="ltx_Math" display="inline">
              <mrow>
                <mi mathcolor="#000000" mathsize="90%">M</mi>
                <mo mathcolor="#000">=</mo>
                <mn mathcolor="black">6.09</mn>
                <mi mathcolor="#ff0000">x</mi>
              </mrow>
            </math>
          </td></tr></table>
          <figcaption>Table 1</figcaption>
        </figure>
        """,
    )
    _write_html_attachment(
        data_dir,
        key="LTXMATH1",
        parent_key="PARENT1",
        title="LaTeXML Black Math [source HTML]",
        html=html,
    )

    report = run_audit(zotero_data_dirs=(data_dir,), state_db=None)

    assert report["summary"]["issue_counts"]["latexml_math_black_color"] == 1
    assert report["critical_records"][0]["counts"]["latexml_black_mathcolor_attrs"] == 3


def test_source_html_audit_flags_discovered_regression_defects(tmp_path: Path) -> None:
    data_dir = tmp_path / "Zotero_Test"
    webp = "UklGRhAAAABXRUJQVlA4IHoybQ=="
    html = f"""<!doctype html>
    <html>
    <head>
      <style data-z2m-style="web-html-polish">body {{ color: #111; }}</style>
    </head>
    <body>
    <main id="web-doc" data-z2m-source-kind="arxiv_latexml">
      <h1>Regression Article</h1>
      <dl class="def-list"><dt>MI</dt><dd><p>motor imagery</p></dd></dl>
      <picture>
        <source srcset="https://cdn.example/fig.webp 1x">
        <img alt="Figure" src="data:image/webp;base64,{webp}">
      </picture>
      <img alt="Generic MIME" src="data:application/octet-stream;base64,{webp}">
      <figure class="ltx_table">
        <figcaption class="ltx_caption">
          <span class="ltx_transformed_outer"><span class="ltx_transformed_inner">Table 1</span></span>
        </figcaption>
        <table><tr><td>\\rowcolor gray!10 RNN</td></tr></table>
      </figure>
      <table class="disp-formula"><tr><td class="formula"><math display="block">x</math></td><td class="label">(1)</td></tr></table>
    </main>
    </body>
    </html>
    """
    _write_html_attachment(
        data_dir,
        key="REGRESS1",
        parent_key="PARENT1",
        title="Regression Article [source HTML]",
        html=html,
    )

    report = run_audit(zotero_data_dirs=(data_dir,), state_db=None)
    issues = set(report["critical_records"][0]["issues"])

    assert "image_data_non_image_mime" in issues
    assert "picture_source_overrides_inline_image" in issues
    assert "latexml_rowcolor_artifact" in issues
    assert "missing_def_list_style" in issues
    assert "missing_latexml_caption_style" in issues
    assert "missing_latexml_table_style" in issues
    assert "missing_formula_style" in issues
    assert report["critical_records"][0]["counts"]["picture_inline_data_img_with_source"] == 1
    assert report["critical_records"][0]["counts"]["latexml_rowcolor_artifacts"] == 1


def test_source_html_audit_flags_frontiers_reference_buttons(tmp_path: Path) -> None:
    data_dir = tmp_path / "Zotero_Test"
    html = GOOD_HTML.replace(
        '<p>See <a href="#bib1">reference</a>.</p>',
        """
        <p>
          See
          <button type="button" color="Blue40" id="B1-button" class="ArticleReference" data-event="articleReference-a-b1"> 2024 </button>.
        </p>
        <ul class="References">
          <li class="References__item" id="B1">Reference one.</li>
        </ul>
        """,
    )
    _write_html_attachment(
        data_dir,
        key="FRONT001",
        parent_key="PARENT1",
        title="Frontiers Article [source HTML]",
        html=html,
    )

    report = run_audit(zotero_data_dirs=(data_dir,), state_db=None)

    assert report["summary"]["issue_counts"]["frontiers_reference_button"] == 1
    assert report["critical_records"][0]["counts"]["frontiers_reference_buttons"] == 1
    assert "frontiers_reference_button" in report["critical_records"][0]["issues"]


def test_source_html_audit_flags_discovered_js_wrapped_source_html(tmp_path: Path) -> None:
    data_dir = tmp_path / "Zotero_Test"
    html = f"""<!doctype html>
    <html>
    <head>
      <title>JS Wrapped Article</title>
      {web_readability_style()}
    </head>
    <body>
      <main id="web-doc" data-z2m-source-kind="pmc_article">
        <main id="web-doc" data-z2m-source-kind="pmc_article">
          <h1>JS Wrapped Article</h1>
          <p>
            See <a class="ArticleReference" data-event="articleReference-a-sm1">Supplementary Material</a>.
          </p>
          <button class="ArticleFigure__figureButton" aria-label="Open lightbox for FIGURE 1">
            <figure id="F1"><img alt="Figure" src="data:image/png;base64,iVBORw0KGgo="/></figure>
          </button>
          <button class="ButtonIcon" data-event="articleFigure-button-download" aria-label="Download FIGURE 1">Download</button>
          <button class="citation-dialog-trigger">Cite</button>
          <form id="collections-action-dialog-form"><button type="button">Save</button></form>
          <table><tr><td>A</td></tr></table>
        </main>
      </main>
    </body>
    </html>
    """
    _write_html_attachment(
        data_dir,
        key="JSWRAP01",
        parent_key="PARENT1",
        title="JS Wrapped Article [source HTML]",
        html=html,
    )

    report = run_audit(zotero_data_dirs=(data_dir,), state_db=None)
    record = report["critical_records"][0]
    issues = set(record["issues"])

    assert "nested_web_doc_wrapper" in issues
    assert "frontiers_empty_article_reference_link" in issues
    assert "frontiers_figure_js_control" in issues
    assert "pmc_dead_ui_control" in issues
    assert record["counts"]["web_doc_mains"] == 2
    assert record["counts"]["frontiers_empty_article_reference_links"] == 1
    assert record["counts"]["frontiers_figure_js_controls"] == 2
    assert record["counts"]["pmc_dead_ui_controls"] == 2


def test_source_html_audit_flags_discovered_js_wrapped_generated_html(tmp_path: Path) -> None:
    data_dir = tmp_path / "Zotero_Test"
    html = f"""<!doctype html>
    <html>
    <head>
      <title>Generated Article</title>
      {web_readability_style()}
    </head>
    <body>
      <main id="web-doc" data-z2m-source-kind="pmc_article">
        <main id="web-doc" data-z2m-source-kind="pmc_article">
          <h1>Generated Article</h1>
          <p>Translated article text.</p>
          <button class="pmc-permalink__dropdown__copy__btn" type="button">Copy</button>
        </main>
      </main>
    </body>
    </html>
    """
    _write_html_attachment(
        data_dir,
        key="GENRU001",
        parent_key="PARENT1",
        title="Generated Article [RU HTML]",
        html=html,
    )

    report = run_audit(zotero_data_dirs=(data_dir,), state_db=None)
    record = report["critical_records"][0]

    assert report["summary"]["source_html_files"] == 0
    assert report["summary"]["non_source_html_files"] == 1
    assert record["is_generated_html"] is True
    assert record["is_source_html"] is False
    assert "nested_web_doc_wrapper" in record["issues"]
    assert "pmc_dead_ui_control" in record["issues"]
    assert "missing_web_polish_style" not in record["issues"]


def test_source_html_audit_flags_stale_active_arxiv_html_sibling(tmp_path: Path) -> None:
    data_dir = tmp_path / "Zotero_Test"
    _write_html_attachment(
        data_dir,
        key="SOURCE1",
        parent_key="PARENT1",
        title="Article [source HTML]",
        html=GOOD_HTML,
    )
    arxiv_dir = data_dir / "storage" / "ARXIV001"
    arxiv_dir.mkdir(parents=True)
    arxiv_filename = "Article [ARXIV HTML].html"
    (arxiv_dir / arxiv_filename).write_text("<html><body>old arxiv html</body></html>", encoding="utf-8")
    connection = sqlite3.connect(data_dir / "zotero.sqlite")
    try:
        connection.execute("insert into items (itemID, key, dateModified) values (3, 'ARXIV001', '')")
        connection.execute(
            """
            insert into itemAttachments (itemID, parentItemID, linkMode, contentType, path)
            values (3, 1, 1, 'text/html', ?)
            """,
            (f"storage:{arxiv_filename}",),
        )
        connection.execute("insert into itemDataValues (valueID, value) values (2, 'Article [ARXIV HTML]')")
        connection.execute("insert into itemData (itemID, fieldID, valueID) values (3, 1, 2)")
        connection.commit()
    finally:
        connection.close()

    report = run_audit(zotero_data_dirs=(data_dir,), state_db=None)
    stale = [
        record
        for record in report["critical_records"]
        if "stale_arxiv_html_attachment" in record["issues"]
    ]

    assert len(stale) == 1
    assert stale[0]["key"] == "ARXIV001"
    assert stale[0]["is_arxiv_html"] is True
    assert report["summary"]["issue_counts"]["stale_arxiv_html_attachment"] == 1


def test_source_html_audit_flags_stale_arxiv_db_record_with_missing_file(tmp_path: Path) -> None:
    data_dir = tmp_path / "Zotero_Test"
    _write_html_attachment(
        data_dir,
        key="SOURCE1",
        parent_key="PARENT1",
        title="Article [source HTML]",
        html=GOOD_HTML,
    )
    connection = sqlite3.connect(data_dir / "zotero.sqlite")
    try:
        connection.execute("insert into items (itemID, key, dateModified) values (3, 'ARXIV001', '')")
        connection.execute(
            """
            insert into itemAttachments (itemID, parentItemID, linkMode, contentType, path)
            values (3, 1, 1, 'text/html', 'storage:Article [ARXIV HTML].html')
            """
        )
        connection.execute("insert into itemDataValues (valueID, value) values (2, 'Article [ARXIV HTML]')")
        connection.execute("insert into itemData (itemID, fieldID, valueID) values (3, 1, 2)")
        connection.commit()
    finally:
        connection.close()

    report = run_audit(zotero_data_dirs=(data_dir,), state_db=None)
    stale = [
        record
        for record in report["critical_records"]
        if "stale_arxiv_html_attachment" in record["issues"]
    ]

    assert len(stale) == 1
    assert stale[0]["key"] == "ARXIV001"
    assert stale[0]["read_error"] == "attachment_file_missing"
    assert "html_attachment_missing_file" in stale[0]["issues"]
    assert report["summary"]["issue_counts"]["stale_arxiv_html_attachment"] == 1
    assert report["summary"]["issue_counts"]["html_attachment_missing_file"] == 1


def test_source_html_audit_does_not_flag_jobs_when_state_has_no_source_jobs(tmp_path: Path) -> None:
    data_dir = tmp_path / "Zotero_Test"
    _write_html_attachment(
        data_dir,
        key="GOOD1234",
        parent_key="PARENT1",
        title="Good Article [source HTML]",
        html=GOOD_HTML,
    )
    state_db = tmp_path / "state.sqlite"
    _write_state_schema(state_db)

    report = run_audit(zotero_data_dirs=(data_dir,), state_db=state_db)

    assert report["summary"]["source_html_job_check_enabled"] is False
    assert "no_succeeded_source_html_job" not in report["all_records"][0]["issues"]
    assert report["all_records"][0]["job_ok"] is None


def test_source_html_audit_flags_orphan_source_html_file(tmp_path: Path) -> None:
    data_dir = tmp_path / "Zotero_Test"
    storage_dir = data_dir / "storage" / "ORPHAN1"
    storage_dir.mkdir(parents=True)
    (storage_dir / "Orphan [SOURCE HTML].html").write_text(GOOD_HTML, encoding="utf-8")
    _write_zotero_schema(data_dir)

    report = run_audit(zotero_data_dirs=(data_dir,), state_db=None)

    assert report["summary"]["source_html_files"] == 1
    assert report["critical_records"][0]["issues"] == ["missing_zotero_attachment_record"]


def test_source_html_audit_flags_orphan_arxiv_html_file(tmp_path: Path) -> None:
    data_dir = tmp_path / "Zotero_Test"
    storage_dir = data_dir / "storage" / "ARXIV1"
    storage_dir.mkdir(parents=True)
    (storage_dir / "Article [ARXIV HTML].html").write_text(GOOD_HTML, encoding="utf-8")
    _write_zotero_schema(data_dir)

    report = run_audit(zotero_data_dirs=(data_dir,), state_db=None)

    assert report["summary"]["non_source_html_files"] == 1
    assert report["summary"]["issue_counts"]["missing_zotero_attachment_record"] == 1
    assert report["critical_records"][0]["is_arxiv_html"] is True
    assert report["critical_records"][0]["issues"] == ["missing_zotero_attachment_record"]


def _write_html_attachment(
    data_dir: Path,
    *,
    key: str,
    parent_key: str,
    title: str,
    html: str,
) -> Path:
    storage_dir = data_dir / "storage" / key
    storage_dir.mkdir(parents=True)
    filename = f"{title}.html"
    html_path = storage_dir / filename
    html_path.write_text(html, encoding="utf-8")
    _write_zotero_schema(data_dir)
    connection = sqlite3.connect(data_dir / "zotero.sqlite")
    try:
        connection.execute("insert into items (itemID, key, dateModified) values (1, ?, '')", (parent_key,))
        connection.execute("insert into items (itemID, key, dateModified) values (2, ?, '')", (key,))
        connection.execute(
            """
            insert into itemAttachments (itemID, parentItemID, linkMode, contentType, path)
            values (2, 1, 1, 'text/html', ?)
            """,
            (f"storage:{filename}",),
        )
        connection.execute("insert into fields (fieldID, fieldName) values (1, 'title')")
        connection.execute("insert into itemDataValues (valueID, value) values (1, ?)", (title,))
        connection.execute("insert into itemData (itemID, fieldID, valueID) values (2, 1, 1)")
        connection.commit()
    finally:
        connection.close()
    return html_path


def _write_zotero_schema(data_dir: Path) -> None:
    (data_dir / "storage").mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(data_dir / "zotero.sqlite")
    try:
        connection.executescript(
            """
            create table if not exists items (
              itemID integer primary key,
              key text not null,
              dateModified text
            );
            create table if not exists itemAttachments (
              itemID integer primary key,
              parentItemID integer,
              linkMode integer,
              contentType text,
              path text
            );
            create table if not exists deletedItems (itemID integer primary key);
            create table if not exists fields (fieldID integer primary key, fieldName text);
            create table if not exists itemData (itemID integer, fieldID integer, valueID integer);
            create table if not exists itemDataValues (valueID integer primary key, value text);
            """
        )
        connection.commit()
    finally:
        connection.close()


def _write_html_job(
    state_db: Path,
    *,
    library_id: str,
    attachment_key: str,
    status: str,
    pipeline_key: str,
) -> None:
    _write_state_schema(state_db)
    connection = sqlite3.connect(state_db)
    try:
        connection.execute(
            """
            insert into html_jobs (library_id, attachment_key, status, pipeline_key, en_html_path)
            values (?, ?, ?, ?, '/tmp/en.html')
            """,
            (library_id, attachment_key, status, pipeline_key),
        )
        connection.commit()
    finally:
        connection.close()


def _write_state_schema(state_db: Path) -> None:
    state_db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(state_db)
    try:
        connection.execute(
            """
            create table if not exists html_jobs (
              library_id text,
              attachment_key text,
              status text,
              pipeline_key text,
              en_html_path text
            )
            """
        )
        connection.commit()
    finally:
        connection.close()
