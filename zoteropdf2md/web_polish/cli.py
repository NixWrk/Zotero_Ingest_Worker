"""CLI helpers for publisher-specific web HTML polish scripts."""

from __future__ import annotations

from pathlib import Path
import argparse
import sys

from .core import WebHtmlKind, WebHtmlPolishError
from ..web_html_polish import detect_web_html_kind, polish_web_html_file


def run_web_polish_cli(expected_kind: WebHtmlKind | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="Source HTML file.")
    parser.add_argument("output", nargs="?", type=Path, help="Output polished HTML file.")
    parser.add_argument("--source-url", help="Original source URL for same-document link canonicalization.")
    parser.add_argument("--canonical-url", help="Canonical article URL when it differs from --source-url.")
    parser.add_argument(
        "--allow-other-kind",
        action="store_true",
        help="Allow this script to process a detected kind different from the script default.",
    )
    args = parser.parse_args()

    html = args.input.read_text(encoding="utf-8", errors="replace")
    detected_kind = detect_web_html_kind(html, source_url=args.source_url)
    if expected_kind is not None and detected_kind != expected_kind and not args.allow_other_kind:
        print(
            f"Refusing {args.input}: detected {detected_kind.value}, expected {expected_kind.value}.",
            file=sys.stderr,
        )
        return 2

    output = args.output or args.input.with_suffix(".web.polish.html")
    try:
        result = polish_web_html_file(
            args.input,
            source_url=args.source_url,
            canonical_url=args.canonical_url,
        )
    except WebHtmlPolishError as exc:
        print(f"Cannot web-polish {args.input}: {exc}", file=sys.stderr)
        return 3

    output.write_text(result.html, encoding="utf-8")
    source_errors = ",".join(result.source_recovery_errors) or "-"
    print(
        "web-polish "
        f"kind={result.kind.value} "
        f"extracted={result.article_extracted} "
        f"selector={result.article_selector or '-'} "
        f"same_doc_links={result.same_document_links_rewritten} "
        f"unresolved_same_doc_links={result.unresolved_same_document_links} "
        f"inlined_images={result.inlined_images} "
        f"source_figures={result.recovered_source_figures}/{result.attempted_source_figures} "
        f"source_figure_errors={source_errors} "
        f"output={output}"
    )
    return 0


def main() -> int:
    return run_web_polish_cli()


def main_arxiv() -> int:
    return run_web_polish_cli(WebHtmlKind.ARXIV_LATEXML)


def main_pmc() -> int:
    return run_web_polish_cli(WebHtmlKind.PMC_ARTICLE)


def main_taylor_francis() -> int:
    return run_web_polish_cli(WebHtmlKind.TAYLOR_FRANCIS_ARTICLE)


def main_springer_nature() -> int:
    return run_web_polish_cli(WebHtmlKind.SPRINGER_NATURE_ARTICLE)


def main_iop() -> int:
    return run_web_polish_cli(WebHtmlKind.IOP_ARTICLE)


def main_researchgate() -> int:
    return run_web_polish_cli(WebHtmlKind.RESEARCHGATE_PAGE)


def main_ojs() -> int:
    return run_web_polish_cli(WebHtmlKind.OJS_ABSTRACT_PAGE)


def main_sciendo() -> int:
    return run_web_polish_cli(WebHtmlKind.SCIENDO_ABSTRACT_PAGE)


if __name__ == "__main__":
    raise SystemExit(main())
