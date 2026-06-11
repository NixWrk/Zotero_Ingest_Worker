from __future__ import annotations

from typing import Any


def _result_summary(result: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "ok",
        "mode",
        "job_type",
        "scanned",
        "downloaded",
        "queued",
        "processed",
        "failed",
        "problem_documents",
        "skipped",
        "skipped_reason",
        "recovered",
        "recovered_expired_jobs",
    )
    return {key: result.get(key) for key in keys if key in result}


def _result_failure_count(result: dict[str, Any]) -> int:
    failed = int(result.get("failed") or 0)
    problem_documents = int(result.get("problem_documents") or 0)
    return failed + problem_documents


def _result_message(result: dict[str, Any]) -> str:
    summary = _result_summary(result)
    return ", ".join(f"{key}={value}" for key, value in summary.items()) or "No summary."
