from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .package_paths import ensure_local_package_paths

ensure_local_package_paths()

from zotero_metadata_enrichment.attachment_types import (  # type: ignore[import-not-found]
    is_html_attachment,
    is_pdf_attachment,
)


@dataclass(frozen=True)
class FullTextAttachmentRecord:
    key: str
    content_type: str
    path: str
    title: str = ""
    file_path: str = ""
    exists: bool | None = None

    @property
    def is_pdf(self) -> bool:
        return is_pdf_attachment(
            content_type=self.content_type,
            path=self.path,
            file_path=self.file_path,
        )

    @property
    def is_html(self) -> bool:
        return is_html_attachment(
            content_type=self.content_type,
            path=self.path,
            file_path=self.file_path,
        )

    @property
    def is_source_html(self) -> bool:
        if not self.is_html:
            return False
        haystack = f"{self.title} {self.path} {self.file_path}".casefold()
        if "[source html]" in haystack or re.search(r"\bsource\s+html\b", haystack) is not None:
            return True
        match = re.search(
            r"\[([a-z0-9]{2,12}|mixed|unknown) html\](?:\.x?html?)?",
            haystack,
            re.IGNORECASE,
        )
        return bool(match and _is_source_html_language_marker(match.group(1)))

    @property
    def is_generated_html(self) -> bool:
        if not self.is_html:
            return False
        haystack = f"{self.title} {self.path} {self.file_path}".casefold()
        return re.search(r"\[(?:[a-z]{2,12}|mixed|unknown) html\]\.html?\b", haystack) is not None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["is_pdf"] = self.is_pdf
        payload["is_html"] = self.is_html
        payload["is_source_html"] = self.is_source_html
        payload["is_generated_html"] = self.is_generated_html
        return payload


@dataclass(frozen=True)
class FullTextInventory:
    attachments: tuple[FullTextAttachmentRecord, ...]

    @property
    def pdf_count(self) -> int:
        return sum(1 for item in self.attachments if item.is_pdf)

    @property
    def html_count(self) -> int:
        # Only real HTML counts: a record whose file is missing on disk
        # (e.g. an unsynced Zotero snapshot) is not usable full text.
        return sum(1 for item in self.attachments if item.is_html and item.exists is not False)

    @property
    def source_html_count(self) -> int:
        return sum(1 for item in self.attachments if item.is_source_html and item.exists is not False)

    @property
    def generated_html_count(self) -> int:
        return sum(1 for item in self.attachments if item.is_generated_html and item.exists is not False)

    @property
    def unknown_html_count(self) -> int:
        return sum(
            1
            for item in self.attachments
            if item.is_html
            and not item.is_source_html
            and not item.is_generated_html
            and item.exists is not False
        )

    @property
    def missing_file_count(self) -> int:
        return sum(1 for item in self.attachments if item.exists is False)

    @property
    def has_pdf(self) -> bool:
        return self.pdf_count > 0

    @property
    def has_html(self) -> bool:
        return self.html_count > 0

    @property
    def has_source_html(self) -> bool:
        return self.source_html_count > 0

    def to_dict(self) -> dict[str, object]:
        return {
            "pdf_count": self.pdf_count,
            "html_count": self.html_count,
            "source_html_count": self.source_html_count,
            "generated_html_count": self.generated_html_count,
            "unknown_html_count": self.unknown_html_count,
            "missing_file_count": self.missing_file_count,
            "has_pdf": self.has_pdf,
            "has_html": self.has_html,
            "has_source_html": self.has_source_html,
            "attachments": [item.to_dict() for item in self.attachments],
        }


def inventory_has_pdf(inventory: dict[str, object] | FullTextInventory) -> bool:
    if isinstance(inventory, FullTextInventory):
        return inventory.has_pdf
    return bool(inventory.get("has_pdf"))


def inventory_has_source_html(inventory: dict[str, object] | FullTextInventory) -> bool:
    if isinstance(inventory, FullTextInventory):
        return inventory.has_source_html
    if "has_source_html" in inventory:
        return bool(inventory.get("has_source_html"))
    return bool(inventory.get("has_html"))


def should_skip_full_text_scan(inventory: dict[str, object] | FullTextInventory) -> bool:
    return inventory_has_pdf(inventory) and inventory_has_source_html(inventory)


def pdf_download_limit(inventory: dict[str, object] | FullTextInventory, *, default: int = 3) -> int:
    return 0 if inventory_has_pdf(inventory) else default


def inventory_fingerprint(inventory: dict[str, object] | FullTextInventory) -> str:
    if isinstance(inventory, FullTextInventory):
        data = inventory.to_dict()
    else:
        data = inventory
    return (
        f"pdf={int(bool(data.get('has_pdf')))}:{int(data.get('pdf_count') or 0)}|"
        f"source_html={int(bool(data.get('has_source_html')))}:{int(data.get('source_html_count') or 0)}|"
        f"html={int(bool(data.get('has_html')))}:{int(data.get('html_count') or 0)}"
    )


def _is_source_html_language_marker(value: str) -> bool:
    marker = re.sub(r"[^a-z0-9]+", "", value.casefold())
    return marker not in {"ru", "source", "arxiv", "ocr", "fulltext", "pdf", "html"}


def resolved_attachment_path(
    *,
    storage_dir: Path,
    key: str,
    zotero_path: str,
) -> Path | None:
    if zotero_path.startswith("storage:"):
        return storage_dir / key / zotero_path.removeprefix("storage:")
    if zotero_path:
        return Path(zotero_path)
    return None
