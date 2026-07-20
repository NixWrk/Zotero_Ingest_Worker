from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import zotero_ingest_worker.local_attachment_sync as local_sync
from zotero_ingest_worker.local_attachment_sync import (
    patched_sync_cache_json,
    replace_original_file,
    sync_local_zotero_storage_metadata,
    sync_ensured_parent_local,
    sync_parent_attachment_local,
    sync_parent_metadata_local,
    write_new_attachment_local_copy,
)
from zotero_ingest_worker.local_zotero import LocalAttachment, LocalItemMetadata


def test_patched_sync_cache_json_updates_nested_file_metadata() -> None:
    raw = json.dumps(
        {
            "key": "PDF1234",
            "version": 7,
            "data": {"key": "PDF1234", "version": 7, "md5": "old", "mtime": 1000},
        }
    )

    patched = patched_sync_cache_json(
        raw,
        attachment_key="PDF1234",
        version=8,
        storage_hash="new-md5",
        storage_mtime=2000,
    )

    payload = json.loads(str(patched))
    assert payload["version"] == 8
    assert payload["data"]["key"] == "PDF1234"
    assert payload["data"]["version"] == 8
    assert payload["data"]["md5"] == "new-md5"
    assert payload["data"]["mtime"] == 2000


def test_replace_original_file_overwrites_pdf(tmp_path: Path) -> None:
    original = tmp_path / "paper.pdf"
    output = tmp_path / "paper.ocr.pdf"
    original.write_bytes(b"old")
    output.write_bytes(b"new")

    replace_original_file(source_path=original, output_pdf=output)

    assert original.read_bytes() == b"new"
    assert not (tmp_path / ".paper.pdf.ocr-tmp").exists()


def _attachment(tmp_path: Path) -> LocalAttachment:
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir(exist_ok=True)
    return LocalAttachment(
        library_id="LIB1",
        data_dir=tmp_path,
        storage_dir=storage_dir,
        key="PDF1234",
        item_id=20,
        parent_item_id=10,
        date_modified=None,
        link_mode=0,
        content_type="application/pdf",
        zotero_path="storage:paper.pdf",
        file_path=storage_dir / "PDF1234" / "paper.pdf",
        parent_key="PARENT1",
    )


def _metadata(tmp_path: Path) -> LocalItemMetadata:
    return LocalItemMetadata(
        library_id="LIB1",
        data_dir=tmp_path,
        key="PARENT1",
        item_id=10,
        version=7,
        item_type="journalArticle",
        date_modified=None,
        fields={"title": "Parent"},
        creators=[],
        tags=[],
        collections=[],
        relations=[],
    )


def test_replace_original_file_uses_bounded_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = tmp_path / "paper.pdf"
    output = tmp_path / "paper.ocr.pdf"
    original.write_bytes(b"old")
    output.write_bytes(b"new")
    calls: list[tuple[Path, Path, int | None]] = []

    def bounded_publish(
        source: Path,
        target: Path,
        *,
        max_bytes: int | None,
    ) -> bool:
        calls.append((source, target, max_bytes))
        target.write_bytes(source.read_bytes())
        return True

    monkeypatch.setattr(local_sync, "_publish_bounded_copy_no_clobber", bounded_publish)

    replace_original_file(source_path=original, output_pdf=output)

    assert calls == [(output, original, None)]
    assert original.read_bytes() == b"new"


def test_replace_original_file_does_not_clobber_competing_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = tmp_path / "paper.pdf"
    output = tmp_path / "paper.ocr.pdf"
    original.write_bytes(b"old")
    output.write_bytes(b"new")
    real_link = local_sync.os.link

    def competing_link(
        source: object, target: object, *args: object, **kwargs: object
    ) -> None:
        Path(target).write_bytes(b"foreign")
        real_link(source, target, *args, **kwargs)

    monkeypatch.setattr(local_sync.os, "link", competing_link)

    with pytest.raises(OSError):
        replace_original_file(source_path=original, output_pdf=output)

    assert original.read_bytes() == b"foreign"
    claims = list(tmp_path.glob(".paper.pdf.replace-claim-*"))
    assert len(claims) == 1
    assert claims[0].read_bytes() == b"old"
    assert not list(tmp_path.glob(".paper.pdf.local-publish-*"))


