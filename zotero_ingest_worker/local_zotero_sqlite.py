from __future__ import annotations

import sqlite3
from pathlib import Path


def connect_readonly_sqlite(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.as_posix()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.execute("pragma query_only = true")
    return connection
