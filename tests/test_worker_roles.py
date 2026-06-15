from __future__ import annotations

import pytest

from zotero_ingest_worker.worker_roles import (
    ROLE_FULLTEXT,
    ROLE_METADATA,
    ensure_role_allows_action,
    post_action_paths_for_role,
    role_mode_label,
)


def test_metadata_role_exposes_only_metadata_actions() -> None:
    paths = post_action_paths_for_role(ROLE_METADATA)

    assert "/api/zotero/metadata/enrich/backlog-scan" in paths
    assert "/api/zotero/full-text/backlog-scan" not in paths
    assert role_mode_label(ROLE_METADATA) == "metadata-only"


def test_fulltext_role_exposes_native_fulltext_actions() -> None:
    paths = post_action_paths_for_role(ROLE_FULLTEXT)

    assert "/api/zotero/full-text/backlog-scan" in paths
    assert "/api/zotero/arxiv-html/backlog-scan" in paths
    assert "/api/zotero/metadata/enrich/backlog-scan" not in paths
    assert role_mode_label(ROLE_FULLTEXT) == "native-fulltext"


def test_role_guard_blocks_wrong_queue_type() -> None:
    with pytest.raises(PermissionError, match="metadata-only"):
        ensure_role_allows_action(
            ROLE_METADATA,
            "/api/zotero/metadata/queue/summary",
            {"type": "full_text"},
        )

    with pytest.raises(PermissionError, match="native-fulltext"):
        ensure_role_allows_action(
            ROLE_FULLTEXT,
            "/api/zotero/metadata/queue/summary",
            {"type": "enrich"},
        )