def test_replace_original_file_restores_original_on_base_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = tmp_path / "paper.pdf"
    output = tmp_path / "paper.ocr.pdf"
    original.write_bytes(b"old")
    output.write_bytes(b"new")

    def cancel_publication(*_args: object, **_kwargs: object) -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        local_sync,
        "_publish_bounded_copy_no_clobber",
        cancel_publication,
    )

    with pytest.raises(KeyboardInterrupt):
        replace_original_file(source_path=original, output_pdf=output)

    assert original.read_bytes() == b"old"
    assert not list(tmp_path.glob(".paper.pdf.replace-claim-*"))


def test_write_new_attachment_local_copy_does_not_clobber_competing_new_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "result.pdf"
    source.write_bytes(b"%PDF-result")
    target = tmp_path / "storage" / "NEWPDF1" / "result.pdf"
    real_link = local_sync.os.link

    def competing_link(
        source_path: object, target_path: object, *args: object, **kwargs: object
    ) -> None:
        Path(target_path).write_bytes(b"foreign")
        real_link(source_path, target_path, *args, **kwargs)

    monkeypatch.setattr(local_sync.os, "link", competing_link)

    with pytest.raises(OSError):
        write_new_attachment_local_copy(
            attachment=_attachment(tmp_path),
            source_path=source,
            relay_result={"newAttachmentKey": "NEWPDF1", "filename": "result.pdf"},
            backups_root=tmp_path / "backups",
        )

    assert target.read_bytes() == b"foreign"
    assert not list(target.parent.glob(".result.pdf.local-publish-*"))


def test_write_new_attachment_local_copy_restores_competing_existing_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "result.pdf"
    source.write_bytes(b"%PDF-result")
    target_dir = tmp_path / "storage" / "NEWPDF1"
    target_dir.mkdir(parents=True)
    target = target_dir / "result.pdf"
    target.write_bytes(b"old")
    backups_root = tmp_path / "backups"
    real_rename = local_sync.os.rename
    swapped = False

    def competing_rename(source_path: object, claim_path: object) -> None:
        nonlocal swapped
        if Path(source_path) == target and not swapped:
            swapped = True
            replacement = target.with_name(".foreign-replacement")
            replacement.write_bytes(b"foreign")
            local_sync.os.replace(replacement, target)
        real_rename(source_path, claim_path)

    monkeypatch.setattr(local_sync.os, "rename", competing_rename)

    with pytest.raises(OSError, match="changed before local publication"):
        write_new_attachment_local_copy(
            attachment=_attachment(tmp_path),
            source_path=source,
            relay_result={"newAttachmentKey": "NEWPDF1", "filename": "result.pdf"},
            backups_root=backups_root,
        )

    assert target.read_bytes() == b"foreign"
    assert not list(target.parent.glob(".result.pdf.replace-claim-*"))
    backups = [path for path in backups_root.rglob("*") if path.is_file()]
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"old"


def test_write_new_attachment_local_copy_replaces_existing_target_with_backup(
    tmp_path: Path,
) -> None:
    source = tmp_path / "result.pdf"
    source.write_bytes(b"%PDF-result")
    target_dir = tmp_path / "storage" / "NEWPDF1"
    target_dir.mkdir(parents=True)
    target = target_dir / "result.pdf"
    target.write_bytes(b"old")

    result = write_new_attachment_local_copy(
        attachment=_attachment(tmp_path),
        source_path=source,
        relay_result={"newAttachmentKey": "NEWPDF1", "filename": "result.pdf"},
        backups_root=tmp_path / "backups",
    )

    backup_path = Path(result["backupPath"])
    assert target.read_bytes() == b"%PDF-result"
    assert backup_path.read_bytes() == b"old"
    assert not list(target.parent.glob(".result.pdf.replace-claim-*"))
    assert not list(target.parent.glob(".result.pdf.local-publish-*"))


