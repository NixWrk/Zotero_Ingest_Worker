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
