from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from scripts.bulk_repolish_source_html import _source_url_hint
from scripts.source_html_audit_cleanup import (
    cleanup_plan_from_audit,
    quarantine_storage_dir,
    relay_library_id_for_record,
    trash_stale_arxiv_html,
)


def test_cleanup_plan_groups_audit_records() -> None:
    report = {
        "all_records": [
            {
                "key": "ORPHAN1",
                "library_id": "LIB1",
                "path": r"C:\Zotero\storage\ORPHAN1\Article [SOURCE HTML].html",
                "is_source_html": True,
                "is_arxiv_html": False,
                "issues": ["missing_zotero_attachment_record"],
            },
            {
                "key": "ARXIVOLD",
                "library_id": "LIB1",
                "path": r"C:\Zotero\storage\ARXIVOLD\Article [ARXIV HTML].html",
                "is_source_html": False,
                "is_arxiv_html": True,
                "issues": ["stale_arxiv_html_attachment"],
            },
            {
                "key": "LATEXML1",
                "library_id": "LIB1",
                "path": r"C:\Zotero\storage\LATEXML1\Article [SOURCE HTML].html",
                "is_source_html": True,
                "is_arxiv_html": False,
                "issues": ["latexml_figure_render_error"],
            },
        ]
    }

    plan = cleanup_plan_from_audit(report)

    assert [record["key"] for record in plan["orphan_source_html"]] == ["ORPHAN1"]
    assert [record["key"] for record in plan["stale_arxiv_html"]] == ["ARXIVOLD"]
    assert [record["key"] for record in plan["latexml_repolish"]] == ["LATEXML1"]


def test_quarantine_storage_dir_moves_whole_attachment_folder(tmp_path: Path) -> None:
    storage_dir = tmp_path / "Zotero_Test" / "storage" / "ORPHAN1"
    storage_dir.mkdir(parents=True)
    html_path = storage_dir / "Article [SOURCE HTML].html"
    asset_path = storage_dir / "asset.png"
    html_path.write_text("<html></html>", encoding="utf-8")
    asset_path.write_bytes(b"png")
    record = {
        "key": "ORPHAN1",
        "library_id": "LIB1",
        "path": str(html_path),
    }

    result = quarantine_storage_dir(
        record,
        run_root=tmp_path / "run",
        dry_run=False,
        label="orphan_source_html",
    )

    target = Path(result["target"])
    assert result["ok"] is True
    assert not storage_dir.exists()
    assert (target / html_path.name).read_text(encoding="utf-8") == "<html></html>"
    assert (target / asset_path.name).read_bytes() == b"png"


def test_trash_stale_arxiv_html_dry_run_does_not_need_relay() -> None:
    result = trash_stale_arxiv_html(
        {"key": "ARXIVOLD", "library_id": "LIB1"},
        relay={},
        dry_run=True,
        delete_webdav=True,
        timeout=1,
        deduplication_prefix="test",
    )

    assert result == {"ok": True, "dryRun": True, "wouldTrash": True, "deleteWebdav": True}


def test_relay_library_id_for_record_uses_current_binding_path(tmp_path: Path) -> None:
    data_dir = tmp_path / "Zotero_Data"
    html_path = data_dir / "storage" / "ARXIVOLD" / "Article [ARXIV HTML].html"
    html_path.parent.mkdir(parents=True)
    html_path.write_text("<html></html>", encoding="utf-8")

    library_id = relay_library_id_for_record(
        {"library_id": "stale_audit_id", "path": str(html_path)},
        [SimpleNamespace(library_id="current_relay_id", host_data_dir=data_dir)],
    )

    assert library_id == "current_relay_id"


def test_source_url_hint_prefers_arxiv_parent_metadata() -> None:
    assert (
        _source_url_hint(
            parent_url="https://dl.acm.org/doi/10.1145/example",
            parent_doi="10.48550/arXiv.2507.01903",
            parent_archive_id="",
            parent_archive_location="",
            parent_extra="",
        )
        == "https://arxiv.org/html/2507.01903"
    )
    assert (
        _source_url_hint(
            parent_url="http://arxiv.org/abs/2502.10561",
            parent_doi="10.1145/3706598.3713847",
            parent_archive_id="",
            parent_archive_location="2502.10561",
            parent_extra="arXiv:2502.10561 [cs.HC]",
        )
        == "https://arxiv.org/html/2502.10561"
    )
