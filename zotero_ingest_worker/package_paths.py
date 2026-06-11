from __future__ import annotations

import sys
from pathlib import Path


LOCAL_PACKAGE_SRCS = (
    Path(__file__).resolve().parents[1] / "repositories" / "zotero-metadata-enrichment" / "src",
    Path(__file__).resolve().parents[1] / "repositories" / "zotero-arxiv-html-ingest" / "src",
)


def ensure_local_package_paths() -> None:
    for package_src in LOCAL_PACKAGE_SRCS:
        if not package_src.is_dir():
            continue
        package_src_text = str(package_src)
        if package_src_text not in sys.path:
            sys.path.insert(0, package_src_text)
