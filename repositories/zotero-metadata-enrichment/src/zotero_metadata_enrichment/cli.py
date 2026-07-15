from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .diff import build_metadata_diff
from .discovery import SourceDiscovery, SourceDiscoveryResult
from .enrichment import EnricherConfig
from .fulltext import discover_and_download_full_text
from .html_sources import download_html_sources
from .local_zotero import LocalZoteroReader
from .models import LocalAttachment, LocalItemMetadata, MetadataCandidate
from .pdf_sources import download_pdf_sources


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="zotero-metadata-enrichment")
    subparsers = parser.add_subparsers(dest="command")

    diff_parser = subparsers.add_parser("diff", help="Build a metadata patch diff from JSON files.")
    diff_parser.add_argument("--candidate-json", required=True, type=Path)
    diff_parser.add_argument("--current-json", required=True, type=Path)
    diff_parser.add_argument("--policy", default="emptyFieldsOnly", choices=("emptyFieldsOnly", "allowOverwrite"))

    inspect_parser = subparsers.add_parser("inspect-zotero", help="Read parent metadata for one Zotero attachment.")
    inspect_parser.add_argument("--data-dir", required=True, type=Path)
    inspect_parser.add_argument("--attachment-key", default="")
    inspect_parser.add_argument("--item-key", default="")

    discover_parser = subparsers.add_parser("discover-sources", help="Discover metadata/full-text sources for one Zotero attachment.")
    discover_parser.add_argument("--data-dir", required=True, type=Path)
    discover_parser.add_argument("--attachment-key", default="")
    discover_parser.add_argument("--item-key", default="")
    discover_parser.add_argument("--mailto", default="")
    discover_parser.add_argument("--unpaywall-email", default="")
    discover_parser.add_argument("--openalex-api-key", default="")
    discover_parser.add_argument("--semantic-scholar-api-key", default="")
    discover_parser.add_argument("--core-api-key", default="")
    discover_parser.add_argument("--timeout-seconds", type=int, default=30)

    html_parser = subparsers.add_parser("download-html-sources", help="Discover source locations and save reachable non-PDF HTML.")
    html_parser.add_argument("--data-dir", required=True, type=Path)
    html_parser.add_argument("--attachment-key", default="")
    html_parser.add_argument("--item-key", default="")
    html_parser.add_argument("--output-dir", required=True, type=Path)
    html_parser.add_argument("--mailto", default="")
    html_parser.add_argument("--unpaywall-email", default="")
    html_parser.add_argument("--openalex-api-key", default="")
    html_parser.add_argument("--semantic-scholar-api-key", default="")
    html_parser.add_argument("--core-api-key", default="")
    html_parser.add_argument("--timeout-seconds", type=int, default=30)
    html_parser.add_argument("--max-downloads", type=int, default=5)
    html_parser.add_argument("--max-assets", type=int, default=80)
    html_parser.add_argument("--no-assets", action="store_true")

    pdf_parser = subparsers.add_parser("download-pdf-sources", help="Discover source locations and save reachable legal PDF full text.")
    pdf_parser.add_argument("--data-dir", required=True, type=Path)
    pdf_parser.add_argument("--attachment-key", default="")
    pdf_parser.add_argument("--item-key", default="")
    pdf_parser.add_argument("--output-dir", required=True, type=Path)
    pdf_parser.add_argument("--mailto", default="")
    pdf_parser.add_argument("--unpaywall-email", default="")
    pdf_parser.add_argument("--openalex-api-key", default="")
    pdf_parser.add_argument("--semantic-scholar-api-key", default="")
    pdf_parser.add_argument("--core-api-key", default="")
    pdf_parser.add_argument("--timeout-seconds", type=int, default=30)
    pdf_parser.add_argument("--max-downloads", type=int, default=3)

    fulltext_parser = subparsers.add_parser("download-full-text-sources", help="Save article HTML first; if none is found, save legal PDF full text.")
    fulltext_parser.add_argument("--data-dir", required=True, type=Path)
    fulltext_parser.add_argument("--attachment-key", default="")
    fulltext_parser.add_argument("--item-key", default="")
    fulltext_parser.add_argument("--output-dir", required=True, type=Path)
    fulltext_parser.add_argument("--mailto", default="")
    fulltext_parser.add_argument("--unpaywall-email", default="")
    fulltext_parser.add_argument("--openalex-api-key", default="")
    fulltext_parser.add_argument("--semantic-scholar-api-key", default="")
    fulltext_parser.add_argument("--core-api-key", default="")
    fulltext_parser.add_argument("--timeout-seconds", type=int, default=30)
    fulltext_parser.add_argument("--max-html-downloads", type=int, default=5)
    fulltext_parser.add_argument("--max-pdf-downloads", type=int, default=3)
    fulltext_parser.add_argument("--max-assets", type=int, default=80)
    fulltext_parser.add_argument("--no-assets", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "diff":
        candidate_payload = json.loads(args.candidate_json.read_text(encoding="utf-8"))
        current_fields = json.loads(args.current_json.read_text(encoding="utf-8"))
        candidate = MetadataCandidate(
            source=str(candidate_payload.get("source") or "json"),
            identifier=str(candidate_payload.get("identifier") or ""),
            score=float(candidate_payload.get("score") or 0),
            fields=dict(candidate_payload.get("fields") or {}),
            raw=dict(candidate_payload.get("raw") or {}),
        )
        print(
            json.dumps(
                build_metadata_diff(candidate, current_fields=current_fields, policy=args.policy),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "inspect-zotero":
        reader = LocalZoteroReader(args.data_dir)
        attachment, metadata = _attachment_and_metadata_for_args(reader, args)
        print(
            json.dumps(
                {
                    "attachment": attachment.to_dict() if attachment else None,
                    "parent_metadata": metadata.to_dict() if metadata else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "discover-sources":
        reader = LocalZoteroReader(args.data_dir)
        attachment, metadata = _attachment_and_metadata_for_args(reader, args)
        if metadata is None:
            raise RuntimeError("Parent metadata was not found.")
        config = EnricherConfig(
            crossref_mailto=args.mailto,
            unpaywall_email=args.unpaywall_email,
            openalex_api_key=args.openalex_api_key,
            semantic_scholar_api_key=args.semantic_scholar_api_key,
            core_api_key=args.core_api_key,
            request_timeout_seconds=args.timeout_seconds,
        )
        discovery_result = SourceDiscovery(config).discover(metadata=metadata, attachment=attachment)
        print(json.dumps(discovery_result.to_dict(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "download-html-sources":
        attachment, metadata, config, discovery = _discover_for_args(args)
        html_downloads = download_html_sources(
            discovery.locations,
            output_dir=args.output_dir,
            limit=args.max_downloads,
            timeout_seconds=args.timeout_seconds,
            user_agent=config.user_agent,
            expected_title=metadata.title,
            save_assets=not bool(args.no_assets),
            max_assets=args.max_assets,
        )
        print(
            json.dumps(
                {
                    "attachment": attachment.to_dict(),
                    "parent_metadata": metadata.to_dict(),
                    "candidate_count": len(discovery.candidates),
                    "location_count": len(discovery.locations),
                    "provider_events": discovery.provider_events,
                    "downloads": [result.to_dict() for result in html_downloads],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "download-pdf-sources":
        attachment, metadata, _config, discovery = _discover_for_args(args)
        pdf_downloads = download_pdf_sources(
            discovery.locations,
            output_dir=args.output_dir,
            limit=args.max_downloads,
            timeout_seconds=args.timeout_seconds,
            expected_title=metadata.title,
            require_text_identity=True,
        )
        print(
            json.dumps(
                {
                    "attachment": attachment.to_dict(),
                    "parent_metadata": metadata.to_dict(),
                    "candidate_count": len(discovery.candidates),
                    "location_count": len(discovery.locations),
                    "provider_events": discovery.provider_events,
                    "downloads": [result.to_dict() for result in pdf_downloads],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "download-full-text-sources":
        attachment, metadata, config, discovery = _discover_for_args(args)
        full_text_result = discover_and_download_full_text(
            metadata=metadata,
            attachment=attachment,
            output_dir=args.output_dir,
            config=config,
            max_html_downloads=args.max_html_downloads,
            max_pdf_downloads=args.max_pdf_downloads,
            max_assets=args.max_assets,
            save_assets=not bool(args.no_assets),
            require_pdf_text_identity=True,
        )
        print(
            json.dumps(
                full_text_result.to_dict(),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    parser.print_help()
    return 0


def _discover_for_args(
    args: argparse.Namespace,
) -> tuple[LocalAttachment, LocalItemMetadata, EnricherConfig, SourceDiscoveryResult]:
    reader = LocalZoteroReader(args.data_dir)
    attachment, metadata = _attachment_and_metadata_for_args(reader, args)
    if metadata is None:
        raise RuntimeError("Parent metadata was not found.")
    config = EnricherConfig(
        crossref_mailto=args.mailto,
        unpaywall_email=args.unpaywall_email,
        openalex_api_key=args.openalex_api_key,
        semantic_scholar_api_key=args.semantic_scholar_api_key,
        core_api_key=args.core_api_key,
        request_timeout_seconds=args.timeout_seconds,
    )
    discovery = SourceDiscovery(config).discover(metadata=metadata, attachment=attachment)
    return attachment, metadata, config, discovery


def _attachment_and_metadata_for_args(
    reader: LocalZoteroReader,
    args: argparse.Namespace,
) -> tuple[LocalAttachment, LocalItemMetadata | None]:
    attachment_key = str(getattr(args, "attachment_key", "") or "").strip()
    item_key = str(getattr(args, "item_key", "") or "").strip()
    if attachment_key:
        attachment = reader.get_attachment(attachment_key)
        return attachment, reader.get_parent_metadata_for_attachment(attachment)
    if item_key:
        metadata = reader.get_item_metadata(item_key)
        filename = f"{metadata.title or item_key}.pdf"
        attachment = LocalAttachment(
            library_id=metadata.library_id,
            data_dir=metadata.data_dir,
            storage_dir=reader.storage_dir,
            key=item_key,
            item_id=None,
            parent_item_id=metadata.item_id,
            parent_key=metadata.key,
            file_path=Path(filename),
        )
        return attachment, metadata
    raise RuntimeError("Either --attachment-key or --item-key is required.")


if __name__ == "__main__":
    raise SystemExit(main())
