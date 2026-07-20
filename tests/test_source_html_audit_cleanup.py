from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from scripts.bulk_repolish_source_html import _source_url_hint
import scripts.source_html_audit_cleanup as cleanup_module
from scripts.source_html_audit_cleanup import (
    cleanup_plan_from_audit,
    list_remote_html_attachments,
    mark_local_attachment_deleted,
    quarantine_storage_dir,
    relay_library_id_for_record,
    remote_stale_arxiv_records,
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
                "key": "ARXIVORPHAN",
                "library_id": "LIB1",
                "path": r"C:\Zotero\storage\ARXIVORPHAN\Article [ARXIV HTML].html",
                "is_source_html": False,
                "is_arxiv_html": True,
                "issues": ["missing_zotero_attachment_record"],
            },
            {
                "key": "LATEXML1",
                "library_id": "LIB1",
                "path": r"C:\Zotero\storage\LATEXML1\Article [SOURCE HTML].html",
                "is_source_html": True,
                "is_arxiv_html": False,
                "issues": ["latexml_figure_render_error"],
            },
            {
                "key": "LATEXML2",
                "library_id": "LIB1",
                "path": r"C:\Zotero\storage\LATEXML2\Article [SOURCE HTML].html",
                "is_source_html": True,
                "is_arxiv_html": False,
                "issues": ["latexml_itemize_marker_layout"],
            },
            {
                "key": "LATEXML3",
                "library_id": "LIB1",
                "path": r"C:\Zotero\storage\LATEXML3\Article [SOURCE HTML].html",
                "is_source_html": True,
                "is_arxiv_html": False,
                "issues": ["latexml_inline_black_text"],
            },
            {
                "key": "LATEXML4",
                "library_id": "LIB1",
                "path": r"C:\Zotero\storage\LATEXML4\Article [SOURCE HTML].html",
                "is_source_html": True,
                "is_arxiv_html": False,
                "issues": ["latexml_math_black_color"],
            },
            {
                "key": "RAWHTML1",
                "library_id": "LIB1",
                "path": r"C:\Zotero\storage\RAWHTML1\Article [SOURCE HTML].html",
                "is_source_html": True,
                "is_arxiv_html": False,
                "issues": ["missing_web_polish_style", "script_tags_present"],
            },
        ]
    }

    plan = cleanup_plan_from_audit(report)

    assert [record["key"] for record in plan["orphan_source_html"]] == ["ORPHAN1"]
    assert [record["key"] for record in plan["orphan_arxiv_html"]] == ["ARXIVORPHAN"]
    assert [record["key"] for record in plan["stale_arxiv_html"]] == ["ARXIVOLD"]
    assert [record["key"] for record in plan["latexml_repolish"]] == [
        "LATEXML1",
        "LATEXML2",
        "LATEXML3",
        "LATEXML4",
        "RAWHTML1",
    ]


