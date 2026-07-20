from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import scripts.bulk_repolish_source_html as bulk


EXPECTED_MAX_RELAY_RESPONSE_BYTES = 1024 * 1024


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.read_sizes: list[int] = []
        self.request_data: bytes | None = None

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if size < 0:
            return self.payload
        return self.payload[:size]


def _attachment(tmp_path: Path) -> bulk.SourceAttachment:
    binding = bulk.RelayBinding(
        library_id="18870990",
        name="test",
        host_data_dir=tmp_path,
        container_data_dir="/data",
    )
    return bulk.SourceAttachment(
        binding=binding,
        item_id=1,
        key="ABC12345",
        version=4,
        local_library_id=1,
        parent_key="PARENT01",
        title="[SOURCE HTML] Example",
        zotero_path="storage:paper.html",
        host_path=tmp_path / "storage" / "ABC12345" / "paper.html",
        container_path="/data/storage/ABC12345/paper.html",
        source_url="https://example.test/paper",
    )


def _valid_relay_result() -> dict[str, Any]:
    return {
        "ok": True,
        "operationId": 7,
        "attachmentKey": "ABC12345",
        "strategy": "webdav_only",
        "dryRun": False,
        "webDav": {
            "ok": True,
            "filename": "paper.html",
            "md5": "0123456789abcdef0123456789abcdef",
            "mtime": 1_720_000_000_000,
            "metadataPatch": {
                "ok": True,
                "previousVersion": 4,
                "newVersion": 5,
            },
        },
    }


def test_completed_keys_streams_and_ignores_non_object_records(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    results_path = tmp_path / "results.jsonl"
    results_path.write_text(
        '[]\n{"ok":true,"key":"DONE1234"}\n',
        encoding="utf-8",
    )

    def reject_unbounded_read(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("resume reader must stream JSONL")

    monkeypatch.setattr(Path, "read_text", reject_unbounded_read)

    assert bulk._completed_keys(results_path) == {"DONE1234"}


def _relay_replace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: object,
) -> tuple[dict[str, Any], _Response]:
    response = _Response(json.dumps(payload).encode("utf-8"))

    def urlopen(request: Any, **_kwargs: Any) -> _Response:
        response.request_data = request.data
        return response

    monkeypatch.setattr(bulk.urllib.request, "urlopen", urlopen)
    result = bulk._relay_replace(
        relay={"url": "http://127.0.0.1:23118", "token": "secret"},
        attachment=_attachment(tmp_path),
        source_path="/data/storage/ABC12345/paper.html",
        expected_old_sha256="f" * 64,
        deduplication_key="repolish:ABC12345:hash",
        timeout=30,
    )
    return result, response


def test_relay_replace_accepts_exact_success_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected = _valid_relay_result()

    result, response = _relay_replace(monkeypatch, tmp_path, expected)

    assert result == expected
    assert response.read_sizes == [EXPECTED_MAX_RELAY_RESPONSE_BYTES + 1]
    assert response.request_data is not None
    request_payload = json.loads(response.request_data)
    assert request_payload["expectedVersion"] == 4
    assert request_payload["expectedOldSha256"] == "f" * 64


@pytest.mark.parametrize(
    "case",
    [
        "not_mapping",
        "ok_not_boolean",
        "not_ok",
        "wrong_attachment",
        "wrong_strategy",
        "dry_run_not_boolean",
        "dry_run",
        "webdav_not_mapping",
        "webdav_not_ok",
        "md5_not_string",
        "mtime_not_exact_int",
        "metadata_patch_missing",
        "metadata_patch_not_ok",
        "new_version_not_exact_int",
    ],
)
def test_relay_replace_rejects_malformed_success_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case: str,
) -> None:
    payload: object = copy.deepcopy(_valid_relay_result())
    if case == "not_mapping":
        payload = []
    else:
        assert isinstance(payload, dict)
        webdav = payload["webDav"]
        assert isinstance(webdav, dict)
        metadata_patch = webdav["metadataPatch"]
        assert isinstance(metadata_patch, dict)
        if case == "ok_not_boolean":
            payload["ok"] = 1
        elif case == "not_ok":
            payload["ok"] = False
        elif case == "wrong_attachment":
            payload["attachmentKey"] = "OTHER123"
        elif case == "wrong_strategy":
            payload["strategy"] = "local_only"
        elif case == "dry_run_not_boolean":
            payload["dryRun"] = 0
        elif case == "dry_run":
            payload["dryRun"] = True
        elif case == "webdav_not_mapping":
            payload["webDav"] = []
        elif case == "webdav_not_ok":
            webdav["ok"] = False
        elif case == "md5_not_string":
            webdav["md5"] = 123
        elif case == "mtime_not_exact_int":
            webdav["mtime"] = True
        elif case == "metadata_patch_missing":
            webdav.pop("metadataPatch")
        elif case == "metadata_patch_not_ok":
            metadata_patch["ok"] = False
        elif case == "new_version_not_exact_int":
            metadata_patch["newVersion"] = "5"

    with pytest.raises(RuntimeError, match="relay"):
        _relay_replace(monkeypatch, tmp_path, payload)


