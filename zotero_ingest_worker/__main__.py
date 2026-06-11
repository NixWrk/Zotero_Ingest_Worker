from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace

from .config import WorkerConfig, from_env
from .full_run import FullRunManager
from .metadata_processor import (
    METADATA_JOB_ARXIV_HTML,
    METADATA_JOB_ENRICH,
    METADATA_JOB_FULL_TEXT,
    METADATA_JOB_RESEARCHGATE_PDF,
    METADATA_JOB_SCIHUB_PDF,
)
from .service import run_server
from .service_actions import run_post_action


CLI_POST_ACTIONS = {
    "metadata-queue": "/api/zotero/metadata/queue/summary",
    "metadata-backlog-scan": "/api/zotero/metadata/enrich/backlog-scan",
    "metadata-drain-queue": "/api/zotero/metadata/enrich/queue/drain",
    "arxiv-html-backlog-scan": "/api/zotero/arxiv-html/backlog-scan",
    "arxiv-html-drain-queue": "/api/zotero/arxiv-html/queue/drain",
    "full-text-backlog-scan": "/api/zotero/full-text/backlog-scan",
    "full-text-drain-queue": "/api/zotero/full-text/queue/drain",
    "researchgate-pdf-drain-queue": "/api/zotero/researchgate-pdf/queue/drain",
    "scihub-pdf-backlog-scan": "/api/zotero/scihub-pdf/backlog-scan",
    "scihub-pdf-drain-queue": "/api/zotero/scihub-pdf/queue/drain",
    "full-run-start": "/api/zotero/pipeline/full-run/start",
    "full-run-status": "/api/zotero/pipeline/full-run/status",
    "full-run-stop": "/api/zotero/pipeline/full-run/stop",
}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="zotero-ingest-worker")
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser("serve", help="Run the local HTTP bridge for the orchestrator.")
    serve_parser.add_argument("--host", default=None)
    serve_parser.add_argument("--port", type=int, default=None)

    metadata_queue_parser = subparsers.add_parser(
        "metadata-queue",
        help="Show metadata/full-text discovery queue summary and jobs.",
    )
    metadata_queue_parser.add_argument(
        "--type",
        choices=(
            METADATA_JOB_ENRICH,
            METADATA_JOB_ARXIV_HTML,
            METADATA_JOB_FULL_TEXT,
            METADATA_JOB_RESEARCHGATE_PDF,
            METADATA_JOB_SCIHUB_PDF,
        ),
        default=None,
    )
    metadata_queue_parser.add_argument("--status", action="append", default=None)
    metadata_queue_parser.add_argument("--limit", type=int, default=100)

    _add_backlog_parser(
        subparsers,
        "metadata-backlog-scan",
        "Inspect configured Zotero libraries and enqueue parent metadata enrichment jobs.",
    )
    metadata_drain_parser = subparsers.add_parser(
        "metadata-drain-queue",
        help="Process queued parent metadata enrichment jobs.",
    )
    _add_drain_args(metadata_drain_parser)
    metadata_drain_parser.add_argument(
        "--policy",
        choices=("emptyFieldsOnly", "allowOverwrite"),
        default=None,
    )

    _add_backlog_parser(
        subparsers,
        "arxiv-html-backlog-scan",
        "Inspect configured Zotero libraries and enqueue arXiv HTML fetch jobs.",
    )
    arxiv_drain_parser = subparsers.add_parser(
        "arxiv-html-drain-queue",
        help="Process queued arXiv HTML fetch jobs.",
    )
    _add_drain_args(arxiv_drain_parser)

    _add_backlog_parser(
        subparsers,
        "full-text-backlog-scan",
        "Inspect configured Zotero libraries and enqueue external full-text discovery jobs.",
    )
    fulltext_drain_parser = subparsers.add_parser(
        "full-text-drain-queue",
        help="Process queued external full-text discovery jobs.",
    )
    fulltext_drain_parser.add_argument("--limit", type=int, default=1)
    fulltext_drain_parser.add_argument("--dry-run", action="store_true")

    researchgate_pdf_drain_parser = subparsers.add_parser(
        "researchgate-pdf-drain-queue",
        help="Process queued ResearchGate browser PDF jobs.",
    )
    _add_drain_args(researchgate_pdf_drain_parser)

    scihub_pdf_backlog_parser = _add_backlog_parser(
        subparsers,
        "scihub-pdf-backlog-scan",
        "Queue Sci-Hub fallback jobs for parent items that still have no PDF.",
    )
    scihub_pdf_backlog_parser.add_argument("--auto-drain", action="store_true")
    scihub_pdf_backlog_parser.add_argument("--drain-limit", type=int, default=1)
    scihub_pdf_backlog_parser.add_argument("--dry-run", action="store_true")
    scihub_pdf_backlog_parser.add_argument("--no-require-relay", action="store_true")

    scihub_pdf_drain_parser = subparsers.add_parser(
        "scihub-pdf-drain-queue",
        help="Process queued Sci-Hub PDF fallback jobs.",
    )
    _add_drain_args(scihub_pdf_drain_parser)

    full_run_start = subparsers.add_parser(
        "full-run-start",
        help="Run the complete ingest cycle in the background.",
    )
    full_run_start.add_argument("--max-items", type=int, default=None)
    full_run_start.add_argument("--limit", type=int, default=None)
    full_run_start.add_argument("--drain-limit", type=int, default=1)
    full_run_start.add_argument("--poll-seconds", type=int, default=60)
    full_run_start.add_argument("--intake-interval-seconds", type=int, default=300)
    full_run_start.add_argument("--dry-run", action="store_true")
    full_run_start.add_argument("--force", action="store_true")
    full_run_start.add_argument("--no-require-relay", action="store_true")

    full_run_status = subparsers.add_parser("full-run-status", help="Show latest ingest run status.")
    full_run_status.add_argument("--run-id", default=None)
    full_run_status.add_argument("--event-limit", type=int, default=50)

    full_run_stop = subparsers.add_parser("full-run-stop", help="Request ingest run stop.")
    full_run_stop.add_argument("--run-id", default=None)

    subparsers.add_parser("check-config", help="Validate required Zotero ingest configuration.")

    args = parser.parse_args(argv)
    config = from_env()

    if args.command == "serve":
        updates = {}
        if args.host:
            updates["worker_host"] = args.host
        if args.port:
            updates["worker_port"] = args.port
        run_server(replace(config, **updates) if updates else config)
        return 0

    if args.command == "check-config":
        config.validate_for_scan()
        _print_json(
            {
                "ok": True,
                "service": "zotero-ingest-worker",
                "zotero_discovery_roots": [str(path) for path in config.zotero_discovery_roots],
                "zotero_discovery_max_depth": config.zotero_discovery_max_depth,
                "zotero_data_dirs": [str(path) for path in config.zotero_data_dirs],
                "zotero_path_prefix_map": [
                    [source_prefix, target_prefix]
                    for source_prefix, target_prefix in config.zotero_path_prefix_map
                ],
                "state_db": str(config.state_db_path),
                "metadata_policy": config.metadata_policy,
                "metadata_title_min_score": config.metadata_title_min_score,
                "metadata_extended_providers_enabled": config.metadata_extended_providers_enabled,
                "metadata_request_timeout_seconds": config.metadata_request_timeout_seconds,
                "metadata_crossref_email_configured": bool(config.metadata_crossref_email),
                "metadata_unpaywall_email_configured": bool(
                    config.metadata_unpaywall_email or config.metadata_crossref_email
                ),
                "metadata_openalex_api_key_configured": bool(config.metadata_openalex_api_key),
                "metadata_semantic_scholar_api_key_configured": bool(
                    config.metadata_semantic_scholar_api_key
                ),
                "metadata_core_api_key_configured": bool(config.metadata_core_api_key),
                "arxiv_html_root": str(config.arxiv_html_root),
                "arxiv_html_attach": config.arxiv_html_attach,
                "arxiv_search_min_score": config.arxiv_search_min_score,
                "scihub_enabled": config.scihub_enabled,
                "worker": f"http://{config.worker_host}:{config.worker_port}",
            }
        )
        return 0

    shared_result = _run_shared_cli_action(args.command, args, config)
    if shared_result is not None:
        _print_json(shared_result)
        return 0

    parser.print_help()
    return 0