def test_remote_stale_arxiv_records_include_remote_only_siblings(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "Zotero"
    source_path = data_dir / "storage" / "SOURCE1" / "Article [SOURCE HTML].html"
    report = {
        "all_records": [
            {
                "key": "SOURCE1",
                "library_id": "local_hash",
                "parent_key": "PARENT1",
                "path": str(source_path),
                "is_source_html": True,
                "is_arxiv_html": False,
                "issues": [],
            },
            {
                "key": "REMOTE1",
                "library_id": "local_hash",
                "parent_key": None,
                "path": str(
                    data_dir / "storage" / "REMOTE1" / "Article [ARXIV HTML].html"
                ),
                "is_source_html": False,
                "is_arxiv_html": True,
                "issues": ["missing_zotero_attachment_record"],
            },
        ]
    }
    binding = SimpleNamespace(library_id="RELAY_LIB", host_data_dir=data_dir)
    remote_by_library = {
        "RELAY_LIB": [
            {
                "key": "REMOTE1",
                "parentItem": "PARENT1",
                "title": "Article [FULL TEXT] [ARXIV HTML].html",
                "filename": "Article [FULL TEXT] [ARXIV HTML].html",
                "contentType": "text/html",
                "version": 12,
                "deleted": False,
            },
            {
                "key": "REMOTE2",
                "parentItem": "OTHER",
                "title": "Other [ARXIV HTML].html",
                "filename": "Other [ARXIV HTML].html",
                "contentType": "text/html",
                "version": 13,
                "deleted": False,
            },
            {
                "key": "REMOTE3",
                "parentItem": "PARENT1",
                "title": "Deleted [ARXIV HTML].html",
                "filename": "Deleted [ARXIV HTML].html",
                "contentType": "text/html",
                "version": 14,
                "deleted": True,
            },
        ]
    }

    records = remote_stale_arxiv_records(
        report,
        bindings=[binding],
        remote_by_library=remote_by_library,
    )

    assert [record["key"] for record in records] == ["REMOTE1"]
    assert records[0]["library_id"] == "RELAY_LIB"
    assert records[0]["remote_only"] is True
    assert records[0]["issues"] == [
        "stale_arxiv_html_attachment",
        "remote_only_arxiv_html_attachment",
    ]


def test_remote_stale_keys_are_scoped_by_library(tmp_path: Path) -> None:
    first_dir = tmp_path / "First"
    second_dir = tmp_path / "Second"
    report = {
        "all_records": [
            {
                "key": "SOURCE1",
                "library_id": "local-second",
                "parent_key": "PARENT2",
                "path": str(
                    second_dir / "storage" / "SOURCE1" / "Article [SOURCE HTML].html"
                ),
                "is_source_html": True,
                "issues": [],
            },
            {
                "key": "SAMEKEY",
                "library_id": "local-first",
                "parent_key": "PARENT1",
                "path": str(
                    first_dir / "storage" / "SAMEKEY" / "Article [ARXIV HTML].html"
                ),
                "is_arxiv_html": True,
                "issues": ["stale_arxiv_html_attachment"],
            },
        ]
    }
    bindings = [
        SimpleNamespace(library_id="RELAY_FIRST", host_data_dir=first_dir),
        SimpleNamespace(library_id="RELAY_SECOND", host_data_dir=second_dir),
    ]

    records = remote_stale_arxiv_records(
        report,
        bindings=bindings,
        remote_by_library={
            "RELAY_SECOND": [
                {
                    "key": "SAMEKEY",
                    "parentItem": "PARENT2",
                    "title": "Article [ARXIV HTML].html",
                    "contentType": "text/html",
                    "deleted": False,
                }
            ]
        },
    )

    assert [(record["library_id"], record["key"]) for record in records] == [
        ("RELAY_SECOND", "SAMEKEY")
    ]


class _FakeJsonResponse:
    def __init__(self, payload: object) -> None:
        self._payload = json.dumps(payload).encode("utf-8")
        self.read_sizes: list[int] = []

    def __enter__(self) -> _FakeJsonResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if size < 0:
            return self._payload
        return self._payload[:size]


@pytest.mark.parametrize(
    ("function_name", "field"),
    [
        ("list_remote_html_attachments", "attachments"),
        ("list_remote_item_children", "children"),
    ],
)
@pytest.mark.parametrize(
    "payload_factory",
    [
        lambda field: {"ok": "true", field: []},
        lambda field: {"ok": True, field: "not-a-list"},
        lambda field: {"ok": True, field: [{"key": "GOOD"}, "bad-item"]},
    ],
    ids=["truthy-ok", "malformed-container", "malformed-item"],
)
def test_remote_list_rejects_malformed_contract(
    monkeypatch: Any,
    function_name: str,
    field: str,
    payload_factory: Any,
) -> None:
    payload = payload_factory(field)
    monkeypatch.setattr(
        cleanup_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _FakeJsonResponse(payload),
    )
    function = getattr(cleanup_module, function_name)
    kwargs: dict[str, object] = {
        "relay": {"url": "https://relay.test", "token": "token"},
        "library_id": "LIB1",
        "timeout": 1,
    }
    if field == "children":
        kwargs["parent_key"] = "PARENT1"

    with pytest.raises(RuntimeError, match="invalid response contract"):
        function(**kwargs)


def test_remote_list_accepts_exact_contract(monkeypatch: Any) -> None:
    payload = {"ok": True, "attachments": [{"key": "HTML1"}]}
    monkeypatch.setattr(
        cleanup_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _FakeJsonResponse(payload),
    )

    result = list_remote_html_attachments(
        relay={"url": "https://relay.test", "token": "token"},
        library_id="LIB1",
        timeout=1,
    )

    assert result == [{"key": "HTML1"}]


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


def test_quarantine_storage_dir_does_not_delete_competing_canonical_directory(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    storage_dir = tmp_path / "Zotero_Test" / "storage" / "ORPHAN1"
    displaced_dir = tmp_path / "displaced-owned"
    storage_dir.mkdir(parents=True)
    (storage_dir / "owner.txt").write_text("owned", encoding="utf-8")
    record = {
        "key": "ORPHAN1",
        "library_id": "LIB1",
        "path": str(storage_dir / "Article [SOURCE HTML].html"),
    }
    original_copytree = cleanup_module.shutil.copytree

    def copy_then_replace_canonical(
        source: Path,
        target: Path,
        *args: object,
        **kwargs: object,
    ) -> Path:
        copied = original_copytree(source, target, *args, **kwargs)
        if storage_dir.exists():
            cleanup_module.os.replace(storage_dir, displaced_dir)
        storage_dir.mkdir()
        (storage_dir / "owner.txt").write_text("competitor", encoding="utf-8")
        return copied

    monkeypatch.setattr(
        cleanup_module.shutil,
        "copytree",
        copy_then_replace_canonical,
    )

    result = quarantine_storage_dir(
        record,
        run_root=tmp_path / "run",
        dry_run=False,
        label="orphan_source_html",
    )

    assert result["ok"] is True
    assert (storage_dir / "owner.txt").read_text(encoding="utf-8") == "competitor"
    assert (Path(result["target"]) / "owner.txt").read_text(encoding="utf-8") == (
        "owned"
    )


def test_quarantine_storage_dir_restores_source_after_copy_cancellation(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    class QuarantineCancelled(BaseException):
        pass

    storage_dir = tmp_path / "Zotero_Test" / "storage" / "ORPHAN1"
    storage_dir.mkdir(parents=True)
    (storage_dir / "owner.txt").write_text("owned", encoding="utf-8")
    record = {
        "key": "ORPHAN1",
        "library_id": "LIB1",
        "path": str(storage_dir / "Article [SOURCE HTML].html"),
    }

    def cancel_partial_copy(
        _source: Path,
        target: Path,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        target.mkdir(parents=True)
        (target / "partial.txt").write_text("partial", encoding="utf-8")
        raise QuarantineCancelled

    monkeypatch.setattr(
        cleanup_module.shutil,
        "copytree",
        cancel_partial_copy,
    )

    with pytest.raises(QuarantineCancelled):
        quarantine_storage_dir(
            record,
            run_root=tmp_path / "run",
            dry_run=False,
            label="orphan_source_html",
        )

    assert (storage_dir / "owner.txt").read_text(encoding="utf-8") == "owned"
    assert not list(tmp_path.rglob("partial.txt"))
    assert not list(storage_dir.parent.glob(".*.quarantine-claim-*"))


def test_quarantine_storage_dir_preserves_copy_and_restores_source_when_cleanup_fails(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    storage_dir = tmp_path / "Zotero_Test" / "storage" / "ORPHAN1"
    storage_dir.mkdir(parents=True)
    (storage_dir / "owner.txt").write_text("owned", encoding="utf-8")
    record = {
        "key": "ORPHAN1",
        "library_id": "LIB1",
        "path": str(storage_dir / "Article [SOURCE HTML].html"),
    }
    original_rmtree = cleanup_module.shutil.rmtree

    def fail_owned_cleanup(path: Path, *args: object, **kwargs: object) -> None:
        if Path(path).name.startswith(".ORPHAN1.quarantine-claim-"):
            raise PermissionError("claim cleanup blocked")
        original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(cleanup_module.shutil, "rmtree", fail_owned_cleanup)

    with pytest.raises(PermissionError, match="claim cleanup blocked") as captured:
        quarantine_storage_dir(
            record,
            run_root=tmp_path / "run",
            dry_run=False,
            label="orphan_source_html",
        )

    target = (
        tmp_path
        / "run"
        / "storage_quarantine"
        / "orphan_source_html"
        / "LIB1"
        / "ORPHAN1"
    )
    assert (storage_dir / "owner.txt").read_text(encoding="utf-8") == "owned"
    assert (target / "owner.txt").read_text(encoding="utf-8") == "owned"
    assert not list(storage_dir.parent.glob(".*.quarantine-claim-*"))
    notes = getattr(captured.value, "__notes__", [])
    assert any(str(target) in note for note in notes)


def test_trash_stale_arxiv_html_dry_run_does_not_need_relay() -> None:
    result = trash_stale_arxiv_html(
        {"key": "ARXIVOLD", "library_id": "LIB1"},
        relay={},
        dry_run=True,
        delete_webdav=True,
        timeout=1,
        deduplication_prefix="test",
    )

    assert result == {
        "ok": True,
        "dryRun": True,
        "wouldTrash": True,
        "deleteWebdav": True,
    }


def test_trash_stale_arxiv_html_rejects_non_object_json(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        cleanup_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _FakeJsonResponse([]),
    )

    with pytest.raises(RuntimeError, match="trash response must be a JSON object"):
        trash_stale_arxiv_html(
            {"key": "ARXIVOLD", "library_id": "LIB1"},
            relay={"url": "https://relay.test", "token": "token"},
            dry_run=False,
            delete_webdav=True,
            timeout=1,
            deduplication_prefix="test",
        )


def test_load_audit_rejects_non_object_json(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.json"
    audit_path.write_text("[]", encoding="utf-8")

    with pytest.raises(RuntimeError, match="audit JSON must contain an object"):
        cleanup_module._load_or_run_audit(
            audit_path,
            run_root=tmp_path / "run",
        )


def test_load_audit_rejects_oversized_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "audit.json"
    audit_path.write_text('{"payload":"oversized"}', encoding="utf-8")
    monkeypatch.setattr(
        cleanup_module,
        "MAX_AUDIT_JSON_BYTES",
        8,
        raising=False,
    )

    with pytest.raises(OSError, match="exceeds 8 bytes"):
        cleanup_module._load_or_run_audit(
            audit_path,
            run_root=tmp_path / "run",
        )


def test_live_audit_rejects_non_object_result(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(cleanup_module.bulk, "_relay_env", lambda: {})
    monkeypatch.setattr(cleanup_module.bulk, "_relay_bindings", lambda _relay: [])
    monkeypatch.setattr(cleanup_module, "run_audit", lambda **_kwargs: [])

    with pytest.raises(RuntimeError, match="audit runner must return an object"):
        cleanup_module._load_or_run_audit(
            None,
            run_root=tmp_path / "run",
        )


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


def test_mark_local_attachment_deleted_marks_deleted_item(tmp_path: Path) -> None:
    db = tmp_path / "zotero.sqlite"
    connection = sqlite3.connect(db)
    try:
        connection.executescript(
            """
            create table items (
              itemID integer primary key,
              key text,
              version int not null default 0,
              synced int not null default 0
            );
            create table deletedItems (
              itemID integer primary key,
              dateDeleted text not null default CURRENT_TIMESTAMP
            );
            """
        )
        connection.execute(
            "insert into items (itemID, key, version, synced) values (1, 'ARXIVOLD', 10, 0)"
        )
        connection.commit()
    finally:
        connection.close()

    result = mark_local_attachment_deleted(
        {"key": "ARXIVOLD"},
        binding=SimpleNamespace(host_data_dir=tmp_path),
        relay_result={
            "ok": True,
            "operation": "trash_attachment",
            "attachmentKey": "ARXIVOLD",
            "dryRun": False,
            "newVersion": 42,
        },
    )

    connection = sqlite3.connect(db)
    try:
        row = connection.execute(
            """
            select i.version, i.synced, di.itemID as deletedItemID
            from items i left join deletedItems di on di.itemID = i.itemID
            where i.key = 'ARXIVOLD'
            """
        ).fetchone()
    finally:
        connection.close()

    assert result["ok"] is True
    assert result["updated"] is True
    assert row == (42, 1, 1)


@pytest.mark.parametrize(
    "relay_result",
    [
        {
            "ok": "true",
            "operation": "trash_attachment",
            "attachmentKey": "ARXIVOLD",
            "dryRun": False,
            "newVersion": 42,
        },
        {
            "ok": True,
            "operation": "trash_attachment",
            "attachmentKey": "OTHER",
            "dryRun": False,
            "newVersion": 42,
        },
        {
            "ok": True,
            "operation": "trash_attachment",
            "attachmentKey": "ARXIVOLD",
            "dryRun": True,
            "newVersion": 42,
        },
        {
            "ok": True,
            "operation": "trash_attachment",
            "attachmentKey": "ARXIVOLD",
            "dryRun": False,
            "newVersion": "42",
        },
    ],
    ids=["truthy-ok", "wrong-key", "dry-run", "coerced-version"],
)
def test_mark_local_attachment_deleted_rejects_malformed_relay_contract(
    tmp_path: Path,
    relay_result: dict[str, object],
) -> None:
    db = tmp_path / "zotero.sqlite"
    connection = sqlite3.connect(db)
    try:
        connection.executescript(
            """
            create table items (itemID integer primary key, key text, version int, synced int);
            create table deletedItems (itemID integer primary key, dateDeleted text);
            insert into items (itemID, key, version, synced) values (1, 'ARXIVOLD', 10, 0);
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="relay trash result contract"):
        mark_local_attachment_deleted(
            {"key": "ARXIVOLD"},
            binding=SimpleNamespace(host_data_dir=tmp_path),
            relay_result=relay_result,
        )

    connection = sqlite3.connect(db)
    try:
        assert connection.execute("select version, synced from items").fetchone() == (
            10,
            0,
        )
        assert connection.execute("select count(*) from deletedItems").fetchone() == (
            0,
        )
    finally:
        connection.close()


def test_mark_local_attachment_deleted_does_not_create_missing_sqlite(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "zotero.sqlite"

    with pytest.raises(RuntimeError, match="zotero.sqlite"):
        mark_local_attachment_deleted(
            {"key": "ARXIVOLD"},
            binding=SimpleNamespace(host_data_dir=tmp_path),
            relay_result={
                "ok": True,
                "operation": "trash_attachment",
                "attachmentKey": "ARXIVOLD",
                "dryRun": False,
                "newVersion": 42,
            },
        )

    assert not sqlite_path.exists()


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


def test_apply_does_not_mutate_local_state_after_malformed_relay_success(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    storage_dir = tmp_path / "Zotero" / "storage" / "ARXIVOLD"
    storage_dir.mkdir(parents=True)
    html_path = storage_dir / "Article [ARXIV HTML].html"
    html_path.write_text("<html></html>", encoding="utf-8")
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(
        json.dumps(
            {
                "all_records": [
                    {
                        "key": "ARXIVOLD",
                        "library_id": "LIB1",
                        "path": str(html_path),
                        "is_source_html": False,
                        "is_arxiv_html": True,
                        "issues": ["stale_arxiv_html_attachment"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    local_mutations: list[str] = []
    monkeypatch.setattr(cleanup_module.bulk, "_relay_env", lambda: {})
    monkeypatch.setattr(cleanup_module.bulk, "_relay_bindings", lambda _relay: [])
    monkeypatch.setattr(
        cleanup_module,
        "trash_stale_arxiv_html",
        lambda *_args, **_kwargs: {"ok": "true", "newVersion": 42},
    )
    monkeypatch.setattr(
        cleanup_module,
        "mark_local_attachment_deleted",
        lambda *_args, **_kwargs: local_mutations.append("sqlite") or {"ok": True},
    )
    monkeypatch.setattr(
        cleanup_module,
        "quarantine_storage_dir",
        lambda *_args, **_kwargs: local_mutations.append("quarantine") or {"ok": True},
    )

    exit_code = cleanup_module.main(
        [
            "--audit-json",
            str(audit_path),
            "--output-root",
            str(tmp_path / "run"),
            "--apply",
            "--confirm",
            "--skip-remote-arxiv-check",
        ]
    )

    assert exit_code == 1
    assert local_mutations == []
    assert html_path.exists()
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


def test_apply_fails_when_local_quarantine_is_not_exact_success(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    storage_dir = tmp_path / "Zotero" / "storage" / "ARXIVOLD"
    storage_dir.mkdir(parents=True)
    html_path = storage_dir / "Article [ARXIV HTML].html"
    html_path.write_text("<html></html>", encoding="utf-8")
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(
        json.dumps(
            {
                "all_records": [
                    {
                        "key": "ARXIVOLD",
                        "library_id": "LIB1",
                        "path": str(html_path),
                        "is_source_html": False,
                        "is_arxiv_html": True,
                        "issues": ["stale_arxiv_html_attachment"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    quarantine_results = iter(({"ok": "true"}, {"ok": False}))
    monkeypatch.setattr(cleanup_module.bulk, "_relay_env", lambda: {})
    monkeypatch.setattr(cleanup_module.bulk, "_relay_bindings", lambda _relay: [])
    monkeypatch.setattr(
        cleanup_module,
        "trash_stale_arxiv_html",
        lambda *_args, **_kwargs: {"ok": True, "newVersion": 42},
    )
    monkeypatch.setattr(
        cleanup_module,
        "quarantine_storage_dir",
        lambda *_args, **_kwargs: next(quarantine_results),
    )

    for attempt in range(2):
        exit_code = cleanup_module.main(
            [
                "--audit-json",
                str(audit_path),
                "--output-root",
                str(tmp_path / f"run_{attempt}"),
                "--apply",
                "--confirm",
                "--skip-remote-arxiv-check",
            ]
        )

        assert exit_code == 1

    assert html_path.exists()


def test_cleanup_fails_closed_when_remote_inventory_fails(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps({"all_records": []}), encoding="utf-8")
    output_root = tmp_path / "run"
    monkeypatch.setattr(cleanup_module.bulk, "_relay_env", lambda: {})
    monkeypatch.setattr(cleanup_module.bulk, "_relay_bindings", lambda _relay: [])

    def fail_inventory(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise TimeoutError("relay inventory timed out")

    monkeypatch.setattr(
        cleanup_module,
        "find_remote_stale_arxiv_html_records",
        fail_inventory,
    )

    exit_code = cleanup_module.main(
        [
            "--audit-json",
            str(audit_path),
            "--output-root",
            str(output_root),
        ]
    )

    manifest = json.loads((output_root / "manifest.json").read_text(encoding="utf-8"))
    assert exit_code == 1
    assert manifest["ok"] is False
    assert manifest["remote_stale_arxiv_check"]["ok"] is False
    assert manifest["aborted_reason"] == "remote_stale_arxiv_check_failed"


def test_targeted_repolish_streams_logs_and_honors_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, Any] = {}

    class FakeProcess:
        pid = 1234

        def wait(self, timeout: int) -> int:
            observed["timeout"] = timeout
            return 0

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        observed["command"] = command
        observed["kwargs"] = kwargs
        kwargs["stdout"].write(b"x" * 6000 + b"STDOUT-END")
        kwargs["stderr"].write(b"y" * 6000 + b"STDERR-END")
        kwargs["stdout"].flush()
        kwargs["stderr"].flush()
        return FakeProcess()

    monkeypatch.setattr(cleanup_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        cleanup_module,
        "_source_recovery_env",
        lambda env: env,
    )

    def unexpected_run(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError(
            "targeted repolish must not capture unbounded subprocess output"
        )

    monkeypatch.setattr(cleanup_module.subprocess, "run", unexpected_run)
    output_root = tmp_path / "repolish"

    result = cleanup_module.run_targeted_repolish(
        keys=["KEY2", "KEY1", "KEY2"],
        output_root=output_root,
        dry_run=False,
        request_timeout=30,
        timeout_seconds=17,
    )

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["returncode"] == 0
    assert result["timeout_seconds"] == 17
    assert observed["timeout"] == 17
    assert observed["command"].count("KEY1") == 1
    assert observed["command"].count("KEY2") == 1
    assert Path(result["stdout_path"]).read_bytes().endswith(b"STDOUT-END")
    assert Path(result["stderr_path"]).read_bytes().endswith(b"STDERR-END")
    assert result["stdout_tail"].endswith("STDOUT-END")
    assert result["stderr_tail"].endswith("STDERR-END")
    assert len(result["stdout_tail"].encode("utf-8")) <= 4096
    assert len(result["stderr_tail"].encode("utf-8")) <= 4096


def test_targeted_repolish_terminates_process_tree_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    command = ["python", "bulk.py"]
    process = SimpleNamespace(pid=4321)

    def wait(*, timeout: int) -> int:
        raise cleanup_module.subprocess.TimeoutExpired(command, timeout)

    process.wait = wait
    monkeypatch.setattr(
        cleanup_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(
        cleanup_module,
        "_source_recovery_env",
        lambda env: env,
    )
    terminated: list[int] = []
    monkeypatch.setattr(
        cleanup_module,
        "_terminate_process_tree",
        lambda candidate: terminated.append(candidate.pid),
    )

    result = cleanup_module.run_targeted_repolish(
        keys=["KEY1"],
        output_root=tmp_path / "repolish-timeout",
        dry_run=False,
        request_timeout=30,
        timeout_seconds=3,
    )

    assert result["ok"] is False
    assert result["status"] == "timeout"
    assert result["timeout_seconds"] == 3
    assert result["returncode"] is None
    assert terminated == [4321]


@pytest.mark.parametrize(
    "operation",
    ["attachments", "children", "trash"],
)
def test_cleanup_relay_responses_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    field = "attachments" if operation == "attachments" else "children"
    payload: dict[str, object]
    if operation == "trash":
        payload = {
            "ok": True,
            "operation": "trash_attachment",
            "attachmentKey": "ARXIVOLD",
            "dryRun": False,
            "newVersion": 2,
        }
    else:
        payload = {"ok": True, field: []}
    payload["padding"] = "x" * (1024 * 1024)
    response = _FakeJsonResponse(payload)
    monkeypatch.setattr(
        cleanup_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: response,
    )

    with pytest.raises(RuntimeError, match="response exceeds"):
        if operation == "attachments":
            cleanup_module.list_remote_html_attachments(
                relay={"url": "https://relay.test", "token": "token"},
                library_id="LIB1",
                timeout=1,
            )
        elif operation == "children":
            cleanup_module.list_remote_item_children(
                relay={"url": "https://relay.test", "token": "token"},
                library_id="LIB1",
                parent_key="PARENT1",
                timeout=1,
            )
        else:
            cleanup_module.trash_stale_arxiv_html(
                {"key": "ARXIVOLD", "library_id": "LIB1"},
                relay={"url": "https://relay.test", "token": "token"},
                dry_run=False,
                delete_webdav=True,
                timeout=1,
                deduplication_prefix="test",
            )

    assert response.read_sizes == [1024 * 1024 + 1]


def test_write_json_preserves_previous_file_when_atomic_replace_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "manifest.json"
    target.write_text('{"old":true}\n', encoding="utf-8")

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(cleanup_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        cleanup_module._write_json(target, {"new": True})

    assert target.read_text(encoding="utf-8") == '{"old":true}\n'
    assert list(tmp_path.glob(".manifest.json.*.tmp")) == []
