from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from zotero_ingest_worker.config import from_env


def _write_zotero_data_dir(path: Path) -> None:
    (path / "storage").mkdir(parents=True)
    connection = sqlite3.connect(path / "zotero.sqlite")
    try:
        connection.execute("create table items (itemID integer primary key)")
        connection.commit()
    finally:
        connection.close()


def test_from_env_prefers_relay_library_bindings_over_recursive_discovery(
    monkeypatch,
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "pc_zotero" / "Zotero_Elvis_Data"
    duplicate = tmp_path / "pc_zotero" / "updated" / "Zotero_Elvis_Data"
    _write_zotero_data_dir(canonical)
    _write_zotero_data_dir(duplicate)

    monkeypatch.setenv("ZOTERO_DATA_DIRS", "")
    monkeypatch.setenv("ZOTERO_DISCOVERY_ROOTS", str(tmp_path / "pc_zotero"))
    monkeypatch.setenv("ZOTERO_AUTO_DISCOVER", "1")
    monkeypatch.setenv(
        "ZFR_LIBRARY_BINDINGS",
        json.dumps(
            [
                {
                    "libraryId": "Zotero_Elvis_Data_test",
                    "dataDir": str(canonical),
                    "hostDataDir": str(duplicate),
                }
            ]
        ),
    )

    config = from_env(load_file=False)

    assert config.zotero_data_dirs == (canonical,)
    assert duplicate not in config.zotero_data_dirs
