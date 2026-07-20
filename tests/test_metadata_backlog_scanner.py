from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from zotero_ingest_worker import metadata_backlog_scanner as scanner


class _FakeState:
    @staticmethod
    def metadata_queue_summary(*, job_type: str) -> dict[str, object]:
        return {"job_type": job_type, "queued": 0}


class _FakeStore:
    def __init__(
        self,
        *,
        items: list[SimpleNamespace] | None = None,
        attachments: list[SimpleNamespace] | None = None,
    ) -> None:
        self.items = items or []
        self.attachments = attachments or []
        self.attachment_limits: list[int | None] = []

    def iter_regular_items(
        self,
        *,
        max_items: int | None,
        collection: str | None,
        only_keys: set[str] | None,
    ) -> list[SimpleNamespace]:
        del collection
        values = self.items
        if only_keys is not None:
            values = [item for item in values if item.key in only_keys]
        return values if max_items is None else values[:max_items]

    def item_full_text_inventory(self, _metadata: object) -> dict[str, object]:
        return {"has_pdf": False, "has_html": False, "has_source_html": False}

    def iter_pdf_attachments(self, *, max_items: int | None) -> list[SimpleNamespace]:
        self.attachment_limits.append(max_items)
        return self.attachments if max_items is None else self.attachments[:max_items]

    def iter_collection_pdf_attachments(
        self,
        *,
        collection: str,
        max_items: int | None,
    ) -> list[SimpleNamespace]:
        del collection
        return self.iter_pdf_attachments(max_items=max_items)


def _item(key: str, data_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        key=key,
        library_id=data_dir.name,
        item_type="journalArticle",
        title=key,
        data_dir=data_dir,
        version=1,
    )


def _attachment(key: str) -> SimpleNamespace:
    return SimpleNamespace(key=key, parent_key=key)


def _processor(
    configs: list[SimpleNamespace],
    *,
    enqueue_parent: Callable[..., dict[str, Any]] | None = None,
    enqueue_attachment: Callable[..., dict[str, Any]] | None = None,
    enqueue_scihub: Callable[..., dict[str, Any]] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(validate_for_scan=lambda: None),
        state=_FakeState(),
        _library_configs=lambda **_kwargs: configs,
        _enqueue_parent_full_text_item=(
            enqueue_parent
            or (
                lambda **kwargs: {
                    "parent_item_key": kwargs["metadata"].key,
                    "job": {"created": False, "status": "queued"},
                }
            )
        ),
        _enqueue_attachment=(
            enqueue_attachment
            or (
                lambda **kwargs: {
                    "attachment_key": kwargs["attachment"].key,
                    "job": {"created": False, "status": "queued"},
                }
            )
        ),
        _enqueue_scihub_pdf_jobs_for_item=(
            enqueue_scihub
            or (
                lambda **kwargs: {
                    "parent_item_key": kwargs["metadata"].key,
                    "queued": 0,
                }
            )
        ),
    )


def _config(data_dir: Path) -> SimpleNamespace:
    data_dir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        zotero_data_dir=data_dir,
        zotero_path_prefix_map=(),
    )


def _install_stores(
    monkeypatch: pytest.MonkeyPatch,
    stores: dict[Path, _FakeStore],
) -> None:
    monkeypatch.setattr(
        scanner,
        "LocalZoteroStore",
        lambda config: stores[Path(config.zotero_data_dir)],
    )


