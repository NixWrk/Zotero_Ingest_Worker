from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import MAX_OPERATION_ITEMS, MAX_TIMEOUT_SECONDS


MAX_FULL_RUN_DRAIN_ITEMS = 50_000
MAX_FULL_RUN_IDLE_CYCLES = 1_000_000


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
        metadata_backlog_intake = _bool(
            payload.get("metadata_backlog_intake"),
            True,
            field="metadata_backlog_intake",
        )
        arxiv_html_backlog_intake = _bool(
            payload.get("arxiv_html_backlog_intake"),
            True,
            field="arxiv_html_backlog_intake",
        )
        full_text_backlog_intake = _bool(
            payload.get("full_text_backlog_intake"),
            True,
            field="full_text_backlog_intake",
        )
        metadata_drain = _bool(
            payload.get("metadata_drain"),
            metadata_backlog_intake,
            field="metadata_drain",
        )
        arxiv_html_drain = _bool(
            payload.get("arxiv_html_drain"),
            arxiv_html_backlog_intake,
            field="arxiv_html_drain",
        )
        full_text_drain = _bool(
            payload.get("full_text_drain"),
            full_text_backlog_intake,
            field="full_text_drain",
        )
        researchgate_pdf_drain = _bool(
            payload.get("researchgate_pdf_drain"),
            full_text_drain,
            field="researchgate_pdf_drain",
        )
        scihub_pdf_drain = _bool(
            payload.get("scihub_pdf_drain"), full_text_drain, field="scihub_pdf_drain"
        )
        scihub_pdf_backlog_intake = _bool(
            payload.get("scihub_pdf_backlog_intake"),
            scihub_pdf_drain,
            field="scihub_pdf_backlog_intake",
        )
        queue_limit = _optional_int(payload.get("queue_limit"), field="queue_limit")
        limit_alias = _optional_int(payload.get("limit"), field="limit")
        if "queue_limit" in payload and "limit" in payload:
            if queue_limit != limit_alias:
                raise ValueError("queue_limit and limit must specify the same budget.")
        selected_queue_limit = queue_limit if "queue_limit" in payload else limit_alias
        return cls(
            max_items=_optional_int(payload.get("max_items"), field="max_items"),
            queue_limit=selected_queue_limit,
            drain_limit=_bounded_int(
                payload.get("drain_limit"),
                1,
                field="drain_limit",
                minimum=1,
                maximum=MAX_FULL_RUN_DRAIN_ITEMS,
            ),
            poll_seconds=_bounded_int(
                payload.get("poll_seconds"),
                60,
                field="poll_seconds",
                minimum=5,
                maximum=MAX_TIMEOUT_SECONDS,
            ),
            intake_interval_seconds=_bounded_int(
                payload.get("intake_interval_seconds"),
                300,
                field="intake_interval_seconds",
                minimum=30,
                maximum=MAX_TIMEOUT_SECONDS,
            ),
            idle_cycles_to_complete=_bounded_int(
                payload.get("idle_cycles_to_complete"),
                2,
                field="idle_cycles_to_complete",
                minimum=1,
                maximum=MAX_FULL_RUN_IDLE_CYCLES,
            ),
            stop_when_idle=_bool(
                payload.get("stop_when_idle"), True, field="stop_when_idle"
            ),
            dry_run=_bool(payload.get("dry_run"), False, field="dry_run"),
            force=_bool(payload.get("force"), False, field="force"),
            require_relay=_bool(
                payload.get("require_relay"), True, field="require_relay"
            ),
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


def _bool(value: Any, default: bool, *, field: str) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a JSON boolean.")
    return value


def _bounded_int(
    value: object,
    default: int,
    *,
    field: str,
    minimum: int,
    maximum: int,
) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be a JSON integer.")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}.")
    return value


def _optional_int(value: object, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be a JSON integer.")
    if value < 0:
        raise ValueError(f"{field} must be non-negative.")
    if value > MAX_OPERATION_ITEMS:
        raise ValueError(f"{field} must be at most {MAX_OPERATION_ITEMS}.")
    return value