def test_relay_replace_rejects_oversized_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = _valid_relay_result()
    payload["padding"] = "x" * EXPECTED_MAX_RELAY_RESPONSE_BYTES
    response = _Response(json.dumps(payload).encode("utf-8"))
    monkeypatch.setattr(
        bulk.urllib.request, "urlopen", lambda *_args, **_kwargs: response
    )

    with pytest.raises(RuntimeError, match="response exceeds"):
        bulk._relay_replace(
            relay={"url": "http://127.0.0.1:23118", "token": "secret"},
            attachment=_attachment(tmp_path),
            source_path="/data/storage/ABC12345/paper.html",
            expected_old_sha256="f" * 64,
            deduplication_key="repolish:ABC12345:hash",
            timeout=30,
        )

    assert response.read_sizes == [EXPECTED_MAX_RELAY_RESPONSE_BYTES + 1]


@pytest.mark.parametrize("value", [True, False, -1, 1.5, b"2", "-2", "+2", "2.0"])
def test_optional_int_rejects_noncanonical_values(value: object) -> None:
    assert bulk._optional_int(value) is None


@pytest.mark.parametrize("value, expected", [(0, 0), (42, 42), (" 42 ", 42)])
def test_optional_int_accepts_exact_nonnegative_values(
    value: object, expected: int
) -> None:
    assert bulk._optional_int(value) == expected


def _create_local_sqlite(
    tmp_path: Path,
    *,
    key: str = "ABC12345",
    version: int = 4,
    include_attachment: bool = True,
) -> Path:
    sqlite_path = tmp_path / "zotero.sqlite"
    connection = sqlite3.connect(sqlite_path)
    try:
        connection.executescript(
            """
            create table items (
              itemID integer primary key,
              libraryID integer not null,
              key text not null,
              version integer not null,
              synced integer not null
            );
            create table itemAttachments (
              itemID integer primary key,
              storageHash text,
              storageModTime integer,
              syncState integer
            );
            create table syncCache (
              libraryID integer not null,
              key text not null,
              syncObjectTypeID integer not null,
              version integer not null,
              data text
            );
            """
        )
        if include_attachment:
            connection.execute(
                "insert into items values (1, 1, ?, ?, 0)",
                (key, version),
            )
            connection.execute("insert into itemAttachments values (1, 'old', 1, 0)")
            connection.execute(
                "insert into syncCache values (1, ?, 3, ?, ?)",
                (
                    key,
                    version,
                    json.dumps({"version": version, "data": {"key": key}}),
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return sqlite_path


def test_sync_local_storage_metadata_updates_exact_identity(tmp_path: Path) -> None:
    sqlite_path = _create_local_sqlite(tmp_path)

    result = bulk._sync_local_storage_metadata(
        attachment=_attachment(tmp_path),
        relay_result=_valid_relay_result(),
    )

    assert result["ok"] is True
    assert result["updated"] is True
    connection = sqlite3.connect(sqlite_path)
    try:
        item = connection.execute(
            "select version, synced from items where itemID = 1"
        ).fetchone()
        storage = connection.execute(
            "select storageHash, storageModTime, syncState from itemAttachments where itemID = 1"
        ).fetchone()
    finally:
        connection.close()
    assert item == (5, 1)
    assert storage == (
        "0123456789abcdef0123456789abcdef",
        1_720_000_000_000,
        2,
    )


@pytest.mark.parametrize(
    "key, version, include_attachment",
    [
        ("ABC12345", 6, True),
        ("OTHER123", 4, True),
        ("ABC12345", 4, False),
    ],
)
def test_sync_local_storage_metadata_rejects_stale_or_missing_identity(
    tmp_path: Path,
    key: str,
    version: int,
    include_attachment: bool,
) -> None:
    sqlite_path = _create_local_sqlite(
        tmp_path,
        key=key,
        version=version,
        include_attachment=include_attachment,
    )

    with pytest.raises(RuntimeError, match="attachment|version|identity"):
        bulk._sync_local_storage_metadata(
            attachment=_attachment(tmp_path),
            relay_result=_valid_relay_result(),
        )

    connection = sqlite3.connect(sqlite_path)
    try:
        rows = connection.execute(
            "select key, version, synced from items order by itemID"
        ).fetchall()
    finally:
        connection.close()
    assert rows in ([], [(key, version, 0)])


def test_sync_local_storage_metadata_rejects_malformed_relay_result(
    tmp_path: Path,
) -> None:
    _create_local_sqlite(tmp_path)

    with pytest.raises(RuntimeError, match="relay"):
        bulk._sync_local_storage_metadata(
            attachment=_attachment(tmp_path),
            relay_result={"ok": True},
        )
