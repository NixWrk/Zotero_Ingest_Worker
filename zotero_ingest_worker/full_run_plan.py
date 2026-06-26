from __future__ import annotations

from typing import Any

from .full_run_options import FullRunOptions


def next_ingest_action(
    options: FullRunOptions,
    *,
    metadata_queue: dict[str, Any] | None = None,
    arxiv_html_queue: dict[str, Any] | None = None,
    full_text_queue: dict[str, Any] | None = None,
    researchgate_pdf_queue: dict[str, Any] | None = None,
    scihub_pdf_queue: dict[str, Any] | None = None,
    scihub_pdf_backlog_pending: bool = False,
) -> str | None:
    actions = ready_ingest_actions(
        options,
        metadata_queue=metadata_queue,
        arxiv_html_queue=arxiv_html_queue,
        full_text_queue=full_text_queue,
        researchgate_pdf_queue=researchgate_pdf_queue,
        scihub_pdf_queue=scihub_pdf_queue,
        scihub_pdf_backlog_pending=scihub_pdf_backlog_pending,
    )
    return actions[0] if actions else None


def ready_ingest_actions(
    options: FullRunOptions,
    *,
    metadata_queue: dict[str, Any] | None = None,
    arxiv_html_queue: dict[str, Any] | None = None,
    full_text_queue: dict[str, Any] | None = None,
    researchgate_pdf_queue: dict[str, Any] | None = None,
    scihub_pdf_queue: dict[str, Any] | None = None,
    scihub_pdf_backlog_pending: bool = False,
) -> list[str]:
    actions: list[str] = []
    metadata_queued = _queued_count(metadata_queue)
    arxiv_html_queued = _queued_count(arxiv_html_queue)
    full_text_queued = _queued_count(full_text_queue)
    researchgate_pdf_queued = _queued_count(researchgate_pdf_queue)
    scihub_pdf_queued = _queued_count(scihub_pdf_queue)
    if options.metadata_drain and metadata_queued > 0 and _running_count(metadata_queue) == 0:
        actions.append("metadata")
    if options.full_text_drain and full_text_queued > 0 and _running_count(full_text_queue) == 0:
        actions.append("full_text")
    if (
        options.researchgate_pdf_drain
        and researchgate_pdf_queued > 0
        and _running_count(researchgate_pdf_queue) == 0
    ):
        actions.append("researchgate_pdf")
    if (
        options.arxiv_html_drain
        and arxiv_html_queued > 0
        and _running_count(arxiv_html_queue) == 0
    ):
        actions.append("arxiv_html")
    if scihub_pdf_backlog_pending:
        actions.append("scihub_pdf_backlog")
    if options.scihub_pdf_drain and scihub_pdf_queued > 0 and _running_count(scihub_pdf_queue) == 0:
        actions.append("scihub_pdf")
    return actions


def _queued_count(queue: dict[str, Any] | None) -> int:
    return int((queue or {}).get("queued") or 0)


def _running_count(queue: dict[str, Any] | None) -> int:
    return int((queue or {}).get("running") or 0)