def test_write_new_attachment_local_copy_restores_existing_target_on_base_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "result.pdf"
    source.write_bytes(b"%PDF-result")
    target_dir = tmp_path / "storage" / "NEWPDF1"
    target_dir.mkdir(parents=True)
    target = target_dir / "result.pdf"
    target.write_bytes(b"old")
    backups_root = tmp_path / "backups"

    def cancel_publication(*_args: object, **_kwargs: object) -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        local_sync,
        "_publish_bounded_copy_no_clobber",
        cancel_publication,
    )

    with pytest.raises(KeyboardInterrupt):
        write_new_attachment_local_copy(
            attachment=_attachment(tmp_path),
            source_path=source,
            relay_result={"newAttachmentKey": "NEWPDF1", "filename": "result.pdf"},
            backups_root=backups_root,
        )

    assert target.read_bytes() == b"old"
    backups = [path for path in backups_root.rglob("*") if path.is_file()]
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"old"
    assert not list(target.parent.glob(".result.pdf.replace-claim-*"))
    assert not list(target.parent.glob(".result.pdf.local-publish-*"))


def test_write_new_attachment_local_copy_rejects_numeric_key(tmp_path: Path) -> None:
    source = tmp_path / "result.pdf"
    source.write_bytes(b"%PDF-result")

    with pytest.raises(RuntimeError, match="newAttachmentKey"):
        write_new_attachment_local_copy(
            attachment=_attachment(tmp_path),
            source_path=source,
            relay_result={"newAttachmentKey": 12345678, "filename": "result.pdf"},
            backups_root=tmp_path / "backups",
        )


def test_write_new_attachment_local_copy_uses_bounded_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "result.pdf"
    source.write_bytes(b"%PDF-result")
    calls: list[tuple[Path, Path, int | None]] = []

    def bounded_publish(
        source_path: Path,
        target_path: Path,
        *,
        max_bytes: int | None,
    ) -> bool:
        calls.append((source_path, target_path, max_bytes))
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(source_path.read_bytes())
        return True

    monkeypatch.setattr(local_sync, "_publish_bounded_copy_no_clobber", bounded_publish)

    result = write_new_attachment_local_copy(
        attachment=_attachment(tmp_path),
        source_path=source,
        relay_result={"newAttachmentKey": "NEWPDF1", "filename": "result.pdf"},
        backups_root=tmp_path / "backups",
    )

    assert calls == [(source, Path(result["path"]), None)]
    assert Path(result["path"]).read_bytes() == b"%PDF-result"


def test_sync_local_metadata_requires_exact_patch_success(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="successful Zotero Web API metadata patch"):
        sync_local_zotero_storage_metadata(
            attachment=_attachment(tmp_path),
            relay_result={
                "webDav": {
                    "ok": True,
                    "md5": "deadbeef",
                    "mtime": 1234,
                    "metadataPatch": {"ok": "true", "newVersion": 8},
                }
            },
        )


def test_sync_local_metadata_requires_exact_webdav_success_before_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ZOTERO_LOCAL_MIRROR", "off")

    with pytest.raises(RuntimeError, match="successful WebDAV upload"):
        sync_local_zotero_storage_metadata(
            attachment=_attachment(tmp_path),
            relay_result={
                "webDav": {
                    "ok": "true",
                    "md5": "deadbeef",
                    "mtime": 1234,
                    "metadataPatch": {"ok": True, "newVersion": 8},
                }
            },
        )

    assert not (tmp_path / "zotero.sqlite").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mtime", "1234"),
        ("mtime", True),
        ("mtime", 1234.5),
        ("mtime", -1),
        ("newVersion", "8"),
        ("newVersion", True),
        ("newVersion", 8.5),
        ("newVersion", -1),
    ],
)
def test_sync_local_metadata_rejects_malformed_numeric_contract_before_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    monkeypatch.setenv("ZOTERO_LOCAL_MIRROR", "off")
    webdav: dict[str, object] = {
        "ok": True,
        "md5": "deadbeef",
        "mtime": 1234,
        "metadataPatch": {"ok": True, "newVersion": 8},
    }
    if field == "mtime":
        webdav["mtime"] = value
    else:
        metadata_patch = webdav["metadataPatch"]
        assert isinstance(metadata_patch, dict)
        metadata_patch["newVersion"] = value

    with pytest.raises(RuntimeError, match="exact non-negative integer"):
        sync_local_zotero_storage_metadata(
            attachment=_attachment(tmp_path),
            relay_result={"webDav": webdav},
        )

    assert not (tmp_path / "zotero.sqlite").exists()


