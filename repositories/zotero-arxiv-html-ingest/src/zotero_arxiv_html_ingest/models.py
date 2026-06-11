from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LocalAttachment:
    library_id: str
    data_dir: Path
    storage_dir: Path
    key: str
    file_path: Path
    item_id: int | None = None
    parent_item_id: int | None = None
    parent_key: str | None = None
    zotero_path: str | None = None

    @property
    def filename(self) -> str:
        return self.file_path.name

    @property
    def state_key(self) -> str:
        return f"{self.library_id}_{self.key}"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["data_dir"] = str(self.data_dir)
        payload["storage_dir"] = str(self.storage_dir)
        payload["file_path"] = str(self.file_path)
        return payload


@dataclass(frozen=True)
class ArxivCandidate:
    arxiv_id: str
    score: float
    title: str = ""
    abstract: str = ""
    url: str = ""
    doi: str = ""
    source: str = "arxiv"
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ArxivHtmlArtifact:
    path: Path
    manifest_path: Path
    candidate: ArxivCandidate
    html_url: str
    validation: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "manifest_path": str(self.manifest_path),
            "candidate": self.candidate.to_dict(),
            "html_url": self.html_url,
            "validation": self.validation,
        }

