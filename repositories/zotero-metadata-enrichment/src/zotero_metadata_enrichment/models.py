from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TypedDict


@dataclass(frozen=True)
class LocalAttachment:
    library_id: str
    data_dir: Path
    storage_dir: Path
    key: str
    item_id: int | None
    parent_item_id: int | None
    parent_key: str | None
    file_path: Path
    zotero_path: str | None = None
    date_modified: str | None = None
    link_mode: int | None = None
    content_type: str | None = None

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
class LocalItemMetadata:
    library_id: str
    data_dir: Path
    key: str
    item_id: int
    version: int | None
    item_type: str | None
    date_modified: str | None
    fields: dict[str, str] = field(default_factory=dict)
    creators: list[dict[str, Any]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    collections: list[dict[str, Any]] = field(default_factory=list)
    relations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.fields.get("title", "")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["data_dir"] = str(self.data_dir)
        return payload


@dataclass(frozen=True)
class MetadataCandidate:
    source: str
    identifier: str
    score: float
    fields: dict[str, str]
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MetadataDiff(TypedDict):
    policy: str
    current: dict[str, str]
    candidate: dict[str, str]
    patch: dict[str, str]
    skipped_fields: dict[str, str]
    applied_fields: list[str]


@dataclass(frozen=True)
class FullTextLocation:
    source: str
    url: str
    kind: str = "landing"
    is_oa: bool | None = None
    license: str = ""
    version: str = ""
    content_type: str = ""
    repository: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EnrichmentResult:
    candidate: MetadataCandidate | None
    diff: MetadataDiff | None
    provider_events: list[dict[str, Any]]
    reason: str = ""

    @property
    def patch(self) -> dict[str, str]:
        if not self.diff:
            return {}
        return self.diff["patch"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict() if self.candidate else None,
            "diff": self.diff,
            "provider_events": self.provider_events,
            "reason": self.reason,
        }