def test_sync_local_metadata_rejects_non_string_md5_before_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ZOTERO_LOCAL_MIRROR", "off")

    with pytest.raises(RuntimeError, match="non-empty string md5"):
        sync_local_zotero_storage_metadata(
            attachment=_attachment(tmp_path),
            relay_result={
                "webDav": {
                    "ok": True,
                    "md5": 12345678,
                    "mtime": 1234,
                    "metadataPatch": {"ok": True, "newVersion": 8},
                }
            },
        )

    assert not (tmp_path / "zotero.sqlite").exists()


def test_sync_parent_attachment_rejects_numeric_relay_key(tmp_path: Path) -> None:
    result = sync_parent_attachment_local(
        metadata=_metadata(tmp_path),
        attachment=_attachment(tmp_path),
        filename="Article [SOURCE HTML].html",
        title="Article [SOURCE HTML]",
        content_type="text/html",
        relay_result={"newAttachmentKey": 12345678, "newAttachmentVersion": 8},
    )

    assert result == {"ok": False, "reason": "invalid_attachment_key"}
    assert not (tmp_path / "zotero.sqlite").exists()


@pytest.mark.parametrize(
    ("relay_result", "field"),
    [
        (
            {"newAttachmentKey": "HTML123", "newAttachmentVersion": "8"},
            "newAttachmentVersion",
        ),
        (
            {"newAttachmentKey": "HTML123", "newAttachmentVersion": True},
            "newAttachmentVersion",
        ),
        (
            {"newAttachmentKey": "HTML123", "newAttachmentVersion": 8.5},
            "newAttachmentVersion",
        ),
        (
            {"newAttachmentKey": "HTML123", "newAttachmentVersion": -1},
            "newAttachmentVersion",
        ),
        (
            {
                "newAttachmentKey": "HTML123",
                "newAttachmentVersion": 8,
                "webDav": "configured",
            },
            "webDav",
        ),
        (
            {
                "newAttachmentKey": "HTML123",
                "newAttachmentVersion": 8,
                "webDav": {"ok": True, "md5": 12345678, "mtime": 1234},
            },
            "webDav.md5",
        ),
        (
            {
                "newAttachmentKey": "HTML123",
                "newAttachmentVersion": 8,
                "webDav": {"ok": True, "md5": "deadbeef", "mtime": "1234"},
            },
            "webDav.mtime",
        ),
        (
            {
                "newAttachmentKey": "HTML123",
                "newAttachmentVersion": 8,
                "webDav": {
                    "ok": "true",
                    "md5": "deadbeef",
                    "mtime": 1234,
                    "metadataPatch": {"ok": True, "newVersion": 8},
                },
            },
            "webDav.ok",
        ),
        (
            {
                "newAttachmentKey": "HTML123",
                "newAttachmentVersion": 8,
                "webDav": {"ok": True, "md5": "deadbeef", "mtime": 1234},
            },
            "webDav.metadataPatch",
        ),
        (
            {
                "newAttachmentKey": "HTML123",
                "newAttachmentVersion": 8,
                "webDav": {
                    "ok": True,
                    "md5": "deadbeef",
                    "mtime": 1234,
                    "metadataPatch": "patched",
                },
            },
            "webDav.metadataPatch",
        ),
        (
            {
                "newAttachmentKey": "HTML123",
                "newAttachmentVersion": 8,
                "webDav": {
                    "ok": True,
                    "md5": "deadbeef",
                    "mtime": 1234,
                    "metadataPatch": {"ok": "true", "newVersion": 8},
                },
            },
            "webDav.metadataPatch.ok",
        ),
        (
            {
                "newAttachmentKey": "HTML123",
                "newAttachmentVersion": 8,
                "webDav": {
                    "ok": True,
                    "md5": "deadbeef",
                    "mtime": 1234,
                    "metadataPatch": {"ok": True, "newVersion": "8"},
                },
            },
            "webDav.metadataPatch.newVersion",
        ),
        (
            {
                "newAttachmentKey": "HTML123",
                "newAttachmentVersion": 8,
                "webDav": {
                    "ok": True,
                    "md5": "deadbeef",
                    "mtime": 1234,
                    "metadataPatch": {"ok": True, "newVersion": 9},
                },
            },
            "webDav.metadataPatch.newVersion",
        ),
    ],
)
def test_sync_parent_attachment_rejects_malformed_contract_before_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    relay_result: dict[str, object],
    field: str,
) -> None:
    monkeypatch.setenv("ZOTERO_LOCAL_MIRROR", "off")

    result = sync_parent_attachment_local(
        metadata=_metadata(tmp_path),
        attachment=_attachment(tmp_path),
        filename="Article [SOURCE HTML].html",
        title="Article [SOURCE HTML]",
        content_type="text/html",
        relay_result=relay_result,
    )

    assert result["ok"] is False
    assert result["reason"] == "invalid_parent_attachment_contract"
    assert result["field"] == field
    assert not (tmp_path / "zotero.sqlite").exists()


