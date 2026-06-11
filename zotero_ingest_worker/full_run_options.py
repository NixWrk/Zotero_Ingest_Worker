from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FullRunOptions:
    max_items: int | None = None
    queue_limit: int | None = None
    drain_limit: int = 1
    poll_seconds: int = 60
    intake_interval_seconds: int = 300
    idle_cycles_to_complete: int = 2
    stop_when_idle: bool = True
    dry_run: bool = False
    force: bool = False
    require_relay: bool = True
    metadata_backlog_intake: bool = True
    arxiv_html_backlog_intake: bool = True
    full_text_backlog_intake: bool = True
    scihub_pdf_backlog_intake: bool = True
    metadata_drain: bool = True
    arxiv_html_drain: bool = True
    full_text_drain: bool = True
    researchgate_pdf_drain: bool = True
    scihub_pdf_drain: bool = True

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "FullRunOptions":
        metadata_backlog_intake = _bool(payload.get("metadata_backlog_intake"), True)
        arxiv_html_backlog_intake = _bool(payload.get("arxiv_html_backlog_intake"), True)
        full_text_backlog_intake = _bool(payload.get("full_text_backlog_intake"), True)
        metadata_drain = _bool(payload.get("metadata_drain"), metadata_backlog_intake)
        arxiv_html_drain = _bool(payload.get("arxiv_html_drain"), arxiv_html_backlog_intake)
        full_text_drain = _bool(payload.get("full_text_drain"), full_text_backlog_intake)
        researchgate_pdf_drain = _bool(payload.get("researchgate_pdf_drain"), full_text_drain)
        scihub_pdf_drain = _bool(payload.get("scihub_pdf_drain"), full_text_drain)
        scihub_pdf_backlog_intake = _bool(
            payload.get("scihub_pdf_backlog_intake"),
            scihub_pdf_drain,
        )
        return cls(
            max_items=_optional_int(payload.get("max_items")),
            queue_limit=_optional_int(payload.get("queue_limit") or payload.get("limit")),
            drain_limit=max(_int(payload.get("drain_limit"), 1), 1),
            poll_seconds=max(_int(payload.get("poll_seconds"), 60), 5),
            intake_interval_seconds=max(_int(payload.get("intake_interval_seconds"), 300), 30),
            idle_cycles_to_complete=max(_int(payload.get("idle_cycles_to_complete"), 2), 1),
            stop_when_idle=_bool(payload.get("stop_when_idle"), True),
            dry_run=_bool(payload.get("dry_run"), False),
            force=_bool(payload.get("force"), False),
            require_relay=_bool(payload.get("require_relay"), True),
            metadata_backlog_intake=metadata_backlog_intake,
            arxiv_html_backlog_intake=arxiv_html_backlog_intake,
            full_text_backlog_intake=full_text_backlog_intake,
            scihub_pdf_backlog_intake=scihub_pdf_backlog_intake,
            metadata_drain=metadata_drain,
            arxiv_html_drain=arxiv_html_drain,
            full_text_drain=full_text_drain,
            researchgate_pdf_drain=researchgate_pdf_drain,
            scihub_pdf_drain=scihub_pdf_drain,
        )


def _bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, list):
        return bool(value)
    return bool(value)


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
