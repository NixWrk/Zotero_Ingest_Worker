from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .html_fetch import ArxivHtmlClient, validate_arxiv_html
from .lookup import ArxivLookupClient
from .models import LocalAttachment
from .storage import write_arxiv_html_artifact


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="zotero-arxiv-html-ingest")
    subparsers = parser.add_subparsers(dest="command")

    lookup_parser = subparsers.add_parser("lookup", help="Look up an arXiv record.")
    lookup_group = lookup_parser.add_mutually_exclusive_group(required=True)
    lookup_group.add_argument("--arxiv-id")
    lookup_group.add_argument("--title")

    fetch_parser = subparsers.add_parser("fetch", help="Fetch arXiv HTML and write a local artifact.")
    fetch_parser.add_argument("--arxiv-id", required=True)
    fetch_parser.add_argument("--output-root", required=True, type=Path)
    fetch_parser.add_argument("--library-id", required=True)
    fetch_parser.add_argument("--attachment-key", required=True)
    fetch_parser.add_argument("--source-pdf", required=True, type=Path)
    fetch_parser.add_argument("--filename", default=None)

    validate_parser = subparsers.add_parser("validate", help="Validate an HTML file.")
    validate_parser.add_argument("html", type=Path)
    validate_parser.add_argument("--min-text-chars", type=int, default=200)

    args = parser.parse_args(argv)

    if args.command == "lookup":
        client = ArxivLookupClient()
        candidate = client.by_id(args.arxiv_id) if args.arxiv_id else client.by_title(args.title)
        print(json.dumps(candidate.to_dict() if candidate else None, ensure_ascii=False, indent=2))
        return 0

    if args.command == "fetch":
        filename = args.filename or args.source_pdf.name
        attachment = LocalAttachment(
            library_id=args.library_id,
            data_dir=args.source_pdf.parent.parent.parent if len(args.source_pdf.parts) >= 3 else args.source_pdf.parent,
            storage_dir=args.source_pdf.parent.parent if len(args.source_pdf.parts) >= 2 else args.source_pdf.parent,
            key=args.attachment_key,
            file_path=args.source_pdf,
        )
        lookup = ArxivLookupClient()
        candidate = lookup.by_id(args.arxiv_id)
        if candidate is None:
            raise RuntimeError(f"arXiv record was not found: {args.arxiv_id}")
        html_text, validation = ArxivHtmlClient().fetch(candidate.arxiv_id)
        attachment = LocalAttachment(
            library_id=attachment.library_id,
            data_dir=attachment.data_dir,
            storage_dir=attachment.storage_dir,
            key=attachment.key,
            file_path=args.source_pdf.with_name(filename),
        )
        artifact = write_arxiv_html_artifact(
            root=args.output_root,
            attachment=attachment,
            candidate=candidate,
            html_text=html_text,
            validation=validation,
            source_pdf=args.source_pdf,
        )
        print(json.dumps(artifact.to_dict(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "validate":
        result = validate_arxiv_html(
            args.html.read_text(encoding="utf-8"),
            min_text_chars=args.min_text_chars,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