def test_sync_parent_attachment_requires_version_before_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ZOTERO_LOCAL_MIRROR", "off")

    result = sync_parent_attachment_local(
        metadata=_metadata(tmp_path),
        attachment=_attachment(tmp_path),
        filename="Article [SOURCE HTML].html",
        title="Article [SOURCE HTML]",
        content_type="text/html",
        relay_result={"newAttachmentKey": "HTML123"},
    )

    assert result == {
        "ok": False,
        "reason": "missing_zotero_version",
        "attachmentKey": "HTML123",
    }
    assert not (tmp_path / "zotero.sqlite").exists()


def test_sync_local_metadata_skips_when_local_mirror_is_off(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ZOTERO_LOCAL_MIRROR", "off")

    result = sync_local_zotero_storage_metadata(
        attachment=_attachment(tmp_path),
        relay_result={
            "webDav": {
                "ok": True,
                "md5": "deadbeef",
                "mtime": 1234,
                "metadataPatch": {"ok": True, "newVersion": 8},
            }
        },
    )

    assert result["ok"] is True
    assert result["updated"] is False
    assert result["reason"] == "local_mirror_disabled"
    assert not (tmp_path / "zotero.sqlite").exists()


def test_sync_parent_attachment_skips_when_zotero_desktop_is_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        local_sync,
        "_zotero_desktop_connector_running",
        lambda: True,
        raising=False,
    )

    result = sync_parent_attachment_local(
        metadata=_metadata(tmp_path),
        attachment=_attachment(tmp_path),
        filename="Article [SOURCE HTML].html",
        title="Article [SOURCE HTML]",
        content_type="text/html",
        relay_result={"newAttachmentKey": "HTML123", "newAttachmentVersion": 8},
    )

    assert result["ok"] is True
    assert result["updated"] is False
    assert result["reason"] == "zotero_desktop_running"
    assert result["attachmentKey"] == "HTML123"
    assert not (tmp_path / "zotero.sqlite").exists()


@pytest.mark.parametrize(
    ("relay_result", "field"),
    [
        ({"ok": "true", "appliedFields": ["title"], "newVersion": 8}, "ok"),
        ({"ok": True, "appliedFields": "title", "newVersion": 8}, "appliedFields"),
        (
            {"ok": True, "appliedFields": ["title", 123], "newVersion": 8},
            "appliedFields",
        ),
        ({"ok": True, "appliedFields": ["title"]}, "newVersion"),
        (
            {"ok": True, "appliedFields": ["title"], "newVersion": "8"},
            "newVersion",
        ),
        (
            {"ok": True, "appliedFields": ["title"], "newVersion": True},
            "newVersion",
        ),
        (
            {"ok": True, "appliedFields": ["title"], "newVersion": 8.5},
            "newVersion",
        ),
        (
            {"ok": True, "appliedFields": ["title"], "newVersion": -1},
            "newVersion",
        ),
    ],
)
def test_sync_parent_metadata_rejects_malformed_contract_before_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    relay_result: dict[str, object],
    field: str,
) -> None:
    monkeypatch.setenv("ZOTERO_LOCAL_MIRROR", "off")

    result = sync_parent_metadata_local(
        metadata=_metadata(tmp_path),
        fields={"title": "Updated title"},
        relay_result=relay_result,
    )

    assert result["ok"] is False
    assert result["updated"] is False
    assert result["reason"] == "invalid_parent_metadata_contract"
    assert result["field"] == field
    assert not (tmp_path / "zotero.sqlite").exists()