def _full_text_scan(
    processor: SimpleNamespace,
    *,
    max_items: int | None,
    limit: int | None = None,
    filters: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    return scanner.full_text_backlog_scan(
        processor,
        max_items=max_items,
        limit=limit,
        force=True,
        library_id=None,
        data_dir=None,
        collection=None,
        only_parent_keys_by_library=filters,
    )


def _attachment_scan(
    processor: SimpleNamespace,
    *,
    max_items: int | None,
    limit: int | None = None,
    filters: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    return scanner.attachment_backlog_scan(
        processor,
        job_type="enrich",
        max_items=max_items,
        limit=limit,
        force=False,
        library_id=None,
        data_dir=None,
        collection=None,
        only_parent_keys_by_library=filters,
    )


def test_full_text_max_items_is_global_across_libraries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first_config = _config(tmp_path / "library-1")
    second_config = _config(tmp_path / "library-2")
    stores = {
        first_config.zotero_data_dir: _FakeStore(
            items=[
                _item("A1", first_config.zotero_data_dir),
                _item("A2", first_config.zotero_data_dir),
            ]
        ),
        second_config.zotero_data_dir: _FakeStore(
            items=[
                _item("B1", second_config.zotero_data_dir),
                _item("B2", second_config.zotero_data_dir),
            ]
        ),
    }
    _install_stores(monkeypatch, stores)
    processor = _processor([first_config, second_config])

    result = _full_text_scan(processor, max_items=3)

    assert result["scanned"] == 3
    assert [entry["parent_item_key"] for entry in result["results"]] == [
        "A1",
        "A2",
        "B1",
    ]


def test_backlog_results_are_bounded_even_when_every_item_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "library")
    item_count = 1_005
    store = _FakeStore(
        items=[
            _item(f"P{index:04d}", config.zotero_data_dir)
            for index in range(item_count)
        ]
    )
    _install_stores(monkeypatch, {config.zotero_data_dir: store})
    processor = _processor([config])

    result = _full_text_scan(processor, max_items=None, limit=1)

    assert result["scanned"] == item_count
    assert result["queued"] == 0
    assert len(result["results"]) == 1_000
    assert result["results_truncated"] is True
    assert result["omitted_results"] == 5


@pytest.mark.parametrize("queued", [True, "1", 1.0, -1, 2, None])
def test_scihub_scan_rejects_malformed_queued_count(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    queued: object,
) -> None:
    config = _config(tmp_path / "library")
    store = _FakeStore(items=[_item("P1", config.zotero_data_dir)])
    _install_stores(monkeypatch, {config.zotero_data_dir: store})
    processor = _processor(
        [config], enqueue_scihub=lambda **_kwargs: {"queued": queued}
    )

    with pytest.raises(RuntimeError, match="queued"):
        scanner.scihub_pdf_backlog_scan(
            processor,
            max_items=1,
            limit=1,
            force=False,
            library_id=None,
            data_dir=None,
            collection=None,
        )


def test_attachment_scan_rejects_truthy_non_boolean_created_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "library")
    store = _FakeStore(attachments=[_attachment("P1")])
    _install_stores(monkeypatch, {config.zotero_data_dir: store})
    processor = _processor(
        [config],
        enqueue_attachment=lambda **_kwargs: {
            "job": {"created": "true", "status": "queued"}
        },
    )

    with pytest.raises(RuntimeError, match="created"):
        _attachment_scan(processor, max_items=1)


def test_parent_filter_rejects_non_string_keys_before_scan(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "library")
    processor = _processor([config])

    with pytest.raises(ValueError, match="parent keys"):
        _full_text_scan(
            processor,
            max_items=1,
            filters={"REMOTE": [True]},  # type: ignore[list-item]
        )


def test_malformed_binding_json_fails_with_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "library")
    processor = _processor([config])
    monkeypatch.setenv("ZFR_LIBRARY_BINDINGS", "{not-json")

    with pytest.raises(ValueError, match="ZFR_LIBRARY_BINDINGS"):
        _full_text_scan(processor, max_items=1, filters={"REMOTE": ["P1"]})