def _add_backlog_parser(
    subparsers: argparse._SubParsersAction,
    name: str,
    help_text: str,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(name, help=help_text)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--max-items", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--library-id", default=None)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--collection", default=None)
    return parser


def _add_drain_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-require-relay", action="store_true")


def _run_shared_cli_action(
    command: str | None,
    args: argparse.Namespace,
    config: WorkerConfig,
) -> dict[str, object] | None:
    if command not in CLI_POST_ACTIONS:
        return None
    payload = _cli_action_payload(command, args)
    return run_post_action(
        CLI_POST_ACTIONS[command],
        config,
        payload,
        FullRunManager(config),
    )


def _cli_action_payload(command: str, args: argparse.Namespace) -> dict[str, object]:
    if command == "metadata-queue":
        return {"type": args.type, "statuses": args.status, "limit": args.limit}
    if command in {"metadata-backlog-scan", "arxiv-html-backlog-scan", "full-text-backlog-scan"}:
        return _backlog_payload(args)
    if command == "scihub-pdf-backlog-scan":
        return {
            **_backlog_payload(args),
            "auto_drain": bool(args.auto_drain),
            "drain_limit": args.drain_limit,
            "dry_run": bool(args.dry_run),
            "require_relay": not bool(args.no_require_relay),
        }
    if command in {
        "metadata-drain-queue",
        "arxiv-html-drain-queue",
        "researchgate-pdf-drain-queue",
        "scihub-pdf-drain-queue",
    }:
        payload: dict[str, object] = {
            "limit": args.limit,
            "dry_run": bool(args.dry_run),
            "require_relay": not bool(args.no_require_relay),
        }
        if command == "metadata-drain-queue":
            payload["policy"] = args.policy
        return payload
    if command == "full-text-drain-queue":
        return {"limit": args.limit, "dry_run": bool(args.dry_run)}
    if command == "full-run-start":
        return {
            "max_items": args.max_items,
            "limit": args.limit,
            "drain_limit": args.drain_limit,
            "poll_seconds": args.poll_seconds,
            "intake_interval_seconds": args.intake_interval_seconds,
            "dry_run": bool(args.dry_run),
            "force": bool(args.force),
            "require_relay": not bool(args.no_require_relay),
        }
    if command == "full-run-status":
        return {"run_id": args.run_id, "event_limit": args.event_limit}
    if command == "full-run-stop":
        return {"run_id": args.run_id}
    raise ValueError(f"Unsupported shared CLI action: {command}")


def _backlog_payload(args: argparse.Namespace) -> dict[str, object]:
    return {
        "max_items": args.max_items,
        "limit": args.limit,
        "force": bool(args.force),
        "library_id": args.library_id,
        "data_dir": args.data_dir,
        "collection": args.collection,
    }


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