@pytest.mark.parametrize(
    ("fields", "relay_result", "field"),
    [
        (
            {"title": "Updated title"},
            {
                "ok": True,
                "appliedFields": ["abstractNote"],
                "newVersion": 8,
            },
            "appliedFields",
        ),
        (
            {"title": 12345678},
            {"ok": True, "appliedFields": ["title"], "newVersion": 8},
            "fields.title",
        ),
    ],
)
def test_sync_parent_metadata_rejects_applied_field_mismatch_before_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fields: dict[str, object],
    relay_result: dict[str, object],
    field: str,
) -> None:
    monkeypatch.setenv("ZOTERO_LOCAL_MIRROR", "off")

    result = sync_parent_metadata_local(
        metadata=_metadata(tmp_path),
        fields=fields,  # type: ignore[arg-type]
        relay_result=relay_result,
    )

    assert result["ok"] is False
    assert result["updated"] is False
    assert result["reason"] == "invalid_parent_metadata_contract"
    assert result["field"] == field
    assert not (tmp_path / "zotero.sqlite").exists()


def test_sync_parent_metadata_accepts_exact_empty_applied_fields(
    tmp_path: Path,
) -> None:
    result = sync_parent_metadata_local(
        metadata=_metadata(tmp_path),
        fields={"title": "Updated title"},
        relay_result={"ok": True, "appliedFields": [], "newVersion": 8},
    )

    assert result == {
        "ok": True,
        "updated": False,
        "reason": "no_applied_fields",
        "item_key": "PARENT1",
    }
    assert not (tmp_path / "zotero.sqlite").exists()


def test_sync_parent_metadata_does_not_create_missing_sqlite(tmp_path: Path) -> None:
    result = sync_parent_metadata_local(
        metadata=_metadata(tmp_path),
        fields={"title": "Updated title"},
        relay_result={
            "ok": True,
            "appliedFields": ["title"],
            "newVersion": 8,
        },
    )

    assert result == {
        "ok": True,
        "updated": False,
        "reason": "sqlite_missing",
        "item_key": "PARENT1",
        "sqlite_path": str(tmp_path / "zotero.sqlite"),
    }
    assert not (tmp_path / "zotero.sqlite").exists()


def _ensured_parent_relay_result() -> dict[str, object]:
    return {
        "ok": True,
        "parentItemKey": "PARENTNEW",
        "alreadyHadParent": False,
        "parentCreated": {
            "key": "PARENTNEW",
            "title": "Standalone paper",
            "itemType": "document",
            "version": 11,
            "collections": [],
        },
        "pdfParentPatch": {
            "ok": True,
            "pdfKey": "PDF1234",
            "parentItemKey": "PARENTNEW",
            "oldVersion": 7,
            "newVersion": 8,
            "clearedCollections": False,
        },
    }