def test_attachment_filter_does_not_hide_allowed_key_behind_local_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "library")
    store = _FakeStore(attachments=[_attachment("OTHER"), _attachment("TARGET")])
    _install_stores(monkeypatch, {config.zotero_data_dir: store})
    processor = _processor([config])
    monkeypatch.setenv(
        "ZFR_LIBRARY_BINDINGS",
        json.dumps([{"libraryId": "REMOTE", "dataDir": str(config.zotero_data_dir)}]),
    )

    result = _attachment_scan(
        processor,
        max_items=1,
        filters={"REMOTE": ["TARGET"]},
    )

    assert result["scanned"] == 1
    assert result["results"][0]["attachment_key"] == "TARGET"
    assert store.attachment_limits == [None]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_items", False),
        ("max_items", 0.0),
        ("max_items", -1),
        ("max_items", "1"),
        ("limit", False),
        ("limit", 0.0),
        ("limit", -1),
        ("limit", "1"),
    ],
)
def test_backlog_scan_rejects_malformed_or_negative_budgets(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    config = _config(tmp_path / "library")
    processor = _processor([config])
    kwargs: dict[str, object] = {"max_items": 1, "limit": 1}
    kwargs[field] = value

    with pytest.raises(ValueError, match=field):
        _full_text_scan(
            processor,
            max_items=kwargs["max_items"],  # type: ignore[arg-type]
            limit=kwargs["limit"],  # type: ignore[arg-type]
        )


def test_attachment_max_items_is_global_across_libraries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first_config = _config(tmp_path / "library-1")
    second_config = _config(tmp_path / "library-2")
    stores = {
        first_config.zotero_data_dir: _FakeStore(
            attachments=[_attachment("A1"), _attachment("A2")]
        ),
        second_config.zotero_data_dir: _FakeStore(
            attachments=[_attachment("B1"), _attachment("B2")]
        ),
    }
    _install_stores(monkeypatch, stores)
    processor = _processor([first_config, second_config])

    result = _attachment_scan(processor, max_items=3)

    assert result["scanned"] == 3
    assert [entry["attachment_key"] for entry in result["results"]] == [
        "A1",
        "A2",
        "B1",
    ]


def test_scihub_max_items_is_global_across_libraries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first_config = _config(tmp_path / "library-1")
    second_config = _config(tmp_path / "library-2")
    stores = {
        first_config.zotero_data_dir: _FakeStore(
            items=[
                _item("A1", first_config.zotero_data_dir),
                _item("A2", first_config.zotero_data_dir),
            ]
        ),
        second_config.zotero_data_dir: _FakeStore(
            items=[
                _item("B1", second_config.zotero_data_dir),
                _item("B2", second_config.zotero_data_dir),
            ]
        ),
    }
    _install_stores(monkeypatch, stores)
    processor = _processor([first_config, second_config])

    result = scanner.scihub_pdf_backlog_scan(
        processor,
        max_items=3,
        limit=None,
        force=False,
        library_id=None,
        data_dir=None,
        collection=None,
    )

    assert result["scanned"] == 3
    assert [entry["parent_item_key"] for entry in result["results"]] == [
        "A1",
        "A2",
        "B1",
    ]


def test_queue_limit_is_global_across_libraries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first_config = _config(tmp_path / "library-1")
    second_config = _config(tmp_path / "library-2")
    stores = {
        first_config.zotero_data_dir: _FakeStore(
            items=[
                _item("A1", first_config.zotero_data_dir),
                _item("A2", first_config.zotero_data_dir),
            ]
        ),
        second_config.zotero_data_dir: _FakeStore(
            items=[
                _item("B1", second_config.zotero_data_dir),
                _item("B2", second_config.zotero_data_dir),
            ]
        ),
    }
    _install_stores(monkeypatch, stores)
    processor = _processor(
        [first_config, second_config],
        enqueue_parent=lambda **kwargs: {
            "parent_item_key": kwargs["metadata"].key,
            "job": {"created": True, "status": "queued"},
        },
    )

    result = _full_text_scan(processor, max_items=None, limit=3)

    assert result["scanned"] == 3
    assert result["queued"] == 3
    assert [entry["parent_item_key"] for entry in result["results"]] == [
        "A1",
        "A2",
        "B1",
    ]


@pytest.mark.parametrize(
    "bindings",
    [
        {},
        ["not-an-object"],
        [{"libraryId": 123, "dataDir": "C:/data"}],
        [{"libraryId": "REMOTE", "dataDir": False}],
    ],
)
def test_parent_filtered_scan_rejects_malformed_binding_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    bindings: object,
) -> None:
    config = _config(tmp_path / "library")
    processor = _processor([config])
    monkeypatch.setenv("ZFR_LIBRARY_BINDINGS", json.dumps(bindings))

    with pytest.raises(ValueError, match="ZFR_LIBRARY_BINDINGS"):
        _full_text_scan(processor, max_items=1, filters={"REMOTE": ["P1"]})


def test_parent_filter_rejects_duplicate_normalized_library_ids(tmp_path: Path) -> None:
    config = _config(tmp_path / "library")
    processor = _processor([config])

    with pytest.raises(ValueError, match="duplicate normalized"):
        _full_text_scan(
            processor,
            max_items=1,
            filters={" REMOTE ": ["P1"], "REMOTE": ["P2"]},
        )