@pytest.mark.parametrize(
    ("case", "field"),
    [
        ("top-level-ok", "ok"),
        ("already-had-parent", "alreadyHadParent"),
        ("parent-key", "parentCreated.key"),
        ("parent-version", "parentCreated.version"),
        ("collections", "parentCreated.collections"),
        ("patch-ok", "pdfParentPatch.ok"),
        ("patch-key", "pdfParentPatch.pdfKey"),
        ("patch-version", "pdfParentPatch.newVersion"),
        ("cleared-collections", "pdfParentPatch.clearedCollections"),
    ],
)
def test_sync_ensured_parent_rejects_malformed_contract_before_mirror_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case: str,
    field: str,
) -> None:
    monkeypatch.setenv("ZOTERO_LOCAL_MIRROR", "off")
    relay_result = _ensured_parent_relay_result()
    parent_created = relay_result["parentCreated"]
    pdf_parent_patch = relay_result["pdfParentPatch"]
    assert isinstance(parent_created, dict)
    assert isinstance(pdf_parent_patch, dict)

    if case == "top-level-ok":
        relay_result["ok"] = "true"
    elif case == "already-had-parent":
        relay_result["alreadyHadParent"] = "false"
    elif case == "parent-key":
        parent_created["key"] = "OTHERPAR"
    elif case == "parent-version":
        parent_created["version"] = "11"
    elif case == "collections":
        parent_created["collections"] = [12345678]
    elif case == "patch-ok":
        pdf_parent_patch["ok"] = "true"
    elif case == "patch-key":
        pdf_parent_patch["pdfKey"] = "OTHERPDF"
    elif case == "patch-version":
        pdf_parent_patch["newVersion"] = 8.5
    elif case == "cleared-collections":
        pdf_parent_patch["clearedCollections"] = 0
    else:
        raise AssertionError(f"Unhandled contract test case: {case}")

    result = sync_ensured_parent_local(
        attachment=_attachment(tmp_path),
        relay_result=relay_result,
    )

    assert result["ok"] is False
    assert result["reason"] == "invalid_parent_preflight_contract"
    assert result["field"] == field
    assert not (tmp_path / "zotero.sqlite").exists()


def test_sync_ensured_existing_parent_allows_absent_creation_payloads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ZOTERO_LOCAL_MIRROR", "off")

    result = sync_ensured_parent_local(
        attachment=_attachment(tmp_path),
        relay_result={
            "ok": True,
            "parentItemKey": "PARENT1",
            "alreadyHadParent": True,
            "parentCreated": None,
            "pdfParentPatch": None,
        },
    )

    assert result["ok"] is True
    assert result["updated"] is False
    assert result["reason"] == "local_mirror_disabled"


def _create_parent_attachment_db(tmp_path: Path, *, sibling_deleted: bool) -> None:
    with sqlite3.connect(tmp_path / "zotero.sqlite") as connection:
        connection.executescript(
            """
            create table items (
                itemID integer primary key,
                itemTypeID int not null,
                dateAdded timestamp not null default current_timestamp,
                dateModified timestamp not null default current_timestamp,
                clientDateModified timestamp not null default current_timestamp,
                libraryID int not null,
                key text not null,
                version int not null default 0,
                synced int not null default 0
            );
            create table itemAttachments (
                itemID integer primary key,
                parentItemID int,
                linkMode int,
                contentType text,
                charsetID int,
                path text,
                syncState int default 0,
                storageModTime int,
                storageHash text,
                lastProcessedModificationTime int,
                lastRead int
            );
            create table itemTypes (
                itemTypeID integer primary key,
                typeName text,
                templateItemTypeID int,
                display int default 1
            );
            create table fields (fieldID integer primary key, fieldName text);
            create table itemDataValues (valueID integer primary key, value text);
            create table itemData (itemID integer, fieldID integer, valueID integer);
            create table deletedItems (itemID integer primary key);
            create table syncCache (
                libraryID int not null,
                key text not null,
                syncObjectTypeID int not null,
                version int not null,
                data text
            );
            insert into itemTypes (itemTypeID, typeName)
                values (3, 'attachment'), (22, 'journalArticle');
            insert into items (itemID, itemTypeID, libraryID, key, version, synced)
                values
                (10, 22, 1, 'PARENT1', 5, 1),
                (20, 3, 1, 'HTMLOLD1', 41, 1);
            insert into itemAttachments
                (itemID, parentItemID, linkMode, contentType, path)
                values (20, 10, 0, 'text/html', 'storage:Article [RU HTML].html');
            """
        )
        if sibling_deleted:
            connection.execute("insert into deletedItems (itemID) values (20)")


def test_sync_parent_metadata_updates_sqlite_and_sync_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        local_sync,
        "_zotero_desktop_connector_running",
        lambda: False,
    )
    _create_parent_attachment_db(tmp_path, sibling_deleted=False)
    sync_payload = {
        "key": "PARENT1",
        "version": 5,
        "data": {
            "key": "PARENT1",
            "version": 5,
            "title": "Parent",
        },
    }
    with sqlite3.connect(tmp_path / "zotero.sqlite") as connection:
        connection.execute(
            "insert into fields (fieldID, fieldName) values (1, 'title')"
        )
        connection.execute(
            "insert into itemDataValues (valueID, value) values (1, 'Parent')"
        )
        connection.execute(
            "insert into itemData (itemID, fieldID, valueID) values (10, 1, 1)"
        )
        connection.execute(
            """
            insert into syncCache (libraryID, key, syncObjectTypeID, version, data)
            values (1, 'PARENT1', 3, 5, ?)
            """,
            (json.dumps(sync_payload),),
        )

    result = sync_parent_metadata_local(
        metadata=_metadata(tmp_path),
        fields={"title": "Updated title"},
        relay_result={
            "ok": True,
            "appliedFields": ["title"],
            "newVersion": 8,
        },
    )

    assert result["ok"] is True
    assert result["updated"] is True
    assert result["zotero_version"] == 8
    assert result["updated_fields"] == ["title"]
    with sqlite3.connect(tmp_path / "zotero.sqlite") as connection:
        item = connection.execute(
            "select version, synced from items where itemID = 10"
        ).fetchone()
        title = connection.execute(
            """
            select values_table.value
            from itemData data
            join fields field on field.fieldID = data.fieldID
            join itemDataValues values_table on values_table.valueID = data.valueID
            where data.itemID = 10 and field.fieldName = 'title'
            """
        ).fetchone()
        cache = connection.execute(
            "select version, data from syncCache where key = 'PARENT1'"
        ).fetchone()

    assert item == (8, 1)
    assert title == ("Updated title",)
    assert cache is not None
    assert cache[0] == 8
    cache_payload = json.loads(cache[1])
    assert cache_payload["version"] == 8
    assert cache_payload["data"]["version"] == 8
    assert cache_payload["data"]["title"] == "Updated title"


@pytest.mark.parametrize(
    ("sibling_deleted", "expected_updated", "expected_key"),
    [
        (False, False, "HTMLOLD1"),
        (True, True, "HTMLNEW1"),
    ],
)
def test_sync_parent_attachment_deduplicates_only_active_sibling_filename(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sibling_deleted: bool,
    expected_updated: bool,
    expected_key: str,
) -> None:
    monkeypatch.setattr(
        local_sync,
        "_zotero_desktop_connector_running",
        lambda: False,
    )
    _create_parent_attachment_db(tmp_path, sibling_deleted=sibling_deleted)

    result = sync_parent_attachment_local(
        metadata=_metadata(tmp_path),
        attachment=_attachment(tmp_path),
        filename="Article [RU HTML].html",
        title="Article [RU HTML]",
        content_type="text/html",
        relay_result={
            "newAttachmentKey": "HTMLNEW1",
            "newAttachmentVersion": 42,
            "webDav": {
                "ok": True,
                "md5": "deadbeef",
                "mtime": 1234567890,
                "metadataPatch": {"ok": True, "newVersion": 42},
            },
        },
    )

    assert result["ok"] is True
    assert result["updated"] is expected_updated
    assert result["attachmentKey"] == expected_key
    if not sibling_deleted:
        assert result["reason"] == "html_sibling_already_exists"
        assert result["requestedAttachmentKey"] == "HTMLNEW1"

    with sqlite3.connect(tmp_path / "zotero.sqlite") as connection:
        new_item = connection.execute(
            "select itemID from items where key = 'HTMLNEW1'"
        ).fetchone()
        attachment_count = connection.execute(
            "select count(*) from itemAttachments"
        ).fetchone()[0]

    assert (new_item is not None) is sibling_deleted
    assert attachment_count == (2 if sibling_deleted else 1)
